"""Pure-function layout planner.

This module is intentionally free of GUI, image-IO, and PDF dependencies so it
can be unit-tested in isolation. All sizes are in millimetres with a top-left
origin (X right, Y down) — the renderer is responsible for converting to PDF's
bottom-left coordinate system.

The entry point is :func:`plan_layout`, which turns a list of :class:`PhotoRef`
plus a :class:`LayoutParams` into a :class:`LayoutPlan` of pages and cells.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

# -- Enums ---------------------------------------------------------------------


class Orientation(str, Enum):
    """Paper orientation."""

    PORTRAIT = "portrait"
    LANDSCAPE = "landscape"
    AUTO = "auto"


class FitMode(str, Enum):
    """How a photo is placed inside its grid cell."""

    FIT = "fit"          # contain — whole photo visible, possibly with whitespace
    FILL = "fill"        # cover  — crop to fill the cell, no whitespace
    STRETCH = "stretch"  # distort to fit exactly


class FillOrder(str, Enum):
    """Order in which photos populate cells of a single page."""

    ROW_MAJOR = "row_major"        # left→right, then top→bottom
    COLUMN_MAJOR = "column_major"  # top→bottom, then left→right


class CaptionSource(str, Enum):
    """What text to use for the optional per-photo caption."""

    NONE = "none"
    FILENAME = "filename"
    EXIF_DATE = "exif_date"


# -- Data classes --------------------------------------------------------------


@dataclass(frozen=True)
class PaperSize:
    """Sheet dimensions in millimetres in portrait orientation."""

    name: str
    width_mm: float
    height_mm: float

    def with_orientation(self, orientation: Orientation, auto_landscape: bool) -> PaperSize:
        """Return a copy oriented per ``orientation``.

        For :attr:`Orientation.AUTO`, ``auto_landscape`` decides whether to swap.
        """
        if orientation is Orientation.LANDSCAPE or (
            orientation is Orientation.AUTO and auto_landscape
        ):
            return PaperSize(self.name, self.height_mm, self.width_mm)
        return self


@dataclass(frozen=True)
class Margins:
    """Page margins in millimetres."""

    top: float = 5.0
    right: float = 5.0
    bottom: float = 5.0
    left: float = 5.0


@dataclass(frozen=True)
class GridSpec:
    """Grid of cells per page (``cols`` columns × ``rows`` rows)."""

    cols: int
    rows: int

    def __post_init__(self) -> None:
        if self.cols < 1 or self.rows < 1:
            raise ValueError(f"Grid must be >=1×1, got {self.cols}×{self.rows}")

    @property
    def per_page(self) -> int:
        return self.cols * self.rows


@dataclass(frozen=True)
class BorderSpec:
    """Border drawn around each photo (not each cell)."""

    width_pt: float = 0.0
    color_rgb: tuple[float, float, float] = (0.0, 0.0, 0.0)


@dataclass(frozen=True)
class CaptionSpec:
    """Optional caption rendered below the photo within the cell."""

    source: CaptionSource = CaptionSource.NONE
    font_size_pt: float = 8.0
    reserved_mm: float = 4.0  # vertical space taken from the cell for the caption


@dataclass(frozen=True)
class LayoutParams:
    """Complete set of parameters required to plan a layout."""

    paper: PaperSize
    orientation: Orientation = Orientation.AUTO
    grid: GridSpec = field(default_factory=lambda: GridSpec(1, 1))
    margins: Margins = field(default_factory=Margins)
    gutter_mm: float = 2.0
    fit_mode: FitMode = FitMode.FIT
    order: FillOrder = FillOrder.ROW_MAJOR
    auto_rotate: bool = True
    border: BorderSpec = field(default_factory=BorderSpec)
    caption: CaptionSpec = field(default_factory=CaptionSpec)


@dataclass(frozen=True)
class PhotoRef:
    """A single source photo. Dimensions must already be EXIF-corrected."""

    path: Path
    width_px: int
    height_px: int
    caption: str = ""

    @property
    def aspect(self) -> float:
        return self.width_px / self.height_px if self.height_px else 1.0


@dataclass(frozen=True)
class CellPlacement:
    """Placement of one photo on the page.

    ``cell_*`` is the full grid cell (incl. any reserved caption strip);
    ``img_*`` is the rectangle the rendered (possibly cropped) photo occupies;
    ``crop_*`` is the source crop in normalized coords (0..1) — only ``FILL``
    mode produces values outside (0, 0, 1, 1); ``rotation_deg`` is 0 or 90 and
    is applied *after* the source crop.
    """

    cell_x_mm: float
    cell_y_mm: float
    cell_w_mm: float
    cell_h_mm: float
    photo: PhotoRef
    img_x_mm: float
    img_y_mm: float
    img_w_mm: float
    img_h_mm: float
    crop_left: float
    crop_top: float
    crop_right: float
    crop_bottom: float
    rotation_deg: int  # 0 or 90
    caption_text: str = ""


@dataclass(frozen=True)
class Page:
    """One physical sheet's worth of placed cells."""

    width_mm: float
    height_mm: float
    cells: tuple[CellPlacement, ...]


@dataclass(frozen=True)
class LayoutPlan:
    """Output of :func:`plan_layout` — all sheets to print."""

    params: LayoutParams
    pages: tuple[Page, ...]

    @property
    def page_count(self) -> int:
        return len(self.pages)


# -- Built-in paper sizes ------------------------------------------------------

PAPER_SIZES: dict[str, PaperSize] = {
    "A3": PaperSize("A3", 297.0, 420.0),
    "A4": PaperSize("A4", 210.0, 297.0),
    "A5": PaperSize("A5", 148.0, 210.0),
    "Letter": PaperSize("Letter", 215.9, 279.4),
    "Legal": PaperSize("Legal", 215.9, 355.6),
    "10x15": PaperSize("10×15 cm", 100.0, 150.0),
    "13x18": PaperSize("13×18 cm", 130.0, 180.0),
    "20x30": PaperSize("20×30 cm", 200.0, 300.0),
}

# Common grids, indexed by total cells. Each entry is a list of (cols, rows)
# variants to offer in the UI.
GRID_PRESETS: dict[int, tuple[GridSpec, ...]] = {
    1: (GridSpec(1, 1),),
    2: (GridSpec(1, 2), GridSpec(2, 1)),
    4: (GridSpec(2, 2),),
    6: (GridSpec(2, 3), GridSpec(3, 2)),
    8: (GridSpec(2, 4), GridSpec(4, 2)),
    9: (GridSpec(3, 3),),
    16: (GridSpec(4, 4),),
}


# -- Planner -------------------------------------------------------------------


def _auto_landscape(
    photos: list[PhotoRef], grid: GridSpec, paper: PaperSize, auto_rotate: bool
) -> bool:
    """Decide whether AUTO orientation should turn the paper landscape.

    Picks the orientation that maximises the mean fit-fraction of photos in
    the chosen grid. When ``auto_rotate`` is True, each photo's score is the
    better of its upright and rotated fits; otherwise only upright counts.
    """
    if not photos:
        return paper.width_mm > paper.height_mm  # already landscape paper → keep

    def cell_aspect(landscape: bool) -> float:
        w = paper.height_mm if landscape else paper.width_mm
        h = paper.width_mm if landscape else paper.height_mm
        return (w / grid.cols) / (h / grid.rows)

    def score(landscape: bool) -> float:
        ca = cell_aspect(landscape)
        total = 0.0
        for p in photos:
            pa = p.aspect
            upright = min(pa / ca, ca / pa)
            if auto_rotate:
                rotated = min((1 / pa) / ca, ca / (1 / pa))
                total += max(upright, rotated)
            else:
                total += upright
        return total / len(photos)

    return score(landscape=True) > score(landscape=False)


def _cells_in_order(grid: GridSpec, order: FillOrder) -> list[tuple[int, int]]:
    """Yield (col, row) tuples in the requested fill order."""
    if order is FillOrder.ROW_MAJOR:
        return [(c, r) for r in range(grid.rows) for c in range(grid.cols)]
    return [(c, r) for c in range(grid.cols) for r in range(grid.rows)]


def _should_rotate(photo_aspect: float, cell_aspect: float) -> bool:
    """Return True iff rotating the photo 90° improves the fit-fraction."""
    upright = min(photo_aspect / cell_aspect, cell_aspect / photo_aspect)
    rotated = min((1 / photo_aspect) / cell_aspect, cell_aspect / (1 / photo_aspect))
    return rotated > upright


def _place_in_cell(
    cell_w: float,
    cell_h: float,
    photo: PhotoRef,
    fit_mode: FitMode,
    rotation: int,
) -> tuple[float, float, float, float, float, float, float, float]:
    """Resolve image rectangle inside cell and source crop.

    Returns ``(img_x, img_y, img_w, img_h, crop_l, crop_t, crop_r, crop_b)``
    where img_x/y are offsets within the cell (top-left origin) and crops are
    normalized to the *source* photo.
    """
    # Effective photo aspect after rotation
    pw = photo.width_px
    ph = photo.height_px
    if rotation == 90:
        pw, ph = ph, pw
    photo_aspect = pw / ph if ph else 1.0
    cell_aspect = cell_w / cell_h if cell_h else 1.0

    if fit_mode is FitMode.STRETCH:
        return (0.0, 0.0, cell_w, cell_h, 0.0, 0.0, 1.0, 1.0)

    if fit_mode is FitMode.FIT:
        if photo_aspect > cell_aspect:
            iw = cell_w
            ih = cell_w / photo_aspect
        else:
            ih = cell_h
            iw = cell_h * photo_aspect
        ix = (cell_w - iw) / 2
        iy = (cell_h - ih) / 2
        return (ix, iy, iw, ih, 0.0, 0.0, 1.0, 1.0)

    # FILL — crop to cell aspect, image fully occupies the cell
    if photo_aspect > cell_aspect:
        # photo wider than cell → crop sides
        new_w = ph * cell_aspect  # in oriented px
        crop_x = (pw - new_w) / 2 / pw
        crop_l, crop_r = crop_x, 1.0 - crop_x
        crop_t, crop_b = 0.0, 1.0
    else:
        new_h = pw / cell_aspect
        crop_y = (ph - new_h) / 2 / ph
        crop_t, crop_b = crop_y, 1.0 - crop_y
        crop_l, crop_r = 0.0, 1.0

    # If rotated, the (crop_l, crop_t, crop_r, crop_b) we computed are in the
    # rotated frame. Map them back to the original-photo frame. A 90° CCW
    # rotation maps rotated (x, y) → original (y, 1-x).
    if rotation == 90:
        ol = crop_t
        ot = 1.0 - crop_r
        or_ = crop_b
        ob = 1.0 - crop_l
        crop_l, crop_t, crop_r, crop_b = ol, ot, or_, ob

    return (0.0, 0.0, cell_w, cell_h, crop_l, crop_t, crop_r, crop_b)


def plan_layout(photos: list[PhotoRef], params: LayoutParams) -> LayoutPlan:
    """Compute a :class:`LayoutPlan` for the given photos and parameters.

    Args:
        photos: Source photos, EXIF-corrected dimensions.
        params: Layout parameters (paper, grid, fit mode, …).

    Returns:
        A :class:`LayoutPlan`. May be empty (no pages) if ``photos`` is empty.

    Raises:
        ValueError: If margins/gutter leave no usable area.
    """
    grid = params.grid
    auto_ls = _auto_landscape(list(photos), grid, params.paper, params.auto_rotate)
    paper = params.paper.with_orientation(params.orientation, auto_ls)

    usable_w = paper.width_mm - params.margins.left - params.margins.right
    usable_h = paper.height_mm - params.margins.top - params.margins.bottom
    if usable_w <= 0 or usable_h <= 0:
        raise ValueError("Margins exceed paper size")

    cell_w = (usable_w - (grid.cols - 1) * params.gutter_mm) / grid.cols
    cell_h = (usable_h - (grid.rows - 1) * params.gutter_mm) / grid.rows
    if cell_w <= 0 or cell_h <= 0:
        raise ValueError("Gutter / grid leaves no room for cells")

    cap_h = (
        params.caption.reserved_mm
        if params.caption.source is not CaptionSource.NONE
        else 0.0
    )
    img_area_h = cell_h - cap_h
    if img_area_h <= 0:
        raise ValueError("Caption reservation leaves no room for image")

    cell_positions = _cells_in_order(grid, params.order)
    per_page = grid.per_page
    pages: list[Page] = []

    if not photos:
        return LayoutPlan(params, ())

    for page_idx in range((len(photos) + per_page - 1) // per_page):
        chunk = photos[page_idx * per_page : (page_idx + 1) * per_page]
        cells: list[CellPlacement] = []
        for slot, photo in enumerate(chunk):
            col, row = cell_positions[slot]
            cx = params.margins.left + col * (cell_w + params.gutter_mm)
            cy = params.margins.top + row * (cell_h + params.gutter_mm)

            rotation = 0
            if params.auto_rotate and _should_rotate(
                photo.aspect, cell_w / img_area_h if img_area_h else 1.0
            ):
                rotation = 90

            ix, iy, iw, ih, cl, ct, cr, cb = _place_in_cell(
                cell_w, img_area_h, photo, params.fit_mode, rotation
            )

            caption_text = ""
            if params.caption.source is CaptionSource.FILENAME:
                caption_text = photo.path.name
            elif params.caption.source is CaptionSource.EXIF_DATE:
                caption_text = photo.caption

            cells.append(
                CellPlacement(
                    cell_x_mm=cx,
                    cell_y_mm=cy,
                    cell_w_mm=cell_w,
                    cell_h_mm=cell_h,
                    photo=photo,
                    img_x_mm=cx + ix,
                    img_y_mm=cy + iy,
                    img_w_mm=iw,
                    img_h_mm=ih,
                    crop_left=cl,
                    crop_top=ct,
                    crop_right=cr,
                    crop_bottom=cb,
                    rotation_deg=rotation,
                    caption_text=caption_text,
                )
            )
        pages.append(Page(paper.width_mm, paper.height_mm, tuple(cells)))

    return LayoutPlan(params, tuple(pages))
