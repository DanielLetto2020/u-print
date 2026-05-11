"""Search tab — заглушка.

Здесь будет полноценный обозреватель фото:
* список отслеживаемых папок (добавить/удалить/пересканировать)
* индекс на SQLite (см. :mod:`photoprint.core.photo_index`)
* строка поиска по имени + фильтр по дате EXIF
* переключатель «сетка миниатюр / список»
* мульти-выделение + кнопка «Отправить в Печать»

Сейчас рендерим только статичный плейсхолдер, чтобы вкладка появилась в шапке.
"""

from __future__ import annotations

import logging

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GObject, Gtk  # noqa: E402

from photoprint.i18n import gettext as _  # noqa: E402

logger = logging.getLogger(__name__)


class SearchView(Gtk.Box):
    """Содержимое вкладки Search.

    Эмитит сигнал ``send-to-print`` со списком строковых путей файлов,
    выбранных пользователем для печати.
    """

    __gsignals__ = {
        # один аргумент — Python-list путей; передаём как PyObject
        "send-to-print": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
    }

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.set_hexpand(True)
        self.set_vexpand(True)

        status = Adw.StatusPage()
        status.set_icon_name("system-search-symbolic")
        status.set_title(_("Search photos"))
        status.set_description(
            _(
                "Configure folders to index, then search by name or EXIF date "
                "and send the selection to the Print tab. Coming next."
            )
        )
        status.set_vexpand(True)
        self.append(status)

    def selected_paths(self) -> list[str]:
        """Заглушка — пока возвращает пустой список."""
        return []
