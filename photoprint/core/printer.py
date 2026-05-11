"""Thin wrapper around :mod:`cups` (pycups).

Goals: keep the rest of the app pycups-free (so it can be mocked in tests),
and surface only the bits we need — list printers, send a PDF, expose common
options as keyword arguments.

Requires the system packages ``python3-cups`` and ``libcups2-dev`` (the latter
only to build pycups if installing via pip).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Final

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PrinterInfo:
    """A printer that CUPS knows about."""

    name: str        # CUPS queue name (used to submit jobs)
    display: str     # human-readable description for the UI
    is_default: bool


@dataclass(frozen=True)
class PrintOptions:
    """User-selectable options forwarded to CUPS."""

    copies: int = 1
    color_mode: str = "color"   # "color" | "mono"
    quality: str = "normal"     # "draft" | "normal" | "best"
    media: str | None = None    # optional CUPS media identifier (e.g. "A4")
    tray: str | None = None     # optional input slot

    def to_cups_options(self) -> dict[str, str]:
        """Map this dataclass to the string-string dict pycups expects."""
        opts: dict[str, str] = {"copies": str(max(1, int(self.copies)))}
        opts["ColorModel"] = "Gray" if self.color_mode == "mono" else "RGB"
        # The print-quality IPP attribute maps roughly to CUPS's "print-quality":
        # 3 = draft, 4 = normal, 5 = best.
        opts["print-quality"] = {"draft": "3", "normal": "4", "best": "5"}.get(
            self.quality, "4"
        )
        if self.media:
            opts["media"] = self.media
        if self.tray:
            opts["InputSlot"] = self.tray
        return opts


_CUPS_IMPORT_ERR: Final[str] = (
    "Cannot connect to CUPS — install the 'python3-cups' system package "
    "(and 'libcups2-dev' if building pycups from source)."
)


def _connection():  # pragma: no cover — exercised via integration only
    try:
        import cups  # type: ignore[import-untyped]
    except ImportError as e:
        raise RuntimeError(_CUPS_IMPORT_ERR) from e
    return cups.Connection()


def list_printers() -> list[PrinterInfo]:
    """Return all CUPS printers known to the local server.

    On failure (no CUPS, no printers) returns an empty list and logs a warning.
    """
    try:
        conn = _connection()
        printers = conn.getPrinters()
        default = conn.getDefault()
    except Exception as exc:  # noqa: BLE001 — pycups raises various errors
        logger.warning("CUPS unavailable: %s", exc)
        return []

    out: list[PrinterInfo] = []
    for name, info in printers.items():
        out.append(
            PrinterInfo(
                name=name,
                display=info.get("printer-info") or info.get("printer-make-and-model") or name,
                is_default=(name == default),
            )
        )
    out.sort(key=lambda p: (not p.is_default, p.display.lower()))
    return out


def print_pdf(printer: str, pdf_path: Path, options: PrintOptions, title: str = "PhotoPrint") -> int:
    """Submit ``pdf_path`` to ``printer``. Returns the CUPS job id.

    Args:
        printer: CUPS queue name from :func:`list_printers`.
        pdf_path: PDF file produced by the renderer.
        options: Print options to forward.
        title: Job title shown in the print queue.

    Raises:
        RuntimeError: If CUPS is not reachable or the job is rejected.
    """
    try:
        conn = _connection()
        job_id = conn.printFile(printer, str(pdf_path), title, options.to_cups_options())
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"Failed to submit print job: {exc}") from exc
    return int(job_id)
