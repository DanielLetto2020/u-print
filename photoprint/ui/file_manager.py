"""Открыть файл в системном проводнике с подсветкой.

Сначала пробуем D-Bus-интерфейс ``org.freedesktop.FileManager1.ShowItems`` —
этот метод реализуют Nautilus, Files, Nemo, Caja и т.д. Он открывает папку
и выделяет в ней наш файл. Если шины нет или интерфейс не отвечает —
падаем в ``xdg-open`` на родительскую директорию (без подсветки самого
файла, но папка хотя бы откроется).
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gio, GLib  # noqa: E402

logger = logging.getLogger(__name__)


def open_in_file_manager(path: Path) -> None:
    """Открыть проводник с выделением ``path``; не блокирует main-тред."""
    uri = path.as_uri()
    try:
        connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        connection.call_sync(
            "org.freedesktop.FileManager1",
            "/org/freedesktop/FileManager1",
            "org.freedesktop.FileManager1",
            "ShowItems",
            GLib.Variant("(ass)", ([uri], "")),
            None,
            Gio.DBusCallFlags.NONE,
            -1,
            None,
        )
        return
    except GLib.Error as exc:
        logger.info(
            "FileManager1.ShowItems failed (%s), falling back to xdg-open", exc
        )
    # Fallback: открываем родительский каталог.
    try:
        subprocess.Popen(["xdg-open", str(path.parent)])  # noqa: S603,S607
    except OSError as exc:
        logger.warning("xdg-open failed for %s: %s", path.parent, exc)
