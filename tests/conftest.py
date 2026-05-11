"""Shared pytest fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image


@pytest.fixture
def make_jpeg(tmp_path: Path):
    """Return a factory that writes a solid-colour JPEG and returns its path."""

    def _make(name: str, width: int = 800, height: int = 600, color=(200, 100, 50)) -> Path:
        path = tmp_path / name
        Image.new("RGB", (width, height), color).save(path, "JPEG", quality=85)
        return path

    return _make
