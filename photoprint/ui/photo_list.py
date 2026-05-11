"""Photo grid with drag-and-drop, reordering, and selection.

Each photo is a thumbnail tile in a :class:`Gtk.FlowBox`. The widget owns the
list of :class:`PhotoRef` objects and emits ``photos-changed`` whenever the
list changes (after add / remove / reorder).
"""

from __future__ import annotations

import logging
from pathlib import Path

import gi

from photoprint.i18n import gettext as _

gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, GLib, GObject, Gtk  # noqa: E402

from photoprint.core.image_loader import is_supported, read_metadata, thumbnail_bytes  # noqa: E402
from photoprint.core.layout import PhotoRef  # noqa: E402

logger = logging.getLogger(__name__)

THUMB_SIZE = 144


class PhotoTile(Gtk.Box):
    """One thumbnail in the grid."""

    def __init__(self, photo: PhotoRef) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.photo = photo
        self.set_size_request(THUMB_SIZE + 16, THUMB_SIZE + 40)
        self.add_css_class("card")
        self.set_margin_top(2)
        self.set_margin_bottom(2)
        self.set_margin_start(2)
        self.set_margin_end(2)

        try:
            png_bytes = thumbnail_bytes(photo.path, max_side=THUMB_SIZE)
            gbytes = GLib.Bytes.new(png_bytes)
            texture = Gdk.Texture.new_from_bytes(gbytes)
            picture = Gtk.Picture.new_for_paintable(texture)
        except (OSError, GLib.Error) as exc:
            logger.warning("Thumbnail failed for %s: %s", photo.path, exc)
            picture = Gtk.Picture()  # blank
        picture.set_size_request(THUMB_SIZE, THUMB_SIZE)
        picture.set_can_shrink(True)
        picture.set_content_fit(Gtk.ContentFit.CONTAIN)
        picture.set_margin_top(6)
        picture.set_margin_start(6)
        picture.set_margin_end(6)
        self.append(picture)

        label = Gtk.Label(label=photo.path.name)
        label.set_ellipsize(3)  # PANGO_ELLIPSIZE_END
        label.set_max_width_chars(18)
        label.add_css_class("caption")
        label.set_margin_bottom(4)
        self.append(label)


class PhotoListWidget(Gtk.Box):
    """The photo grid + a small toolbar (add / remove / clear)."""

    __gsignals__ = {
        "photos-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self._photos: list[PhotoRef] = []

        # -- Toolbar ------------------------------------------------------
        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        toolbar.set_margin_top(6)
        toolbar.set_margin_start(6)
        toolbar.set_margin_end(6)
        add_btn = Gtk.Button.new_with_label(_("Add photos…"))
        add_btn.add_css_class("suggested-action")
        add_btn.connect("clicked", self._on_add_clicked)
        toolbar.append(add_btn)
        self._remove_btn = Gtk.Button.new_with_label(_("Remove selected"))
        self._remove_btn.set_sensitive(False)
        self._remove_btn.connect("clicked", lambda *args: self.remove_selected())
        toolbar.append(self._remove_btn)
        clear_btn = Gtk.Button.new_with_label(_("Clear"))
        clear_btn.connect("clicked", lambda *args: self.clear())
        toolbar.append(clear_btn)
        self.append(toolbar)

        # -- FlowBox in scroller -----------------------------------------
        self._flowbox = Gtk.FlowBox()
        self._flowbox.set_valign(Gtk.Align.START)
        self._flowbox.set_max_children_per_line(20)
        self._flowbox.set_min_children_per_line(2)
        self._flowbox.set_row_spacing(4)
        self._flowbox.set_column_spacing(4)
        self._flowbox.set_selection_mode(Gtk.SelectionMode.MULTIPLE)
        self._flowbox.connect("selected-children-changed", self._on_selection_changed)
        scroller = Gtk.ScrolledWindow()
        scroller.set_child(self._flowbox)
        scroller.set_vexpand(True)
        scroller.set_hexpand(True)
        self.append(scroller)

        # -- Drop target --------------------------------------------------
        drop = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        drop.connect("drop", self._on_drop)
        self.add_controller(drop)

        # Empty-state hint
        self._empty_hint = Gtk.Label(
            label=_("Drag photos here, or click “Add photos…”")
        )
        self._empty_hint.add_css_class("dim-label")
        self._empty_hint.add_css_class("title-2")
        self._empty_hint.set_vexpand(True)
        self._empty_hint.set_valign(Gtk.Align.CENTER)
        self._flowbox.set_placeholder_for(self._empty_hint) if hasattr(
            self._flowbox, "set_placeholder_for"
        ) else None
        # Gtk4's FlowBox has no placeholder API — keep the label in toolbar area:
        # we attach it as a second child via an overlay if needed.

    # -- Public --------------------------------------------------------------

    @property
    def photos(self) -> list[PhotoRef]:
        return list(self._photos)

    def add_paths(self, paths: list[Path]) -> int:
        """Load files at ``paths`` and append them. Returns the count actually added."""
        added = 0
        for p in paths:
            if not is_supported(p):
                logger.info("Skipping unsupported file: %s", p)
                continue
            try:
                meta = read_metadata(p)
            except OSError as exc:
                logger.warning("Could not open %s: %s", p, exc)
                continue
            caption = (
                meta.exif_datetime.strftime("%Y-%m-%d") if meta.exif_datetime else ""
            )
            photo = PhotoRef(
                path=p, width_px=meta.width_px, height_px=meta.height_px, caption=caption
            )
            self._photos.append(photo)
            tile = PhotoTile(photo)
            self._flowbox.append(tile)
            added += 1
        if added:
            self.emit("photos-changed")
        return added

    def clear(self) -> None:
        """Remove every photo."""
        child = self._flowbox.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self._flowbox.remove(child)
            child = nxt
        self._photos.clear()
        self.emit("photos-changed")

    def remove_selected(self) -> None:
        """Drop the currently selected tiles."""
        selected = self._flowbox.get_selected_children()
        if not selected:
            return
        to_remove_idx = {child.get_index() for child in selected}
        # Remove from FlowBox highest-index first so subsequent indices stay valid
        for idx in sorted(to_remove_idx, reverse=True):
            child = self._flowbox.get_child_at_index(idx)
            if child is not None:
                self._flowbox.remove(child)
            del self._photos[idx]
        self.emit("photos-changed")
        self._remove_btn.set_sensitive(False)

    def select_all(self) -> None:
        self._flowbox.select_all()

    # -- Signal handlers -----------------------------------------------------

    def _on_selection_changed(self, _fb) -> None:
        any_selected = bool(self._flowbox.get_selected_children())
        self._remove_btn.set_sensitive(any_selected)

    def _on_add_clicked(self, _btn) -> None:
        dialog = Gtk.FileDialog()
        dialog.set_title(_("Add photos"))
        f = Gtk.FileFilter()
        f.set_name(_("Images"))
        for ext in ("jpg", "jpeg", "png", "tiff", "tif", "webp", "heic", "heif", "bmp"):
            f.add_pattern(f"*.{ext}")
            f.add_pattern(f"*.{ext.upper()}")
        filters = Gio_list([f])
        dialog.set_filters(filters)
        dialog.set_default_filter(f)
        dialog.open_multiple(self.get_root(), None, self._on_file_dialog_finish)

    def _on_file_dialog_finish(self, dialog, result):
        try:
            files = dialog.open_multiple_finish(result)
        except GLib.Error as exc:
            if "Dismissed" not in str(exc):
                logger.warning("File dialog error: %s", exc)
            return
        paths = []
        for i in range(files.get_n_items()):
            f = files.get_item(i)
            p = f.get_path()
            if p:
                paths.append(Path(p))
        if paths:
            self.add_paths(paths)

    def _on_drop(self, _target, value, _x, _y) -> bool:
        # value is a Gdk.FileList
        paths = []
        for f in value.get_files():
            p = f.get_path()
            if p:
                paths.append(Path(p))
        if paths:
            self.add_paths(paths)
            return True
        return False


def Gio_list(items: list) -> object:
    """Build a Gio.ListStore holding ``items`` (used for FileDialog filters)."""
    from gi.repository import Gio

    store = Gio.ListStore.new(items[0].__class__)
    for it in items:
        store.append(it)
    return store
