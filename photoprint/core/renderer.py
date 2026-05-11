"""Render a :class:`~photoprint.core.layout.LayoutPlan` to a PDF.

The renderer is the only place that knows about reportlab and PDF coordinate
spaces. It takes images via :mod:`photoprint.core.image_loader` so that EXIF,
HEIC and downscaling are all handled in one place.

PDF y-axis is bottom-left origin; layout y-axis is top-left. Conversion:
``pdf_y = (page_h_mm - top_left_y_mm - height_mm) * mm_to_pt``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Final

from PIL import Image
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from photoprint.core.image_loader import downscale_for_dpi, load_image_oriented
from photoprint.core.layout import CellPlacement, LayoutPlan, Page

logger = logging.getLogger(__name__)

DEFAULT_DPI: Final[int] = 300
PREVIEW_DPI: Final[int] = 96


def render_plan_to_pdf(
    plan: LayoutPlan,
    out_path: Path,
    *,
    dpi: int = DEFAULT_DPI,
    image_cache: dict[Path, Image.Image] | None = None,
) -> Path:
    """Render ``plan`` to a PDF at ``out_path``.

    Args:
        plan: Layout plan produced by :func:`~photoprint.core.layout.plan_layout`.
        out_path: Destination PDF. Existing file will be overwritten.
        dpi: Target resolution for embedded images. Lower for preview, higher
            for print. Each photo is downscaled to no more than ``dpi`` at its
            printed size before being embedded.
        image_cache: Optional dict to reuse loaded source images across pages
            (the same photo may appear on more than one page in some layouts).

    Returns:
        The output path (echoed back for convenience).

    Raises:
        OSError: If any source image cannot be opened.
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if plan.page_count == 0:
        c = canvas.Canvas(str(out_path))
        c.setPageSize((10 * mm, 10 * mm))
        c.save()
        return out_path

    first = plan.pages[0]
    c = canvas.Canvas(str(out_path), pagesize=(first.width_mm * mm, first.height_mm * mm))

    cache: dict[Path, Image.Image] = image_cache if image_cache is not None else {}

    for page in plan.pages:
        c.setPageSize((page.width_mm * mm, page.height_mm * mm))
        for cell in page.cells:
            _draw_cell(c, page, cell, plan, dpi, cache)
        c.showPage()

    c.save()
    return out_path


def _draw_cell(
    c: canvas.Canvas,
    page: Page,
    cell: CellPlacement,
    plan: LayoutPlan,
    dpi: int,
    cache: dict[Path, Image.Image],
) -> None:
    """Draw a single cell — image, optional border, optional caption."""
    photo_path = cell.photo.path
    src = cache.get(photo_path)
    if src is None:
        src = load_image_oriented(photo_path)
        # Ensure RGB for predictable PDF embedding (RGBA also OK)
        if src.mode not in {"RGB", "RGBA"}:
            src = src.convert("RGBA" if "A" in src.mode else "RGB")
        cache[photo_path] = src

    img = _crop_and_rotate(src, cell)
    img = downscale_for_dpi(img, cell.img_w_mm, cell.img_h_mm, dpi=dpi)

    x_pt = cell.img_x_mm * mm
    y_pt = (page.height_mm - cell.img_y_mm - cell.img_h_mm) * mm
    w_pt = cell.img_w_mm * mm
    h_pt = cell.img_h_mm * mm

    c.drawImage(
        ImageReader(img),
        x_pt,
        y_pt,
        w_pt,
        h_pt,
        preserveAspectRatio=False,
        mask="auto",
    )

    border = plan.params.border
    if border.width_pt > 0:
        c.setStrokeColorRGB(*border.color_rgb)
        c.setLineWidth(border.width_pt)
        c.rect(x_pt, y_pt, w_pt, h_pt, stroke=1, fill=0)

    if cell.caption_text:
        cap = plan.params.caption
        c.setFont("Helvetica", cap.font_size_pt)
        c.setFillColorRGB(0, 0, 0)
        text_y_mm = page.height_mm - (cell.cell_y_mm + cell.cell_h_mm) + 1.0
        c.drawCentredString(
            (cell.cell_x_mm + cell.cell_w_mm / 2) * mm,
            text_y_mm * mm,
            cell.caption_text,
        )


def _crop_and_rotate(src: Image.Image, cell: CellPlacement) -> Image.Image:
    """Apply the cell's normalised source crop, then any 90° rotation."""
    w, h = src.size
    left = int(round(cell.crop_left * w))
    top = int(round(cell.crop_top * h))
    right = int(round(cell.crop_right * w))
    bottom = int(round(cell.crop_bottom * h))
    # Clamp to safety — rounding can produce tiny over/underflows
    left = max(0, min(left, w - 1))
    top = max(0, min(top, h - 1))
    right = max(left + 1, min(right, w))
    bottom = max(top + 1, min(bottom, h))

    img = src
    if (left, top, right, bottom) != (0, 0, w, h):
        img = src.crop((left, top, right, bottom))

    if cell.rotation_deg == 90:
        img = img.rotate(90, expand=True)
    elif cell.rotation_deg == 180:  # not currently produced by the planner
        img = img.rotate(180, expand=True)
    elif cell.rotation_deg == 270:
        img = img.rotate(270, expand=True)

    return img
