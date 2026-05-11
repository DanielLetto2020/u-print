"""Индекс фотографий: список отслеживаемых папок + быстрый поиск.

Назначение модуля — не загружать пиксели и не строить миниатюры (это делает
:mod:`photoprint.core.image_loader`), а вести компактную метабазу: какие
файлы лежат в отслеживаемых папках, какие у них размеры, EXIF-дата, mtime.
Этим питается вкладка Search: при открытии приложения индекс уже готов,
никакой массовой повторной обработки.

Хранилище — SQLite в ``~/.config/photoprint/photo_index.db``. Сканирование
инкрементальное: для существующих записей сравниваем ``mtime`` и пересчитываем
только то, что изменилось. Файлы, которых на диске больше нет, удаляются.

API сознательно синхронный — UI зовёт через worker-поток, как удобно.
"""

from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from photoprint.core.image_loader import SUPPORTED_EXTENSIONS, read_metadata
from photoprint.core.settings import config_dir

logger = logging.getLogger(__name__)

DB_FILE = "photo_index.db"

# Схема. version_info() возвращает ту же версию, что в PRAGMA user_version.
SCHEMA_VERSION = 2

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS folders (
    path TEXT PRIMARY KEY,
    added_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS photos (
    path TEXT PRIMARY KEY,
    folder TEXT NOT NULL,
    name TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime INTEGER NOT NULL,
    width INTEGER,
    height INTEGER,
    exif_iso TEXT,
    indexed_at INTEGER NOT NULL,
    content_hash TEXT
);
CREATE INDEX IF NOT EXISTS idx_photos_folder ON photos(folder);
CREATE INDEX IF NOT EXISTS idx_photos_name ON photos(name);
CREATE INDEX IF NOT EXISTS idx_photos_exif ON photos(exif_iso);
CREATE INDEX IF NOT EXISTS idx_photos_size ON photos(size);
CREATE INDEX IF NOT EXISTS idx_photos_hash ON photos(content_hash);
"""

#: Чанк для потокового SHA-256 — 64 КБ. Хватает, не съедает память даже на DSLR-кадрах.
_HASH_CHUNK = 65536


@dataclass(frozen=True)
class PhotoEntry:
    """Одна строка из таблицы photos в удобной форме."""

    path: Path
    folder: Path
    name: str
    size: int
    mtime: int
    width: int | None
    height: int | None
    exif_datetime: datetime | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> PhotoEntry:
        exif_iso = row["exif_iso"]
        try:
            exif_dt = datetime.fromisoformat(exif_iso) if exif_iso else None
        except ValueError:
            exif_dt = None
        return cls(
            path=Path(row["path"]),
            folder=Path(row["folder"]),
            name=row["name"],
            size=row["size"],
            mtime=row["mtime"],
            width=row["width"],
            height=row["height"],
            exif_datetime=exif_dt,
        )


@dataclass(frozen=True)
class ScanProgress:
    """Снимок прогресса сканирования, отдаваемый в callback."""

    processed: int     # сколько файлов уже посмотрели на текущей папке
    total: int         # сколько в этой папке всего нашли
    folder: Path       # какая папка сейчас обрабатывается
    new: int           # добавлено новых записей
    updated: int       # обновлено существующих
    removed: int       # удалено из индекса


ProgressCallback = Callable[[ScanProgress], None]
EntryCallback = Callable[[PhotoEntry], None]
HashProgressCallback = Callable[[int, int], None]  # (done, total)


class PhotoIndex:
    """SQLite-обёртка над списком фото и их метаданных.

    Объект безопасно использовать только в одном потоке (как и обычный sqlite3
    cursor) — для UI это значит «работаем из worker-треда, в основной поток
    отдаём через GLib.idle_add».
    """

    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path or (config_dir() / DB_FILE)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: разрешаем дёргать из worker-треда (rescan)
        # и main-треда (search). UI следит, чтобы одновременных писателей не
        # было — кнопка rescan дизейблится на время прохода.
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA_SQL)
        # Миграция со схемы v1: добавляем content_hash + индекс, если нет.
        # Свежие БД получают это из CREATE TABLE выше, ALTER упадёт с
        # OperationalError для них — поглощаем.
        try:
            self._conn.execute("ALTER TABLE photos ADD COLUMN content_hash TEXT")
            self._conn.commit()
        except sqlite3.OperationalError:
            pass
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_photos_hash ON photos(content_hash)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_photos_size ON photos(size)"
        )
        self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self._conn.commit()

    # -- Folders -----------------------------------------------------------

    def add_folder(self, folder: Path) -> bool:
        """Добавить папку в список отслеживаемых. Идемпотентно."""
        folder = folder.resolve()
        if not folder.is_dir():
            raise NotADirectoryError(f"Not a directory: {folder}")
        try:
            self._conn.execute(
                "INSERT INTO folders(path, added_at) VALUES(?, ?)",
                (str(folder), int(datetime.now().timestamp())),
            )
            self._conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False  # уже есть

    def remove_folder(self, folder: Path) -> int:
        """Убрать папку и все её записи из индекса. Возвращает число удалённых фото."""
        folder = folder.resolve()
        cur = self._conn.execute("DELETE FROM photos WHERE folder = ?", (str(folder),))
        removed = cur.rowcount or 0
        self._conn.execute("DELETE FROM folders WHERE path = ?", (str(folder),))
        self._conn.commit()
        return removed

    def folders(self) -> list[Path]:
        """Все отслеживаемые папки, отсортировано по пути."""
        rows = self._conn.execute(
            "SELECT path FROM folders ORDER BY path"
        ).fetchall()
        return [Path(r["path"]) for r in rows]

    # -- Scanning ----------------------------------------------------------

    def rescan(
        self,
        folders: Iterable[Path] | None = None,
        progress: ProgressCallback | None = None,
        on_entry: EntryCallback | None = None,
    ) -> ScanProgress:
        """Пересканировать выбранные (или все известные) папки.

        Инкрементально:
          * новый файл — добавляется
          * существующий с прежним mtime — пропускается
          * существующий с изменённым mtime — обновляется
          * файл из индекса, которого больше нет — удаляется

        Args:
            folders: какие папки сканировать. ``None`` — все известные.
            progress: коллбек, дёргается после каждой обработанной папки
                и периодически внутри неё (раз в ~50 файлов).
            on_entry: коллбек, дёргается после каждой добавленной/обновлённой
                записи с готовым :class:`PhotoEntry`. UI пользуется этим, чтобы
                показывать фото по мере индексации, не дожидаясь конца прохода.

        Returns:
            Сводный :class:`ScanProgress` после прохода.
        """
        targets = list(folders) if folders is not None else self.folders()
        agg_new = agg_upd = agg_rem = 0
        last: ScanProgress = ScanProgress(0, 0, Path(), 0, 0, 0)

        for folder in targets:
            folder = folder.resolve()
            on_disk = _collect_supported_files(folder)
            existing = self._existing_in_folder(folder)

            total = len(on_disk)
            processed = 0
            new = upd = 0

            # Добавления и обновления
            for processed, (fp, st) in enumerate(on_disk.items(), start=1):
                size, mtime = st
                old = existing.pop(fp, None)
                changed = False
                if old is None and self._upsert(fp, folder, size, mtime):
                    new += 1
                    changed = True
                elif old is not None and old != (size, mtime) and self._upsert(
                    fp, folder, size, mtime
                ):
                    upd += 1
                    changed = True
                if changed and on_entry is not None:
                    entry = self._fetch_entry(fp)
                    if entry is not None:
                        on_entry(entry)
                if progress and processed % 50 == 0:
                    progress(
                        ScanProgress(processed, total, folder, new, upd, 0)
                    )

            # Удаления — то, что осталось в existing, исчезло с диска
            removed = 0
            for missing in existing:
                self._conn.execute("DELETE FROM photos WHERE path = ?", (str(missing),))
                removed += 1
            self._conn.commit()

            agg_new += new
            agg_upd += upd
            agg_rem += removed
            last = ScanProgress(total, total, folder, new, upd, removed)
            if progress:
                progress(last)

        return ScanProgress(
            last.processed,
            last.total,
            last.folder,
            agg_new,
            agg_upd,
            agg_rem,
        )

    def _fetch_entry(self, path: Path) -> PhotoEntry | None:
        """Считать одну запись из БД и собрать :class:`PhotoEntry`."""
        row = self._conn.execute(
            "SELECT * FROM photos WHERE path = ?", (str(path),)
        ).fetchone()
        return PhotoEntry.from_row(row) if row else None

    def _existing_in_folder(self, folder: Path) -> dict[Path, tuple[int, int]]:
        rows = self._conn.execute(
            "SELECT path, size, mtime FROM photos WHERE folder = ?",
            (str(folder),),
        ).fetchall()
        return {Path(r["path"]): (r["size"], r["mtime"]) for r in rows}

    def _upsert(self, path: Path, folder: Path, size: int, mtime: int) -> bool:
        """Считать метаданные и сохранить запись. ``False`` если файл не открылся."""
        try:
            meta = read_metadata(path)
        except OSError as exc:
            logger.warning("Skip %s: %s", path, exc)
            return False
        exif_iso = meta.exif_datetime.isoformat() if meta.exif_datetime else None
        # content_hash для новых/изменённых файлов сбрасываем — содержимое
        # могло поменяться, прежний SHA уже не валиден; перевычислим лениво
        # при следующем поиске дублей.
        self._conn.execute(
            """
            INSERT INTO photos(path, folder, name, size, mtime, width, height,
                               exif_iso, indexed_at, content_hash)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
            ON CONFLICT(path) DO UPDATE SET
              folder=excluded.folder,
              name=excluded.name,
              size=excluded.size,
              mtime=excluded.mtime,
              width=excluded.width,
              height=excluded.height,
              exif_iso=excluded.exif_iso,
              indexed_at=excluded.indexed_at,
              content_hash=NULL
            """,
            (
                str(path),
                str(folder),
                path.name,
                size,
                mtime,
                meta.width_px,
                meta.height_px,
                exif_iso,
                int(datetime.now().timestamp()),
            ),
        )
        return True

    # -- Queries -----------------------------------------------------------

    def count(self) -> int:
        """Сколько всего записей в индексе."""
        row = self._conn.execute("SELECT COUNT(*) AS n FROM photos").fetchone()
        return int(row["n"])

    def search(
        self,
        query: str = "",
        *,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        folder: Path | None = None,
        limit: int = 5000,
    ) -> list[PhotoEntry]:
        """Подобрать фото по подстроке в имени и/или диапазону EXIF-даты.

        Args:
            query: подстрока, регистронезависимая, ищется в ``name``.
            date_from, date_to: границы EXIF-даты (если есть). Записи без EXIF
                в диапазоны не попадают.
            folder: ограничение по папке.
            limit: максимум записей в ответе.
        """
        sql = ["SELECT * FROM photos WHERE 1=1"]
        params: list = []
        if query:
            sql.append("AND lower(name) LIKE ?")
            params.append(f"%{query.lower()}%")
        if folder is not None:
            sql.append("AND folder = ?")
            params.append(str(folder.resolve()))
        if date_from is not None:
            sql.append("AND exif_iso >= ?")
            params.append(date_from.isoformat())
        if date_to is not None:
            sql.append("AND exif_iso <= ?")
            params.append(date_to.isoformat())
        sql.append("ORDER BY exif_iso DESC, name ASC LIMIT ?")
        params.append(int(limit))
        rows = self._conn.execute(" ".join(sql), params).fetchall()
        return [PhotoEntry.from_row(r) for r in rows]

    # -- Duplicates --------------------------------------------------------

    def compute_missing_hashes(
        self, progress: HashProgressCallback | None = None
    ) -> int:
        """Посчитать SHA-256 для файлов, у которых есть «соседи по размеру».

        Файл-«одиночка» по размеру дубликатом быть не может, поэтому хешируем
        только тех, у кого есть кандидаты-собратья. Хеш складываем в БД.

        Args:
            progress: коллбек ``(сделано, всего)``, дёргается каждые 5 файлов.

        Returns:
            Сколько файлов в итоге было захешировано (может быть 0).
        """
        rows = self._conn.execute(
            """
            SELECT path FROM photos
            WHERE content_hash IS NULL
              AND size IN (
                  SELECT size FROM photos GROUP BY size HAVING COUNT(*) > 1
              )
            ORDER BY size, path
            """
        ).fetchall()
        total = len(rows)
        if total == 0:
            if progress:
                progress(0, 0)
            return 0
        for done, row in enumerate(rows, start=1):
            path = Path(row["path"])
            digest = _hash_file(path)
            if digest is not None:
                self._conn.execute(
                    "UPDATE photos SET content_hash = ? WHERE path = ?",
                    (digest, str(path)),
                )
            if progress and done % 5 == 0:
                progress(done, total)
        self._conn.commit()
        if progress:
            progress(total, total)
        return total

    def find_duplicates(self) -> list[list[PhotoEntry]]:
        """Группы по 2+ фото с одинаковым SHA-256.

        Возвращает список списков; каждый внутренний список отсортирован по
        пути (стабильный «оригинал — слева»). Сами группы упорядочены по
        уменьшающемуся количеству копий.
        """
        rows = self._conn.execute(
            """
            SELECT * FROM photos
            WHERE content_hash IS NOT NULL
              AND content_hash IN (
                  SELECT content_hash FROM photos
                  WHERE content_hash IS NOT NULL
                  GROUP BY content_hash HAVING COUNT(*) > 1
              )
            ORDER BY content_hash, path
            """
        ).fetchall()
        groups: dict[str, list[PhotoEntry]] = {}
        for row in rows:
            entry = PhotoEntry.from_row(row)
            groups.setdefault(row["content_hash"], []).append(entry)
        result = list(groups.values())
        result.sort(key=lambda g: (-len(g), g[0].name))
        return result

    def close(self) -> None:
        """Аккуратно закрыть соединение с SQLite."""
        self._conn.close()


def _hash_file(path: Path) -> str | None:
    """SHA-256 содержимого файла потоком, или ``None`` если файл не открылся."""
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(_HASH_CHUNK), b""):
                h.update(chunk)
    except OSError as exc:
        logger.warning("Hash failed for %s: %s", path, exc)
        return None
    return h.hexdigest()


def _collect_supported_files(folder: Path) -> dict[Path, tuple[int, int]]:
    """Найти все поддерживаемые фото рекурсивно. Возвращает path → (size, mtime_ns)."""
    found: dict[Path, tuple[int, int]] = {}
    for root, dirs, files in os.walk(folder):
        # Не лезем в скрытые папки — мусор из .Trash-1000, .git, .thumbnails
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in files:
            ext = os.path.splitext(name)[1].lower()
            if ext not in SUPPORTED_EXTENSIONS:
                continue
            p = Path(root) / name
            try:
                st = p.stat()
            except OSError:
                continue
            found[p] = (int(st.st_size), int(st.st_mtime_ns))
    return found
