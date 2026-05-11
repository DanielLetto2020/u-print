"""Вкладка Search: обозреватель проиндексированных фото.

Снаружи виджет показывает четыре блока:
* верхняя панель с управлением папками, кнопкой пересканирования, строкой
  поиска и тумблером режима отображения (grid/list);
* область результатов — :class:`Gtk.FlowBox` (миниатюры) или
  :class:`Gtk.ListBox` (компактный список с превью), мульти-выбор включён;
* пустые состояния (нет папок / нет совпадений);
* нижняя actionbar с кнопкой «Отправить N в Печать».

Все «тяжёлые» операции (rescan) уводятся в worker-поток, а обновления UI
доставляются через :func:`GLib.idle_add`. Индекс :class:`PhotoIndex` создаётся
один раз и шарится между потоками (см. ``check_same_thread=False``).
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, GLib, GObject, Gtk  # noqa: E402

from photoprint.core.photo_index import PhotoEntry, PhotoIndex, ScanProgress  # noqa: E402
from photoprint.i18n import gettext as _  # noqa: E402

logger = logging.getLogger(__name__)

THUMB_SIZE = 144      # px, для grid-режима
LIST_THUMB = 48       # px, для списка


def _human_size(size: int) -> str:
    """`12345` → `12.1 KB`. Для отображения в списке."""
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{size} B"
        size /= 1024
    return f"{size:.1f} GB"


def _make_thumb_picture(path: Path, max_side: int) -> Gtk.Picture:
    """Сделать виджет-миниатюру. Тихо отдаёт пустой Picture, если фото не открылось."""
    from photoprint.core.image_loader import thumbnail_bytes

    try:
        png = thumbnail_bytes(path, max_side=max_side)
        gbytes = GLib.Bytes.new(png)
        texture = Gdk.Texture.new_from_bytes(gbytes)
        pic = Gtk.Picture.new_for_paintable(texture)
    except (OSError, GLib.Error) as exc:
        logger.warning("Thumbnail failed for %s: %s", path, exc)
        pic = Gtk.Picture()
    pic.set_size_request(max_side, max_side)
    pic.set_can_shrink(True)
    pic.set_content_fit(Gtk.ContentFit.CONTAIN)
    return pic


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
        self._results: list[PhotoEntry] = []
        self._mode = "grid"      # "grid" или "list"
        self._rescan_running = False

        # -- Верхняя панель -----------------------------------------------
        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        toolbar.set_margin_top(8)
        toolbar.set_margin_start(8)
        toolbar.set_margin_end(8)
        toolbar.set_margin_bottom(6)

        # Менеджмент папок — кнопка с поповером
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

        # Search entry — основная фишка
        self._search = Gtk.SearchEntry()
        self._search.set_placeholder_text(_("Search by name…"))
        self._search.set_hexpand(True)
        self._search.connect("search-changed", lambda *_a: self._refresh_results())
        toolbar.append(self._search)

        # Тумблер режима отображения
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

        # Прогресс пересканирования
        self._progress = Gtk.ProgressBar()
        self._progress.set_visible(False)
        self._progress.set_margin_start(8)
        self._progress.set_margin_end(8)
        self.append(self._progress)

        # -- Контент: stack с пустыми состояниями и собственно результатами
        self._content_stack = Gtk.Stack()
        self._content_stack.set_vexpand(True)

        # Пустое состояние: нет папок
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

        # Пустое состояние: нет результатов
        empty_no_results = Adw.StatusPage()
        empty_no_results.set_icon_name("system-search-symbolic")
        empty_no_results.set_title(_("Nothing matches"))
        empty_no_results.set_description(
            _("Try a shorter query or rescan the folders.")
        )
        self._content_stack.add_named(empty_no_results, "no-results")

        # Сетка миниатюр
        self._flowbox = Gtk.FlowBox()
        self._flowbox.set_valign(Gtk.Align.START)
        self._flowbox.set_selection_mode(Gtk.SelectionMode.MULTIPLE)
        self._flowbox.set_row_spacing(4)
        self._flowbox.set_column_spacing(4)
        self._flowbox.set_max_children_per_line(30)
        self._flowbox.set_min_children_per_line(2)
        self._flowbox.connect(
            "selected-children-changed", lambda *_a: self._update_send_btn()
        )
        grid_scroller = Gtk.ScrolledWindow()
        grid_scroller.set_child(self._flowbox)
        grid_scroller.set_vexpand(True)
        self._content_stack.add_named(grid_scroller, "grid")

        # Список с компактным превью
        self._listbox = Gtk.ListBox()
        self._listbox.set_selection_mode(Gtk.SelectionMode.MULTIPLE)
        self._listbox.add_css_class("boxed-list")
        self._listbox.connect(
            "selected-rows-changed", lambda *_a: self._update_send_btn()
        )
        list_scroller = Gtk.ScrolledWindow()
        list_scroller.set_child(self._listbox)
        list_scroller.set_vexpand(True)
        self._content_stack.add_named(list_scroller, "list")

        self.append(self._content_stack)

        # -- Нижняя actionbar --------------------------------------------
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

        # Стартовое состояние
        self._refresh_results()

    # -- Folders -----------------------------------------------------------

    def _rebuild_folders_popover(self) -> None:
        """Пересобрать поповер со списком папок и кнопкой Add."""
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
        else:
            self._refresh_results()

    def _remove_folder(self, folder: Path) -> None:
        self._index.remove_folder(folder)
        self._rebuild_folders_popover()
        self._refresh_results()
        self._folders_popover.popdown()

    # -- Rescan ------------------------------------------------------------

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

        def worker() -> None:
            def on_progress(p: ScanProgress) -> None:
                fraction = (p.processed / p.total) if p.total else 1.0
                GLib.idle_add(self._progress.set_fraction, fraction)
                GLib.idle_add(
                    self._progress.set_text,
                    f"{p.folder.name}: {p.processed}/{p.total}",
                )

            try:
                self._index.rescan(folders=folders, progress=on_progress)
            except Exception:  # noqa: BLE001 — нельзя ронять worker молча
                logger.exception("Rescan failed")
            finally:
                GLib.idle_add(self._on_rescan_done)

        threading.Thread(target=worker, daemon=True).start()

    def _on_rescan_done(self) -> bool:
        self._rescan_running = False
        self._rescan_btn.set_sensitive(True)
        self._progress.set_visible(False)
        self._refresh_results()
        return False

    # -- View mode + results ----------------------------------------------

    def _on_view_toggle(self, button: Gtk.ToggleButton) -> None:
        if not button.get_active():
            return
        self._mode = "grid" if button is self._grid_btn else "list"
        self._update_visible_stack()

    def _update_visible_stack(self) -> None:
        if not self._index.folders():
            self._content_stack.set_visible_child_name("no-folders")
            return
        if not self._results:
            self._content_stack.set_visible_child_name("no-results")
            return
        self._content_stack.set_visible_child_name(self._mode)

    def _refresh_results(self) -> None:
        query = self._search.get_text().strip()
        self._results = (
            self._index.search(query=query, limit=2000) if self._index.folders() else []
        )
        self._populate()
        total_indexed = self._index.count()
        self._count_label.set_text(
            _("Showing {shown} of {total}").format(
                shown=len(self._results), total=total_indexed
            )
        )
        self._update_visible_stack()
        self._update_send_btn()

    def _populate(self) -> None:
        # Очистка
        self._clear_container(self._flowbox)
        self._clear_container(self._listbox)
        # Заполнение
        for entry in self._results:
            self._flowbox.append(self._make_grid_tile(entry))
            self._listbox.append(self._make_list_row(entry))

    @staticmethod
    def _clear_container(c: Gtk.Widget) -> None:
        child = c.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            c.remove(child)
            child = nxt

    def _make_grid_tile(self, entry: PhotoEntry) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.add_css_class("card")
        box.set_margin_top(2)
        box.set_margin_bottom(2)
        box.set_margin_start(2)
        box.set_margin_end(2)
        pic = _make_thumb_picture(entry.path, THUMB_SIZE)
        pic.set_margin_top(6)
        pic.set_margin_start(6)
        pic.set_margin_end(6)
        box.append(pic)
        label = Gtk.Label(label=entry.name)
        label.set_ellipsize(3)
        label.set_max_width_chars(18)
        label.add_css_class("caption")
        label.set_margin_bottom(4)
        box.append(label)
        # сохраним путь в Python-атрибуте для последующего сбора selection
        box._photo_path = entry.path  # type: ignore[attr-defined]
        return box

    def _make_list_row(self, entry: PhotoEntry) -> Gtk.Widget:
        row = Adw.ActionRow()
        row.set_title(entry.name)
        meta_bits = [str(entry.folder.name) or str(entry.folder)]
        if entry.exif_datetime:
            meta_bits.append(entry.exif_datetime.strftime("%Y-%m-%d"))
        meta_bits.append(_human_size(entry.size))
        if entry.width and entry.height:
            meta_bits.append(f"{entry.width}×{entry.height}")
        row.set_subtitle("   ·   ".join(meta_bits))
        pic = _make_thumb_picture(entry.path, LIST_THUMB)
        pic.set_margin_top(4)
        pic.set_margin_bottom(4)
        row.add_prefix(pic)
        # сохраним путь — ListBox оборачивает row в ListBoxRow, путь возьмём с child
        row._photo_path = entry.path  # type: ignore[attr-defined]
        return row

    # -- Selection ---------------------------------------------------------

    def _selected_paths(self) -> list[Path]:
        if self._mode == "grid":
            out: list[Path] = []
            for child in self._flowbox.get_selected_children():
                tile = child.get_child()
                p = getattr(tile, "_photo_path", None)
                if p is not None:
                    out.append(p)
            return out
        out = []
        for row in self._listbox.get_selected_rows():
            inner = row.get_child()
            p = getattr(inner, "_photo_path", None)
            if p is not None:
                out.append(p)
        return out

    def _select_all(self) -> None:
        if self._mode == "grid":
            self._flowbox.select_all()
        else:
            self._listbox.select_all()

    def _update_send_btn(self) -> None:
        n = len(self._selected_paths())
        self._send_btn.set_sensitive(n > 0)
        if n > 0:
            self._send_btn.set_label(
                _("Send {n} to Print").format(n=n)
            )
        else:
            self._send_btn.set_label(_("Send to Print"))

    def _emit_send(self) -> None:
        paths = [str(p) for p in self._selected_paths()]
        if paths:
            self.emit("send-to-print", paths)

    # -- Public ------------------------------------------------------------

    def selected_paths(self) -> list[str]:
        return [str(p) for p in self._selected_paths()]
