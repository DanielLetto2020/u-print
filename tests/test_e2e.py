"""End-to-end: planner + renderer + image_loader against real image files."""

from __future__ import annotations

from photoprint.core.image_loader import read_metadata
from photoprint.core.layout import (
    PAPER_SIZES,
    CaptionSource,
    CaptionSpec,
    FillOrder,
    FitMode,
    GridSpec,
    LayoutParams,
    Margins,
    Orientation,
    PhotoRef,
    plan_layout,
)
from photoprint.core.renderer import render_plan_to_pdf


def test_real_jpegs_through_full_pipeline(make_jpeg, tmp_path):
    paths = [
        make_jpeg("a.jpg", 800, 600, color=(180, 40, 40)),
        make_jpeg("b.jpg", 600, 800, color=(40, 180, 40)),
        make_jpeg("c.jpg", 1200, 900, color=(40, 40, 180)),
        make_jpeg("d.jpg", 900, 1200, color=(180, 180, 40)),
        make_jpeg("e.jpg", 1024, 768, color=(180, 80, 180)),
    ]
    photos = []
    for p in paths:
        m = read_metadata(p)
        photos.append(PhotoRef(path=p, width_px=m.width_px, height_px=m.height_px))

    params = LayoutParams(
        paper=PAPER_SIZES["A4"],
        orientation=Orientation.AUTO,
        grid=GridSpec(2, 2),
        margins=Margins(5, 5, 5, 5),
        gutter_mm=3.0,
        fit_mode=FitMode.FILL,
        order=FillOrder.ROW_MAJOR,
        auto_rotate=True,
        caption=CaptionSpec(source=CaptionSource.FILENAME, reserved_mm=5.0),
    )

    plan = plan_layout(photos, params)
    assert plan.page_count == 2  # 5 photos / 4 per page

    out = render_plan_to_pdf(plan, tmp_path / "e2e.pdf", dpi=150)
    data = out.read_bytes()
    assert data.startswith(b"%PDF-")
    # Sanity check: the PDF should be a few KB but not megabytes for these small JPEGs
    assert 1000 < len(data) < 5_000_000
