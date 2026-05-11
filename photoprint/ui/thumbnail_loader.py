"""Ленивый загрузчик миниатюр через пул потоков.

Принцип работы:
* UI-виджеты при биндинге не блокируют main-тред чтением PNG и операциями
  PIL — они дают этому модулю путь и максимальный размер плюс callback;
* фактическое декодирование идёт в :class:`concurrent.futures.ThreadPoolExecutor`,
  готовый :class:`Gdk.Texture` доставляется в main-тред через
  :func:`GLib.idle_add` и попадает в callback;
* результаты кешируются в LRU-словаре, чтобы повторный binding (например,
  при скролле туда-сюда) не пересчитывал картинку заново.

Один экземпляр на всё приложение хранится через :func:`get_default`.
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gdk, GLib  # noqa: E402

from photoprint.core.image_loader import thumbnail_bytes  # noqa: E402

logger = logging.getLogger(__name__)

#: Ключ кеша — (абсолютный путь, размер стороны в пикселях).
_Key = tuple[str, int]
#: Callback получает либо готовую текстуру, либо ``None`` (декодер не справился).
_Callback = Callable[["Gdk.Texture | None"], None]


class ThumbnailLoader:
    """Простой пул + LRU-кеш текстур.

    Не привязан к GTK-виджетам — рендерящие виджеты сами решают, что делать с
    результатом. Если для одного и того же ключа уже выполняется загрузка,
    запрос подписывается на тот же future вместо дублирования работы.
    """

    def __init__(self, *, max_workers: int = 4, cache_size: int = 600) -> None:
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="photoprint-thumb"
        )
        self._cache_size = cache_size
        self._cache: OrderedDict[_Key, Gdk.Texture] = OrderedDict()
        self._pending: dict[_Key, list[_Callback]] = {}
        self._lock = threading.Lock()

    def get(self, path: Path, size: int, callback: _Callback) -> None:
        """Запросить миниатюру для ``path`` стороной ``size`` пикселей.

        ``callback`` всегда вызывается в main-треде. Если миниатюра уже в
        кеше — :func:`GLib.idle_add` всё равно используется, чтобы поведение
        было одинаковым (callback не отрабатывает синхронно из ``get``).
        """
        key = (str(path), size)
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                # сохраним порядок LRU
                self._cache.move_to_end(key)
                GLib.idle_add(callback, cached)
                return
            if key in self._pending:
                self._pending[key].append(callback)
                return
            self._pending[key] = [callback]
        self._executor.submit(self._load_in_worker, path, size, key)

    def shutdown(self) -> None:
        """Дождаться завершения активных задач и убить пул (для тестов)."""
        self._executor.shutdown(wait=False, cancel_futures=True)

    # -- internal ----------------------------------------------------------

    def _load_in_worker(self, path: Path, size: int, key: _Key) -> None:
        try:
            png = thumbnail_bytes(path, max_side=size)
        except Exception as exc:  # noqa: BLE001 — PIL и I/O бросают разное
            logger.warning("Thumbnail %s failed: %s", path, exc)
            png = None
        # Текстуру строим в main-треде на всякий случай: Gdk.Texture сам по
        # себе CPU-резидентный, но создание через GLib.Bytes безопаснее не
        # дёргать одновременно с GTK-итерациями.
        GLib.idle_add(self._dispatch, key, png)

    def _dispatch(self, key: _Key, png: bytes | None) -> bool:
        texture: Gdk.Texture | None = None
        if png:
            try:
                texture = Gdk.Texture.new_from_bytes(GLib.Bytes.new(png))
            except GLib.Error as exc:
                logger.warning("Decode texture for %s: %s", key[0], exc)
        with self._lock:
            if texture is not None:
                self._cache[key] = texture
                self._cache.move_to_end(key)
                while len(self._cache) > self._cache_size:
                    self._cache.popitem(last=False)
            callbacks = self._pending.pop(key, [])
        for cb in callbacks:
            try:
                cb(texture)
            except Exception:  # noqa: BLE001 — один кривой callback не валит остальных
                logger.exception("Thumbnail callback raised")
        return False  # one-shot


_default: ThumbnailLoader | None = None


def get_default() -> ThumbnailLoader:
    """Лениво создать общий экземпляр и вернуть его."""
    global _default
    if _default is None:
        _default = ThumbnailLoader()
    return _default
