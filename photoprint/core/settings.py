"""Persistent settings and named layout presets.

Settings live in ``~/.config/photoprint/settings.json`` (or the XDG-compliant
location chosen by :mod:`platformdirs`). Presets are stored alongside as
``presets.json``. Session state — a list of currently-loaded photo paths plus
the active layout params — is written to ``session.json`` on demand so the
user can resume their last batch.

All schemas are forward-compatible: unknown keys are ignored on read and the
default record is written back. Layout params are persisted as dicts and
materialised back into :class:`LayoutParams` via the planner enums.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from platformdirs import user_config_dir

from photoprint.core.layout import (
    PAPER_SIZES,
    BorderSpec,
    CaptionSource,
    CaptionSpec,
    FillOrder,
    FitMode,
    GridSpec,
    LayoutParams,
    Margins,
    Orientation,
    PaperSize,
)

logger = logging.getLogger(__name__)

APP_NAME = "photoprint"


def config_dir() -> Path:
    """Return the per-user config directory, creating it if missing."""
    p = Path(user_config_dir(APP_NAME))
    p.mkdir(parents=True, exist_ok=True)
    return p


# -- Settings ------------------------------------------------------------------


@dataclass
class AppSettings:
    """Top-level persistent settings."""

    last_printer: str = ""
    last_dir: str = ""
    last_layout: dict[str, Any] = field(default_factory=dict)
    # UI language: "en" (default), "ru", or "auto" (follow system locale).
    language: str = "en"


def load_settings() -> AppSettings:
    """Read settings.json, returning defaults on first run / corruption."""
    path = config_dir() / "settings.json"
    if not path.exists():
        return AppSettings()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Failed to read settings: %s — using defaults", exc)
        return AppSettings()
    return AppSettings(
        last_printer=raw.get("last_printer", ""),
        last_dir=raw.get("last_dir", ""),
        last_layout=raw.get("last_layout", {}),
        language=raw.get("language", "en"),
    )


def save_settings(s: AppSettings) -> None:
    """Write settings atomically to disk."""
    path = config_dir() / "settings.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(asdict(s), indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


# -- Presets -------------------------------------------------------------------


@dataclass
class Preset:
    """A named, saved set of layout parameters."""

    name: str
    layout: dict[str, Any]


def load_presets() -> list[Preset]:
    """Return all presets, or an empty list on first run."""
    path = config_dir() / "presets.json"
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Failed to read presets: %s", exc)
        return []
    return [Preset(name=item["name"], layout=item.get("layout", {})) for item in raw]


def save_presets(presets: list[Preset]) -> None:
    """Persist the full preset list."""
    path = config_dir() / "presets.json"
    data = [asdict(p) for p in presets]
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


# -- Session -------------------------------------------------------------------


@dataclass
class Session:
    """The in-progress batch the user can resume on the next launch."""

    photo_paths: list[str] = field(default_factory=list)
    layout: dict[str, Any] = field(default_factory=dict)


def session_path() -> Path:
    return config_dir() / "session.json"


def load_session() -> Session | None:
    """Read the most recent session, if any."""
    path = session_path()
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return Session(
        photo_paths=list(raw.get("photo_paths", [])),
        layout=raw.get("layout", {}),
    )


def save_session(s: Session) -> None:
    """Persist the current session (file paths + layout dict)."""
    tmp = session_path().with_suffix(".json.tmp")
    tmp.write_text(json.dumps(asdict(s), indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(session_path())


def clear_session() -> None:
    """Remove the session file (if it exists)."""
    p = session_path()
    if p.exists():
        p.unlink()


# -- (De)serialisation of LayoutParams ----------------------------------------


def layout_to_dict(p: LayoutParams) -> dict[str, Any]:
    """Convert :class:`LayoutParams` to a JSON-safe dict."""
    return {
        "paper": {"name": p.paper.name, "width_mm": p.paper.width_mm, "height_mm": p.paper.height_mm},
        "orientation": p.orientation.value,
        "grid": {"cols": p.grid.cols, "rows": p.grid.rows},
        "margins": asdict(p.margins),
        "gutter_mm": p.gutter_mm,
        "fit_mode": p.fit_mode.value,
        "order": p.order.value,
        "auto_rotate": p.auto_rotate,
        "border": {"width_pt": p.border.width_pt, "color_rgb": list(p.border.color_rgb)},
        "caption": {
            "source": p.caption.source.value,
            "font_size_pt": p.caption.font_size_pt,
            "reserved_mm": p.caption.reserved_mm,
        },
    }


def layout_from_dict(d: dict[str, Any]) -> LayoutParams:
    """Inverse of :func:`layout_to_dict` with sensible defaults for missing fields."""
    paper_d = d.get("paper") or {}
    paper = PaperSize(
        name=paper_d.get("name", "A4"),
        width_mm=paper_d.get("width_mm", PAPER_SIZES["A4"].width_mm),
        height_mm=paper_d.get("height_mm", PAPER_SIZES["A4"].height_mm),
    )
    grid_d = d.get("grid") or {}
    grid = GridSpec(cols=int(grid_d.get("cols", 2)), rows=int(grid_d.get("rows", 2)))
    margins_d = d.get("margins") or {}
    margins = Margins(**{f.name: float(margins_d.get(f.name, 5.0)) for f in fields(Margins)})
    border_d = d.get("border") or {}
    border = BorderSpec(
        width_pt=float(border_d.get("width_pt", 0.0)),
        color_rgb=tuple(border_d.get("color_rgb", (0.0, 0.0, 0.0))),  # type: ignore[arg-type]
    )
    cap_d = d.get("caption") or {}
    caption = CaptionSpec(
        source=CaptionSource(cap_d.get("source", CaptionSource.NONE.value)),
        font_size_pt=float(cap_d.get("font_size_pt", 8.0)),
        reserved_mm=float(cap_d.get("reserved_mm", 4.0)),
    )
    return LayoutParams(
        paper=paper,
        orientation=Orientation(d.get("orientation", Orientation.AUTO.value)),
        grid=grid,
        margins=margins,
        gutter_mm=float(d.get("gutter_mm", 2.0)),
        fit_mode=FitMode(d.get("fit_mode", FitMode.FIT.value)),
        order=FillOrder(d.get("order", FillOrder.ROW_MAJOR.value)),
        auto_rotate=bool(d.get("auto_rotate", True)),
        border=border,
        caption=caption,
    )
