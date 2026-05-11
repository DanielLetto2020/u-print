"""Тесты для PhotoIndex — sqlite-индекс фотографий."""

from __future__ import annotations

import time

import pytest

from photoprint.core.photo_index import PhotoIndex


@pytest.fixture
def index(tmp_path):
    """Свежий PhotoIndex с базой во временной директории."""
    db = tmp_path / "test.db"
    idx = PhotoIndex(db)
    yield idx
    idx.close()


def test_add_remove_folder(index, tmp_path):
    folder = tmp_path / "photos"
    folder.mkdir()
    assert index.add_folder(folder) is True
    assert index.folders() == [folder.resolve()]
    # повторное добавление идемпотентно
    assert index.add_folder(folder) is False
    assert index.remove_folder(folder) == 0
    assert index.folders() == []


def test_add_folder_rejects_nonexistent(index, tmp_path):
    with pytest.raises(NotADirectoryError):
        index.add_folder(tmp_path / "does-not-exist")


def test_rescan_finds_photos(index, tmp_path, make_jpeg):
    folder = tmp_path / "shoot"
    folder.mkdir()
    p1 = make_jpeg(str(folder / "a.jpg"))
    p2 = make_jpeg(str(folder / "b.jpg"))
    # вложенная папка
    (folder / "nested").mkdir()
    p3 = make_jpeg(str(folder / "nested" / "c.jpg"))
    # неподдерживаемый файл
    (folder / "ignore.txt").write_text("hello")
    # скрытая папка — пропускаем
    (folder / ".Trash").mkdir()
    make_jpeg(str(folder / ".Trash" / "x.jpg"))

    index.add_folder(folder)
    progress = index.rescan()

    assert progress.new == 3
    assert progress.updated == 0
    assert progress.removed == 0
    assert index.count() == 3

    names = sorted(p.name for p in index.search())
    assert names == ["a.jpg", "b.jpg", "c.jpg"]
    # подтверждаем что искомые пути в индексе
    paths = {p.path for p in index.search()}
    assert {p1.resolve(), p2.resolve(), p3.resolve()} == paths


def test_rescan_incremental_skips_unchanged(index, tmp_path, make_jpeg):
    folder = tmp_path / "shoot"
    folder.mkdir()
    make_jpeg(str(folder / "a.jpg"))
    index.add_folder(folder)
    first = index.rescan()
    assert first.new == 1

    # Второй прогон без изменений — ничего не должно добавиться/обновиться.
    second = index.rescan()
    assert second.new == 0
    assert second.updated == 0
    assert second.removed == 0


def test_rescan_picks_up_modified_file(index, tmp_path, make_jpeg):
    folder = tmp_path / "shoot"
    folder.mkdir()
    p = make_jpeg(str(folder / "a.jpg"))
    index.add_folder(folder)
    index.rescan()

    # Меняем mtime в будущее (имитируем редактирование) — sqlite сравнит и обновит.
    time.sleep(0.01)
    new_mtime = p.stat().st_mtime + 10
    import os

    os.utime(p, (new_mtime, new_mtime))
    progress = index.rescan()
    assert progress.updated == 1
    assert progress.new == 0


def test_rescan_removes_deleted_files(index, tmp_path, make_jpeg):
    folder = tmp_path / "shoot"
    folder.mkdir()
    p1 = make_jpeg(str(folder / "a.jpg"))
    make_jpeg(str(folder / "b.jpg"))
    index.add_folder(folder)
    index.rescan()
    assert index.count() == 2

    p1.unlink()
    progress = index.rescan()
    assert progress.removed == 1
    assert index.count() == 1


def test_search_by_name(index, tmp_path, make_jpeg):
    folder = tmp_path / "x"
    folder.mkdir()
    make_jpeg(str(folder / "beach.jpg"))
    make_jpeg(str(folder / "mountain.jpg"))
    make_jpeg(str(folder / "BEACH-2.jpg"))
    index.add_folder(folder)
    index.rescan()

    # регистронезависимо
    results = {p.name for p in index.search("beach")}
    assert results == {"beach.jpg", "BEACH-2.jpg"}


def test_search_filters_by_folder(index, tmp_path, make_jpeg):
    f1 = tmp_path / "f1"
    f2 = tmp_path / "f2"
    f1.mkdir()
    f2.mkdir()
    make_jpeg(str(f1 / "a.jpg"))
    make_jpeg(str(f2 / "b.jpg"))
    index.add_folder(f1)
    index.add_folder(f2)
    index.rescan()

    only_f1 = index.search(folder=f1)
    assert [p.name for p in only_f1] == ["a.jpg"]


def test_on_entry_fires_per_added_photo(index, tmp_path, make_jpeg):
    folder = tmp_path / "shoot"
    folder.mkdir()
    make_jpeg(str(folder / "a.jpg"))
    make_jpeg(str(folder / "b.jpg"))
    make_jpeg(str(folder / "c.jpg"))
    index.add_folder(folder)
    seen: list[str] = []
    index.rescan(on_entry=lambda e: seen.append(e.name))
    assert sorted(seen) == ["a.jpg", "b.jpg", "c.jpg"]

    # Повторный rescan без изменений — коллбек не дёргается
    seen.clear()
    index.rescan(on_entry=lambda e: seen.append(e.name))
    assert seen == []


def test_remove_folder_drops_its_photos(index, tmp_path, make_jpeg):
    folder = tmp_path / "shoot"
    folder.mkdir()
    make_jpeg(str(folder / "a.jpg"))
    make_jpeg(str(folder / "b.jpg"))
    index.add_folder(folder)
    index.rescan()
    assert index.count() == 2

    removed = index.remove_folder(folder)
    assert removed == 2
    assert index.count() == 0
    assert index.folders() == []
