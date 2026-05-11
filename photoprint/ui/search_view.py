"""Вкладка Search: обозреватель проиндексированных фото.

Архитектура:
* Источник данных — :class:`Gio.ListStore` из :class:`PhotoEntryItem`.
* Над ним :class:`Gtk.FilterListModel` (фильтр по подстроке имени) и
  :class:`Gtk.SortListModel` (сортировка задаётся колонками ColumnView).
* Выбор хранит :class:`Gtk.MultiSelection` — он же общий для grid- и
  list-видов, поэтому при переключении представления отметки сохраняются.
* Сами виджеты — :class:`Gtk.GridView` (миниатюры) и :class:`Gtk.ColumnView`
  (таблица). Оба используют :class:`Gtk.SignalListItemFactory`, поэтому
  только видимые элементы создают виджеты — 1500 фото и больше не проблема.
* Миниатюры тянет :class:`photoprint.ui.thumbnail_loader.ThumbnailLoader` в
  пуле потоков, main-тред не блокируется.
* Сканирование запускается в :mod:`threading.Thread`; новые записи поступают
  пачками каждые ~150 мс через :func:`GLib.idle_add`, поэтому фото появляются
  по мере индексации, а не в конце.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gio, GLib, GObject, Gtk  # noqa: E402

from photoprint.core.photo_index import PhotoEntry, PhotoIndex, ScanProgress  # noqa: E402
from photoprint.i18n import gettext as _  # noqa: E402
from photoprint.ui.photo_item_model import PhotoEntryItem  # noqa: E402
from photoprint.ui.thumbnail_loader import get_default as get_thumbnail_loader  # noqa: E402

logger = logging.getLogger(__name__)

THUMB_SIZE = 144     # пиксели стороны миниатюры в grid-режиме
LIST_THUMB = 48      # — то же для строк ColumnView
BATCH_FLUSH_MS = 150  # как часто переливаем накопленные записи из worker в store


def _human_size(size: int) -> str:
    """``12345`` → ``12.1 KB``. Для отображения в строке списка."""
    f = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if f < 1024 or unit == "GB":
            return f"{f:.1f} {unit}" if unit != "B" else f"{int(f)} B"
        f /= 1024
    return f"{f:.1f} GB"


class _FolderRow(Adw.ActionRow):
    """Одна строка в поповере отслеживаемых папок."""

    def __init__(self, path: Path, on_remove) -> None:
        super().__init__()
        self.set_title(path.name or str(path))
        self.set_subtitle(str(path))
        self.path = path
        btn = Gtk.Button.new_from_icon_name("user-trash-symbolic")
        btn.add_css_class("flat")
        btn.set_valign(Gtk.Align.CENTER)
        btn.set_tooltip_text(_("Remove this folder from the index"))
        btn.connect("clicked", lambda *_a: on_remove(path))
        self.add_suffix(btn)


class SearchView(Gtk.Box):
    """Содержимое вкладки Search."""

    __gsignals__ = {
        "send-to-print": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
    }

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.set_hexpand(True)
        self.set_vexpand(True)

        self._index = PhotoIndex()
        self._loader = get_thumbnail_loader()

        # -- Модель --------------------------------------------------------
        # Источник: путь -> PhotoEntryItem. set/list нужен, чтобы по mtime-апдейту
        # не дублировать запись (rescan может прислать тот же путь дважды).
        self._store = Gio.ListStore.new(PhotoEntryItem)
        self._known_paths: set[str] = set()

        self._filter = Gtk.CustomFilter.new(self._filter_match, None)
        filter_model = Gtk.FilterListModel.new(self._store, self._filter)
        self._sort_model = Gtk.SortListModel.new(filter_model, None)
        self._selection = Gtk.MultiSelection.new(self._sort_model)
        self._selection.connect("selection-changed", self._on_selection_changed)

        self._rescan_running = False
        # Буфер для инкрементальной отрисовки во время скана.
        self._pending_entries: list[PhotoEntry] = []
        self._batch_lock = threading.Lock()
        self._batch_timer_id: int | None = None

        # -- Верхняя панель ------------------------------------------------
        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        toolbar.set_margin_top(8)
        toolbar.set_margin_start(8)
        toolbar.set_margin_end(8)
        toolbar.set_margin_bottom(6)

        self._folders_btn = Gtk.MenuButton.new()
        self._folders_btn.set_icon_name("folder-symbolic")
        self._folders_btn.set_tooltip_text(_("Manage indexed folders"))
        self._folders_popover = Gtk.Popover()
        self._folders_btn.set_popover(self._folders_popover)
        self._rebuild_folders_popover()
        toolbar.append(self._folders_btn)

        self._rescan_btn = Gtk.Button.new_from_icon_name("view-refresh-symbolic")
        self._rescan_btn.set_tooltip_text(_("Rescan folders"))
        self._rescan_btn.connect("clicked", lambda *_a: self._start_rescan())
        toolbar.append(self._rescan_btn)

        self._search = Gtk.SearchEntry()
        self._search.set_placeholder_text(_("Search by name…"))
        self._search.set_hexpand(True)
        self._search.connect("search-changed", lambda *_a: self._filter.changed(
            Gtk.FilterChange.DIFFERENT
        ))
        toolbar.append(self._search)

        view_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        view_box.add_css_class("linked")
        self._grid_btn = Gtk.ToggleButton.new()
        self._grid_btn.set_icon_name("view-grid-symbolic")
        self._grid_btn.set_tooltip_text(_("Grid view"))
        self._grid_btn.set_active(True)
        self._grid_btn.connect("toggled", self._on_view_toggle)
        self._list_btn = Gtk.ToggleButton.new()
        self._list_btn.set_icon_name("view-list-symbolic")
        self._list_btn.set_tooltip_text(_("List view"))
        self._list_btn.set_group(self._grid_btn)
        self._list_btn.connect("toggled", self._on_view_toggle)
        view_box.append(self._grid_btn)
        view_box.append(self._list_btn)
        toolbar.append(view_box)

        # Меню видимости колонок таблицы — доступно только в list-режиме.
        self._cols_btn = Gtk.MenuButton.new()
        self._cols_btn.set_icon_name("view-more-symbolic")
        self._cols_btn.set_tooltip_text(_("Columns"))
        self._cols_popover = Gtk.Popover()
        self._cols_btn.set_popover(self._cols_popover)
        self._cols_btn.set_sensitive(False)
        toolbar.append(self._cols_btn)

        self.append(toolbar)

        # -- Прогресс-бар скана --------------------------------------------
        self._progress = Gtk.ProgressBar()
        self._progress.set_visible(False)
        self._progress.set_margin_start(8)
        self._progress.set_margin_end(8)
        self.append(self._progress)

        # -- Tray выбранных фото -------------------------------------------
        self._tray_revealer = Gtk.Revealer()
        self._tray_revealer.set_transition_type(
            Gtk.RevealerTransitionType.SLIDE_DOWN
        )
        tray_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        tray_row.set_margin_top(4)
        tray_row.set_margin_bottom(4)
        tray_row.set_margin_start(8)
        tray_row.set_margin_end(8)
        tray_row.add_css_class("toolbar")

        self._tray_label = Gtk.Label()
        self._tray_label.add_css_class("heading")
        self._tray_label.set_valign(Gtk.Align.CENTER)
        tray_row.append(self._tray_label)

        tray_scroller = Gtk.ScrolledWindow()
        tray_scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.NEVER)
        tray_scroller.set_min_content_height(80)
        tray_scroller.set_max_content_height(80)
        tray_scroller.set_hexpand(True)
        self._tray_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self._tray_box.set_margin_start(4)
        self._tray_box.set_margin_end(4)
        tray_scroller.set_child(self._tray_box)
        tray_row.append(tray_scroller)

        tray_clear = Gtk.Button.new_from_icon_name("edit-clear-symbolic")
        tray_clear.set_tooltip_text(_("Clear selection"))
        tray_clear.set_valign(Gtk.Align.CENTER)
        tray_clear.add_css_class("flat")
        tray_clear.connect("clicked", lambda *_a: self._selection.unselect_all())
        tray_row.append(tray_clear)

        self._tray_revealer.set_child(tray_row)
        self._tray_revealer.set_reveal_child(False)
        self.append(self._tray_revealer)

        # -- Контент --------------------------------------------------------
        self._content_stack = Gtk.Stack()
        self._content_stack.set_vexpand(True)

        empty_no_folders = Adw.StatusPage()
        empty_no_folders.set_icon_name("folder-symbolic")
        empty_no_folders.set_title(_("No folders to index"))
        empty_no_folders.set_description(
            _("Add a folder with your photos and PhotoPrint will index it.")
        )
        add_btn = Gtk.Button.new_with_label(_("Add folder…"))
        add_btn.add_css_class("pill")
        add_btn.add_css_class("suggested-action")
        add_btn.set_halign(Gtk.Align.CENTER)
        add_btn.connect("clicked", lambda *_a: self._open_folder_picker())
        empty_no_folders.set_child(add_btn)
        self._content_stack.add_named(empty_no_folders, "no-folders")

        empty_no_results = Adw.StatusPage()
        empty_no_results.set_icon_name("system-search-symbolic")
        empty_no_results.set_title(_("Nothing matches"))
        empty_no_results.set_description(
            _("Try a shorter query or rescan the folders.")
        )
        self._content_stack.add_named(empty_no_results, "no-results")

        # GridView (виртуальный)
        grid_factory = Gtk.SignalListItemFactory()
        grid_factory.connect("setup", self._grid_setup)
        grid_factory.connect("bind", self._grid_bind)
        self._gridview = Gtk.GridView.new(self._selection, grid_factory)
        self._gridview.set_max_columns(20)
        self._gridview.set_min_columns(2)
        self._gridview.set_enable_rubberband(True)
        grid_scroller = Gtk.ScrolledWindow()
        grid_scroller.set_child(self._gridview)
        grid_scroller.set_vexpand(True)
        self._content_stack.add_named(grid_scroller, "grid")

        # ColumnView (виртуальный, с настраиваемыми колонками и сортировкой)
        self._columnview = Gtk.ColumnView.new(self._selection)
        self._columnview.set_show_column_separators(True)
        self._columnview.set_show_row_separators(True)
        self._columns: dict[str, Gtk.ColumnViewColumn] = {}
        self._build_columns()
        # Сортировка модели связана с сортировщиком ColumnView: клик в шапку
        # колонки автоматически переключает порядок.
        self._sort_model.set_sorter(self._columnview.get_sorter())
        self._build_columns_popover()
        list_scroller = Gtk.ScrolledWindow()
        list_scroller.set_child(self._columnview)
        list_scroller.set_vexpand(True)
        self._content_stack.add_named(list_scroller, "list")

        self.append(self._content_stack)

        # -- Actionbar -----------------------------------------------------
        action_bar = Gtk.ActionBar()
        self._count_label = Gtk.Label()
        self._count_label.add_css_class("dim-label")
        action_bar.pack_start(self._count_label)

        self._select_all_btn = Gtk.Button.new_with_label(_("Select all"))
        self._select_all_btn.connect("clicked", lambda *_a: self._select_all())
        action_bar.pack_start(self._select_all_btn)

        self._send_btn = Gtk.Button.new_with_label(_("Send to Print"))
        self._send_btn.add_css_class("suggested-action")
        self._send_btn.set_sensitive(False)
        self._send_btn.connect("clicked", lambda *_a: self._emit_send())
        action_bar.pack_end(self._send_btn)
        self.append(action_bar)

        # Стартовая загрузка из БД (без I/O — только метаданные)
        self._reload_from_index()
        self._update_visible_stack()

    # -- Factories --------------------------------------------------------

    def _grid_setup(self, _factory, list_item: Gtk.ListItem) -> None:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.add_css_class("card")
        box.set_margin_top(2)
        box.set_margin_bottom(2)
        box.set_margin_start(2)
        box.set_margin_end(2)
        pic = Gtk.Picture()
        pic.set_size_request(THUMB_SIZE, THUMB_SIZE)
        pic.set_can_shrink(True)
        pic.set_content_fit(Gtk.ContentFit.CONTAIN)
        pic.set_margin_top(6)
        pic.set_margin_start(6)
        pic.set_margin_end(6)
        label = Gtk.Label()
        label.set_ellipsize(3)
        label.set_max_width_chars(18)
        label.add_css_class("caption")
        label.set_margin_bottom(4)
        box.append(pic)
        box.append(label)
        box._pic = pic        # type: ignore[attr-defined]
        box._label = label    # type: ignore[attr-defined]
        box._current = None   # type: ignore[attr-defined]
        self._install_toggle_gesture(box, list_item)
        list_item.set_child(box)

    def _grid_bind(self, _factory, list_item: Gtk.ListItem) -> None:
        box = list_item.get_child()
        item: PhotoEntryItem = list_item.get_item()
        entry = item.entry
        box._label.set_text(entry.name)  # type: ignore[attr-defined]
        box._pic.set_paintable(None)     # type: ignore[attr-defined]
        path = entry.path
        box._current = path              # type: ignore[attr-defined]
        self._loader.get(path, THUMB_SIZE, lambda tex,
                         _b=box, _p=path: self._apply_thumb(_b, _p, tex))

    @staticmethod
    def _apply_thumb(box, expected_path: Path, texture) -> None:
        """Установить текстуру, если виджет всё ещё показывает то же фото."""
        if getattr(box, "_current", None) != expected_path:
            return  # виджет уже забиндили на другое фото — пропускаем
        if texture is not None:
            box._pic.set_paintable(texture)  # type: ignore[attr-defined]

    def _install_toggle_gesture(self, widget: Gtk.Widget, list_item: Gtk.ListItem) -> None:
        """Перехватить левый клик и сделать его toggle-выделением.

        По умолчанию GridView/ColumnView при клике без модификаторов делают
        «выбрать только этот», что мешает копить большие подборки. Capture-фаза
        ловит клик до встроенной обработки, мы переключаем элемент через
        Gtk.MultiSelection.select_item(..., unselect_rest=False), помечаем
        gesture-sequence как CLAIMED, и встроенный обработчик не срабатывает.
        """
        gesture = Gtk.GestureClick.new()
        gesture.set_button(1)  # только ЛКМ
        gesture.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)

        def on_pressed(g, n_press, _x, _y):
            if n_press != 1:
                return
            pos = list_item.get_position()
            if pos == Gtk.INVALID_LIST_POSITION:
                return
            if self._selection.is_selected(pos):
                self._selection.unselect_item(pos)
            else:
                self._selection.select_item(pos, False)
            g.set_state(Gtk.EventSequenceState.CLAIMED)

        gesture.connect("pressed", on_pressed)
        widget.add_controller(gesture)

    def _build_columns(self) -> None:
        """Создать все колонки ColumnView. Видимостью каждой управляет попавер."""
        thumb_factory = Gtk.SignalListItemFactory()
        thumb_factory.connect("setup", self._thumb_cell_setup)
        thumb_factory.connect("bind", self._thumb_cell_bind)
        self._add_column(
            "preview", _("Preview"), thumb_factory,
            fixed_w=LIST_THUMB + 16, default_visible=True,
        )
        self._add_column(
            "name", _("Name"),
            self._text_factory(lambda e: e.name),
            expand=True, default_visible=True,
            sorter=self._make_sorter(lambda e: e.name.lower()),
        )
        self._add_column(
            "date", _("Date"),
            self._text_factory(
                lambda e: e.exif_datetime.strftime("%Y-%m-%d %H:%M")
                if e.exif_datetime else ""
            ),
            fixed_w=160, default_visible=True,
            sorter=self._make_sorter(
                lambda e: e.exif_datetime or datetime.min
            ),
        )
        self._add_column(
            "size", _("Size"),
            self._text_factory(lambda e: _human_size(e.size)),
            fixed_w=100, default_visible=True,
            sorter=self._make_sorter(lambda e: e.size),
        )
        self._add_column(
            "dimensions", _("Resolution"),
            self._text_factory(
                lambda e: f"{e.width}×{e.height}"
                if e.width and e.height else "—"
            ),
            fixed_w=120, default_visible=True,
            sorter=self._make_sorter(
                lambda e: (e.width or 0) * (e.height or 0)
            ),
        )
        self._add_column(
            "ext", _("Type"),
            self._text_factory(lambda e: e.path.suffix.lstrip(".").lower() or "—"),
            fixed_w=80, default_visible=False,
            sorter=self._make_sorter(lambda e: e.path.suffix.lower()),
        )
        self._add_column(
            "folder", _("Folder"),
            self._text_factory(lambda e: e.folder.name or str(e.folder)),
            fixed_w=180, default_visible=False,
            sorter=self._make_sorter(lambda e: str(e.folder).lower()),
        )
        self._add_column(
            "path", _("Path"),
            self._text_factory(lambda e: str(e.path)),
            expand=True, default_visible=False,
            sorter=self._make_sorter(lambda e: str(e.path).lower()),
        )
        self._add_column(
            "mtime", _("Modified"),
            self._text_factory(
                lambda e: datetime.fromtimestamp(e.mtime / 1e9).strftime(
                    "%Y-%m-%d %H:%M"
                ) if e.mtime else ""
            ),
            fixed_w=160, default_visible=False,
            sorter=self._make_sorter(lambda e: e.mtime),
        )

    def _add_column(
        self,
        key: str,
        title: str,
        factory: Gtk.ListItemFactory,
        *,
        fixed_w: int | None = None,
        expand: bool = False,
        default_visible: bool = True,
        sorter: Gtk.Sorter | None = None,
    ) -> None:
        col = Gtk.ColumnViewColumn.new(title, factory)
        if fixed_w is not None:
            col.set_fixed_width(fixed_w)
        if expand:
            col.set_expand(True)
        col.set_resizable(True)
        if sorter is not None:
            col.set_sorter(sorter)
        col.set_visible(default_visible)
        self._columnview.append_column(col)
        self._columns[key] = col

    def _text_factory(self, getter) -> Gtk.SignalListItemFactory:
        """Универсальная фабрика «один Gtk.Label со строкой по getter(entry)»."""
        factory = Gtk.SignalListItemFactory()

        def setup(_f, li):
            label = self._make_label()
            self._install_toggle_gesture(label, li)
            li.set_child(label)

        factory.connect("setup", setup)
        factory.connect(
            "bind",
            lambda _f, li: li.get_child().set_text(getter(li.get_item().entry)),
        )
        return factory

    @staticmethod
    def _make_sorter(key_fn) -> Gtk.CustomSorter:
        """Соорудить сортировщик, использующий ``key_fn(PhotoEntry)`` как ключ."""

        def cmp(a: PhotoEntryItem, b: PhotoEntryItem, _ud) -> int:
            ka, kb = key_fn(a.entry), key_fn(b.entry)
            # пустые значения в самый конец
            if ka is None and kb is None:
                return 0
            if ka is None:
                return 1
            if kb is None:
                return -1
            if ka < kb:
                return -1
            if ka > kb:
                return 1
            return 0

        return Gtk.CustomSorter.new(cmp, None)

    def _build_columns_popover(self) -> None:
        """Чекбоксы видимости колонок в всплывающем меню рядом с тоглом list."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_margin_top(8)
        box.set_margin_bottom(8)
        box.set_margin_start(8)
        box.set_margin_end(8)
        title = Gtk.Label()
        title.set_markup(f"<b>{_('Visible columns')}</b>")
        title.set_halign(Gtk.Align.START)
        title.set_margin_bottom(4)
        box.append(title)
        for key, col in self._columns.items():
            check = Gtk.CheckButton.new_with_label(col.get_title())
            check.set_active(col.get_visible())
            # «preview» не выключаем — без него таблица выглядит странно.
            if key == "preview":
                check.set_sensitive(False)
            check.connect("toggled", lambda c, _col=col: _col.set_visible(c.get_active()))
            box.append(check)
        self._cols_popover.set_child(box)

    @staticmethod
    def _make_label() -> Gtk.Label:
        label = Gtk.Label()
        label.set_halign(Gtk.Align.START)
        label.set_ellipsize(3)
        label.set_xalign(0.0)
        return label

    def _thumb_cell_setup(self, _factory, list_item: Gtk.ListItem) -> None:
        pic = Gtk.Picture()
        pic.set_size_request(LIST_THUMB, LIST_THUMB)
        pic.set_can_shrink(True)
        pic.set_content_fit(Gtk.ContentFit.CONTAIN)
        pic.set_margin_top(2)
        pic.set_margin_bottom(2)
        pic.set_margin_start(4)
        pic.set_margin_end(4)
        pic._current = None  # type: ignore[attr-defined]
        self._install_toggle_gesture(pic, list_item)
        list_item.set_child(pic)

    def _thumb_cell_bind(self, _factory, list_item: Gtk.ListItem) -> None:
        pic = list_item.get_child()
        entry = list_item.get_item().entry
        pic.set_paintable(None)
        pic._current = entry.path  # type: ignore[attr-defined]

        def apply(tex, _pic=pic, _p=entry.path):
            if getattr(_pic, "_current", None) == _p and tex is not None:
                _pic.set_paintable(tex)

        self._loader.get(entry.path, LIST_THUMB, apply)

    # -- Filter / selection -----------------------------------------------

    def _filter_match(self, item: PhotoEntryItem, _user_data=None) -> bool:
        query = self._search.get_text().strip().lower()
        if not query:
            return True
        return query in item.entry.name.lower()

    def _on_selection_changed(self, *_args) -> None:
        self._rebuild_tray()
        self._update_send_btn()

    # -- Tray --------------------------------------------------------------

    def _rebuild_tray(self) -> None:
        """Пересобрать горизонтальную полосу выбранных фото."""
        paths = self._selected_paths()
        # Скрываем revealer когда пусто
        self._tray_revealer.set_reveal_child(bool(paths))
        # Чистим
        child = self._tray_box.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self._tray_box.remove(child)
            child = nxt
        # Перезаполняем (ограничиваем 200 миниатюрами, чтобы не плодить виджеты)
        for path in paths[:200]:
            self._tray_box.append(self._make_tray_tile(path))
        if len(paths) > 200:
            more = Gtk.Label(label=_("+{n} more").format(n=len(paths) - 200))
            more.add_css_class("dim-label")
            more.set_valign(Gtk.Align.CENTER)
            self._tray_box.append(more)
        self._tray_label.set_text(_("Selected: {n}").format(n=len(paths)))

    def _make_tray_tile(self, path: Path) -> Gtk.Widget:
        """Маленькая плитка с миниатюрой и кнопкой ×."""
        overlay = Gtk.Overlay()
        overlay.set_size_request(72, 72)
        overlay.add_css_class("card")
        pic = Gtk.Picture()
        pic.set_size_request(72, 72)
        pic.set_can_shrink(True)
        pic.set_content_fit(Gtk.ContentFit.COVER)
        overlay.set_child(pic)

        close_btn = Gtk.Button.new_from_icon_name("window-close-symbolic")
        close_btn.add_css_class("circular")
        close_btn.add_css_class("osd")
        close_btn.set_halign(Gtk.Align.END)
        close_btn.set_valign(Gtk.Align.START)
        close_btn.set_margin_top(2)
        close_btn.set_margin_end(2)
        close_btn.set_tooltip_text(_("Remove from selection"))
        close_btn.connect("clicked", lambda *_a, _p=path: self._deselect_path(_p))
        overlay.add_overlay(close_btn)

        # Клик по самой миниатюре — тоже снимает (как и в основной сетке).
        gesture = Gtk.GestureClick.new()
        gesture.set_button(1)
        gesture.connect(
            "pressed", lambda *_a, _p=path: self._deselect_path(_p)
        )
        overlay.add_controller(gesture)

        def apply(tex, _pic=pic):
            if tex is not None:
                _pic.set_paintable(tex)

        self._loader.get(path, 72, apply)
        return overlay

    def _deselect_path(self, path: Path) -> None:
        """Найти позицию по пути и снять выделение."""
        for i in range(self._sort_model.get_n_items()):
            item: PhotoEntryItem = self._sort_model.get_item(i)
            if item is not None and item.entry.path == path:
                self._selection.unselect_item(i)
                return

    def _select_all(self) -> None:
        self._selection.select_all()

    def _update_send_btn(self) -> None:
        n = self._selected_count()
        self._send_btn.set_sensitive(n > 0)
        self._send_btn.set_label(
            _("Send {n} to Print").format(n=n) if n else _("Send to Print")
        )
        total_visible = self._sort_model.get_n_items()
        total_indexed = self._index.count()
        self._count_label.set_text(
            _("Showing {shown} of {total}").format(
                shown=total_visible, total=total_indexed
            )
        )

    def _selected_count(self) -> int:
        bitset = self._selection.get_selection()
        return bitset.get_size()

    def _selected_paths(self) -> list[Path]:
        bitset = self._selection.get_selection()
        n = bitset.get_size()
        out: list[Path] = []
        for i in range(n):
            idx = bitset.get_nth(i)
            item: PhotoEntryItem = self._sort_model.get_item(idx)
            if item is not None:
                out.append(item.entry.path)
        return out

    def _emit_send(self) -> None:
        paths = [str(p) for p in self._selected_paths()]
        if paths:
            self.emit("send-to-print", paths)

    # -- View toggle ------------------------------------------------------

    def _on_view_toggle(self, button: Gtk.ToggleButton) -> None:
        if not button.get_active():
            return
        # Меню колонок имеет смысл только в режиме списка.
        self._cols_btn.set_sensitive(self._list_btn.get_active())
        self._update_visible_stack()

    def _update_visible_stack(self) -> None:
        if not self._index.folders():
            self._content_stack.set_visible_child_name("no-folders")
            return
        if self._sort_model.get_n_items() == 0 and not self._rescan_running:
            self._content_stack.set_visible_child_name("no-results")
            return
        self._content_stack.set_visible_child_name(
            "grid" if self._grid_btn.get_active() else "list"
        )

    # -- Folders ----------------------------------------------------------

    def _rebuild_folders_popover(self) -> None:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_margin_top(8)
        box.set_margin_bottom(8)
        box.set_margin_start(8)
        box.set_margin_end(8)
        box.set_size_request(360, -1)

        title = Gtk.Label()
        title.set_markup(f"<b>{_('Indexed folders')}</b>")
        title.set_halign(Gtk.Align.START)
        box.append(title)

        folders = self._index.folders()
        if folders:
            list_box = Gtk.ListBox()
            list_box.add_css_class("boxed-list")
            list_box.set_selection_mode(Gtk.SelectionMode.NONE)
            for f in folders:
                list_box.append(_FolderRow(f, on_remove=self._remove_folder))
            box.append(list_box)
        else:
            empty = Gtk.Label(label=_("No folders yet."))
            empty.add_css_class("dim-label")
            box.append(empty)

        add_btn = Gtk.Button.new_with_label(_("Add folder…"))
        add_btn.connect("clicked", lambda *_a: self._open_folder_picker())
        box.append(add_btn)

        self._folders_popover.set_child(box)

    def _open_folder_picker(self) -> None:
        dialog = Gtk.FileDialog()
        dialog.set_title(_("Choose a folder to index"))
        dialog.select_folder(self.get_root(), None, self._on_folder_picked)

    def _on_folder_picked(self, dialog, result) -> None:
        try:
            f = dialog.select_folder_finish(result)
        except GLib.Error:
            return
        folder = Path(f.get_path())
        try:
            added = self._index.add_folder(folder)
        except NotADirectoryError:
            return
        self._rebuild_folders_popover()
        if added:
            self._start_rescan([folder])

    def _remove_folder(self, folder: Path) -> None:
        self._index.remove_folder(folder)
        self._rebuild_folders_popover()
        self._reload_from_index()
        self._update_visible_stack()
        self._folders_popover.popdown()

    # -- Rescan + incremental load ----------------------------------------

    def _reload_from_index(self) -> None:
        """Полная перезаливка store из БД. Дёшево — без I/O самих фото."""
        self._store.remove_all()
        self._known_paths.clear()
        for entry in self._index.search(limit=20000):
            self._store.append(PhotoEntryItem(entry))
            self._known_paths.add(str(entry.path))
        self._update_send_btn()
        self._update_visible_stack()

    def _start_rescan(self, folders: list[Path] | None = None) -> None:
        if self._rescan_running:
            return
        if not self._index.folders():
            return
        self._rescan_running = True
        self._rescan_btn.set_sensitive(False)
        self._progress.set_visible(True)
        self._progress.set_fraction(0)
        self._progress.set_text(_("Scanning…"))
        self._progress.set_show_text(True)
        # Запускаем таймер-флешер
        self._batch_timer_id = GLib.timeout_add(BATCH_FLUSH_MS, self._flush_pending)

        def on_progress(p: ScanProgress) -> None:
            fraction = (p.processed / p.total) if p.total else 1.0
            GLib.idle_add(self._progress.set_fraction, fraction)
            GLib.idle_add(
                self._progress.set_text,
                f"{p.folder.name}: {p.processed}/{p.total}",
            )

        def on_entry(entry: PhotoEntry) -> None:
            with self._batch_lock:
                self._pending_entries.append(entry)

        def worker() -> None:
            try:
                self._index.rescan(
                    folders=folders, progress=on_progress, on_entry=on_entry
                )
            except Exception:  # noqa: BLE001 — не валим worker молча
                logger.exception("Rescan failed")
            finally:
                GLib.idle_add(self._on_rescan_done)

        threading.Thread(target=worker, daemon=True).start()

    def _flush_pending(self) -> bool:
        """Перелить накопленные записи из worker-треда в Gio.ListStore."""
        with self._batch_lock:
            batch = self._pending_entries
            self._pending_entries = []
        if batch:
            for entry in batch:
                key = str(entry.path)
                if key in self._known_paths:
                    continue  # обновление существующей записи — игнорируем,
                              # пересоберём в _on_rescan_done
                self._store.append(PhotoEntryItem(entry))
                self._known_paths.add(key)
            self._update_send_btn()
            self._update_visible_stack()
        return self._rescan_running  # продолжать, пока скан идёт

    def _on_rescan_done(self) -> bool:
        self._rescan_running = False
        if self._batch_timer_id is not None:
            # Финальный сброс на случай, если что-то осталось.
            self._flush_pending()
            GLib.source_remove(self._batch_timer_id)
            self._batch_timer_id = None
        self._rescan_btn.set_sensitive(True)
        self._progress.set_visible(False)
        # Полное обновление — забирает в т.ч. удалённые из индекса записи
        self._reload_from_index()
        return False

    # -- Public ------------------------------------------------------------

    def selected_paths(self) -> list[str]:
        return [str(p) for p in self._selected_paths()]
