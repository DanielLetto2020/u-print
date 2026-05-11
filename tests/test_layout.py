"""Tests for the pure-function layout planner."""

from __future__ import annotations

from pathlib import Path

import pytest

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
    PhotoRef,
    plan_layout,
)


def _photo(name: str, w: int = 4000, h: int = 3000) -> PhotoRef:
    return PhotoRef(path=Path(f"/tmp/{name}"), width_px=w, height_px=h)


def _params(**overrides) -> LayoutParams:
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


# -- Pagination ----------------------------------------------------------------


def test_empty_input_yields_no_pages():
    plan = plan_layout([], _params())
    assert plan.page_count == 0


def test_pagination_splits_photos_across_pages():
    photos = [_photo(f"p{i}.jpg") for i in range(9)]
    plan = plan_layout(photos, _params(grid=GridSpec(2, 2)))
    # 9 photos at 4 per page → 3 pages (last one half-empty)
    assert plan.page_count == 3
    assert len(plan.pages[0].cells) == 4
    assert len(plan.pages[1].cells) == 4
    assert len(plan.pages[2].cells) == 1


def test_single_photo_single_cell():
    plan = plan_layout([_photo("p.jpg")], _params(grid=GridSpec(1, 1)))
    assert plan.page_count == 1
    cell = plan.pages[0].cells[0]
    # cell occupies most of the page minus 5mm margins
    assert cell.cell_x_mm == pytest.approx(5.0)
    assert cell.cell_y_mm == pytest.approx(5.0)
    assert cell.cell_w_mm == pytest.approx(210 - 10)
    assert cell.cell_h_mm == pytest.approx(297 - 10)


# -- Fit modes -----------------------------------------------------------------


def test_fit_mode_letterboxes_inside_cell():
    plan = plan_layout(
        [_photo("p.jpg", 4000, 3000)],  # 4:3 photo
        _params(grid=GridSpec(1, 1), fit_mode=FitMode.FIT),
    )
    cell = plan.pages[0].cells[0]
    # cell aspect on A4 portrait minus 5mm margins ≈ 200/287 ≈ 0.697
    # photo aspect 1.333 > cell aspect → fills width, shorter height
    assert cell.img_w_mm == pytest.approx(cell.cell_w_mm)
    assert cell.img_h_mm < cell.cell_h_mm
    # no source crop
    assert (cell.crop_left, cell.crop_top, cell.crop_right, cell.crop_bottom) == (
        0.0,
        0.0,
        1.0,
        1.0,
    )
    # centred
    assert cell.img_x_mm == pytest.approx(cell.cell_x_mm)


def test_fill_mode_crops_to_cell_aspect():
    # tall 1:2 photo into a wide 2:1 cell — must crop top+bottom
    plan = plan_layout(
        [_photo("p.jpg", 1000, 2000)],
        _params(
            paper=PaperSize("X", 200, 100),
            orientation=Orientation.PORTRAIT,
            margins=Margins(0, 0, 0, 0),
            grid=GridSpec(1, 1),
            gutter_mm=0,
            fit_mode=FitMode.FILL,
        ),
    )
    cell = plan.pages[0].cells[0]
    # image rect equals cell rect
    assert cell.img_w_mm == pytest.approx(200)
    assert cell.img_h_mm == pytest.approx(100)
    # vertical crop (top/bottom) trimmed symmetrically
    assert cell.crop_left == 0.0
    assert cell.crop_right == 1.0
    assert cell.crop_top == pytest.approx(0.375)  # (2000-500)/2/2000
    assert cell.crop_bottom == pytest.approx(0.625)


def test_stretch_mode_fills_cell_no_crop():
    plan = plan_layout(
        [_photo("p.jpg", 100, 100)],
        _params(grid=GridSpec(1, 1), fit_mode=FitMode.STRETCH),
    )
    cell = plan.pages[0].cells[0]
    assert cell.img_w_mm == pytest.approx(cell.cell_w_mm)
    assert cell.img_h_mm == pytest.approx(cell.cell_h_mm)
    assert (cell.crop_left, cell.crop_top, cell.crop_right, cell.crop_bottom) == (
        0.0,
        0.0,
        1.0,
        1.0,
    )


# -- Orientation ---------------------------------------------------------------


def test_explicit_landscape_swaps_paper():
    plan = plan_layout(
        [_photo("p.jpg")],
        _params(orientation=Orientation.LANDSCAPE, grid=GridSpec(1, 1)),
    )
    page = plan.pages[0]
    assert page.width_mm == pytest.approx(297.0)
    assert page.height_mm == pytest.approx(210.0)


def test_auto_orientation_picks_landscape_for_landscape_photos():
    # 10× wide-landscape photos with a 1×1 grid → auto should go landscape
    photos = [_photo(f"p{i}.jpg", 4000, 2000) for i in range(10)]
    plan = plan_layout(
        photos,
        _params(orientation=Orientation.AUTO, grid=GridSpec(1, 1), auto_rotate=False),
    )
    assert plan.pages[0].width_mm > plan.pages[0].height_mm


# -- Auto-rotate ---------------------------------------------------------------


def test_auto_rotate_rotates_when_cell_aspect_inverted():
    # Landscape photo into a portrait cell (A4, 2×1 grid → 2 portrait cells)
    plan = plan_layout(
        [_photo("p.jpg", 4000, 3000)],
        _params(
            orientation=Orientation.PORTRAIT,
            grid=GridSpec(2, 1),
            auto_rotate=True,
        ),
    )
    assert plan.pages[0].cells[0].rotation_deg == 90


def test_auto_rotate_off_keeps_zero():
    plan = plan_layout(
        [_photo("p.jpg", 4000, 3000)],
        _params(
            orientation=Orientation.PORTRAIT,
            grid=GridSpec(2, 1),
            auto_rotate=False,
        ),
    )
    assert plan.pages[0].cells[0].rotation_deg == 0


# -- Ordering ------------------------------------------------------------------


def test_row_major_order():
    photos = [_photo(f"p{i}.jpg") for i in range(4)]
    plan = plan_layout(photos, _params(grid=GridSpec(2, 2), order=FillOrder.ROW_MAJOR))
    # cell 0 top-left, cell 1 top-right, cell 2 bottom-left, cell 3 bottom-right
    cells = plan.pages[0].cells
    assert cells[0].cell_x_mm < cells[1].cell_x_mm
    assert cells[0].cell_y_mm == cells[1].cell_y_mm
    assert cells[2].cell_y_mm > cells[0].cell_y_mm
    assert cells[2].cell_x_mm == cells[0].cell_x_mm


def test_column_major_order():
    photos = [_photo(f"p{i}.jpg") for i in range(4)]
    plan = plan_layout(
        photos, _params(grid=GridSpec(2, 2), order=FillOrder.COLUMN_MAJOR)
    )
    cells = plan.pages[0].cells
    # cell 0 top-left, cell 1 bottom-left (column-first)
    assert cells[0].cell_x_mm == cells[1].cell_x_mm
    assert cells[1].cell_y_mm > cells[0].cell_y_mm


# -- Margins / gutter ----------------------------------------------------------


def test_gutter_separates_cells():
    plan = plan_layout(
        [_photo("a.jpg"), _photo("b.jpg")],
        _params(grid=GridSpec(2, 1), gutter_mm=10.0, margins=Margins(0, 0, 0, 0)),
    )
    c0, c1 = plan.pages[0].cells
    # gap between cells must equal gutter
    assert c1.cell_x_mm - (c0.cell_x_mm + c0.cell_w_mm) == pytest.approx(10.0)


def test_oversized_margins_raise():
    with pytest.raises(ValueError):
        plan_layout(
            [_photo("a.jpg")],
            _params(margins=Margins(200, 200, 200, 200)),
        )


# -- Captions ------------------------------------------------------------------


def test_caption_text_from_filename():
    plan = plan_layout(
        [_photo("my-photo.jpg")],
        _params(
            grid=GridSpec(1, 1),
            caption=CaptionSpec(source=CaptionSource.FILENAME, reserved_mm=5.0),
        ),
    )
    cell = plan.pages[0].cells[0]
    assert cell.caption_text == "my-photo.jpg"
    # image area must be smaller than cell because caption reserves 5mm
    assert cell.cell_h_mm - cell.img_h_mm >= 5.0 - 1e-6
