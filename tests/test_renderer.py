"""Tests for the PDF renderer."""

from __future__ import annotations

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
    PhotoRef,
    plan_layout,
)
from photoprint.core.renderer import render_plan_to_pdf


def _params(**overrides):
    base = dict(
        paper=PAPER_SIZES["A4"],
        orientation=Orientation.PORTRAIT,
        grid=GridSpec(2, 2),
        margins=Margins(5, 5, 5, 5),
        gutter_mm=2.0,
        fit_mode=FitMode.FIT,
        order=FillOrder.ROW_MAJOR,
        auto_rotate=False,
        border=BorderSpec(),
        caption=CaptionSpec(),
    )
    base.update(overrides)
    return LayoutParams(**base)


def test_renders_single_page_pdf(make_jpeg, tmp_path):
    path = make_jpeg("p.jpg")
    plan = plan_layout(
        [PhotoRef(path=path, width_px=800, height_px=600)],
        _params(grid=GridSpec(1, 1)),
    )
    out = render_plan_to_pdf(plan, tmp_path / "out.pdf", dpi=72)
    assert out.exists()
    head = out.read_bytes()[:5]
    assert head == b"%PDF-"


def test_multipage_pdf_has_expected_pages(make_jpeg, tmp_path):
    photos = [
        PhotoRef(path=make_jpeg(f"p{i}.jpg"), width_px=800, height_px=600) for i in range(5)
    ]
    plan = plan_layout(photos, _params(grid=GridSpec(2, 2)))
    out = render_plan_to_pdf(plan, tmp_path / "multi.pdf", dpi=72)
    # 5 photos at 4/page → 2 pages
    assert plan.page_count == 2
    data = out.read_bytes()
    # Crude but adequate: count "/Type /Page\n" markers in the PDF
    assert data.count(b"/Page\n") + data.count(b"/Page ") >= 2


def test_empty_plan_produces_valid_empty_pdf(tmp_path):
    plan = plan_layout([], _params())
    out = render_plan_to_pdf(plan, tmp_path / "empty.pdf", dpi=72)
    assert out.exists()
    assert out.read_bytes().startswith(b"%PDF-")


def test_fill_mode_renders_without_error(make_jpeg, tmp_path):
    # Tall photo into wide cell → significant crop
    path = make_jpeg("tall.jpg", width=400, height=1600)
    plan = plan_layout(
        [PhotoRef(path=path, width_px=400, height_px=1600)],
        _params(grid=GridSpec(1, 1), fit_mode=FitMode.FILL),
    )
    out = render_plan_to_pdf(plan, tmp_path / "fill.pdf", dpi=72)
    assert out.stat().st_size > 1000


def test_caption_text_embedded_in_pdf(make_jpeg, tmp_path):
    path = make_jpeg("named-photo.jpg", width=400, height=300)
    photo = PhotoRef(path=path, width_px=400, height_px=300)
    plain = render_plan_to_pdf(
        plan_layout([photo], _params(grid=GridSpec(1, 1))),
        tmp_path / "plain.pdf",
        dpi=72,
    )
    captioned = render_plan_to_pdf(
        plan_layout(
            [photo],
            _params(
                grid=GridSpec(1, 1),
                caption=CaptionSpec(source=CaptionSource.FILENAME, reserved_mm=5.0),
            ),
        ),
        tmp_path / "captioned.pdf",
        dpi=72,
    )
    # Captioned PDF must include extra text content; size strictly larger.
    assert captioned.stat().st_size > plain.stat().st_size
