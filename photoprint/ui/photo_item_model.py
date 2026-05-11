"""GObject-обёртка над :class:`PhotoEntry`.

Gtk4-модели (Gio.ListStore, Gtk.MultiSelection) умеют хранить только объекты
GObject, поэтому простой dataclass из core отдавать в них нельзя — оборачиваем.
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import GObject  # noqa: E402

from photoprint.core.photo_index import PhotoEntry  # noqa: E402


class PhotoEntryItem(GObject.Object):
    """Лёгкая обёртка вокруг :class:`PhotoEntry` для использования в Gio.ListStore."""

    __gtype_name__ = "PhotoPrintEntry"

    def __init__(self, entry: PhotoEntry) -> None:
        super().__init__()
        self._entry = entry

    @property
    def entry(self) -> PhotoEntry:
        return self._entry
