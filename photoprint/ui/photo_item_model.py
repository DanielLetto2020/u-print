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


class DuplicateGroupItem(GObject.Object):
    """Группа из 2+ одинаковых по содержимому фото.

    Первая запись считается «оригиналом» — она первой попала в индекс
    (порядок гарантируется ``ORDER BY path`` в SQL). Остальные — копии.
    """

    __gtype_name__ = "PhotoPrintDuplicateGroup"

    def __init__(self, entries: list[PhotoEntry]) -> None:
        super().__init__()
        if len(entries) < 2:
            raise ValueError("DuplicateGroupItem requires at least 2 entries")
        self._entries = list(entries)

    @property
    def entries(self) -> list[PhotoEntry]:
        return list(self._entries)

    @property
    def representative(self) -> PhotoEntry:
        """Запись, картинку которой показываем в общей сетке дубликатов."""
        return self._entries[0]

    @property
    def copies_count(self) -> int:
        """Сколько всего экземпляров в группе (включая оригинал)."""
        return len(self._entries)
