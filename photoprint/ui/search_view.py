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

        self.append(toolbar)

        # -- Прогресс-бар скана --------------------------------------------
        self._progress = Gtk.ProgressBar()
        self._progress.set_visible(False)
        self._progress.set_margin_start(8)
        self._progress.set_margin_end(8)
        self.append(self._progress)

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

        # ColumnView (виртуальный, базовый — без настраиваемых колонок пока)
        self._columnview = Gtk.ColumnView.new(self._selection)
        self._columnview.set_show_column_separators(True)
        self._columnview.set_show_row_separators(True)
        self._build_basic_columns()
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

    def _build_basic_columns(self) -> None:
        """Стартовые колонки для ColumnView (full-fledged настройка — отдельным коммитом)."""
        # Превью
        thumb_factory = Gtk.SignalListItemFactory()
        thumb_factory.connect("setup", self._thumb_cell_setup)
        thumb_factory.connect("bind", self._thumb_cell_bind)
        thumb_col = Gtk.ColumnViewColumn.new(_("Preview"), thumb_factory)
        thumb_col.set_fixed_width(LIST_THUMB + 16)
        self._columnview.append_column(thumb_col)

        # Имя
        name_factory = Gtk.SignalListItemFactory()
        name_factory.connect("setup", lambda _f, li: li.set_child(self._make_label()))
        name_factory.connect("bind", lambda _f, li: li.get_child().set_text(
            li.get_item().entry.name
        ))
        name_col = Gtk.ColumnViewColumn.new(_("Name"), name_factory)
        name_col.set_expand(True)
        self._columnview.append_column(name_col)

        # Дата EXIF
        date_factory = Gtk.SignalListItemFactory()
        date_factory.connect("setup", lambda _f, li: li.set_child(self._make_label()))
        date_factory.connect("bind", lambda _f, li: li.get_child().set_text(
            li.get_item().entry.exif_datetime.strftime("%Y-%m-%d %H:%M")
            if li.get_item().entry.exif_datetime else ""
        ))
        date_col = Gtk.ColumnViewColumn.new(_("Date"), date_factory)
        date_col.set_fixed_width(160)
        self._columnview.append_column(date_col)

        # Размер
        size_factory = Gtk.SignalListItemFactory()
        size_factory.connect("setup", lambda _f, li: li.set_child(self._make_label()))
        size_factory.connect("bind", lambda _f, li: li.get_child().set_text(
            _human_size(li.get_item().entry.size)
        ))
        size_col = Gtk.ColumnViewColumn.new(_("Size"), size_factory)
        size_col.set_fixed_width(100)
        self._columnview.append_column(size_col)

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
        self._update_send_btn()

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
