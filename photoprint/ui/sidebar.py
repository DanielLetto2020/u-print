"""Settings sidebar — all the knobs that influence the layout.

The widget owns a :class:`LayoutParams` and emits ``params-changed`` whenever
the user changes anything. Listeners (the main window) re-plan the layout and
re-render the preview.
"""

from __future__ import annotations

import logging

import gi

from photoprint.i18n import gettext as _

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GObject, Gtk  # noqa: E402

from photoprint.core.layout import (  # noqa: E402
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
)

logger = logging.getLogger(__name__)

# Option labels are wrapped in _() so they translate at widget-construction
# time. The translation lookup is lazy — it happens when each _() call runs.
# Короткие подписи: число фото + одно слово об ориентации сетки в скобках.
# Логика: «столбиком/stacked» = одна колонка, «в ряд/side-by-side» = одна строка,
# «книжная/portrait» = выше чем шире, «альбомная/landscape» = шире чем выше.
GRID_OPTIONS: list[tuple[str, GridSpec]] = [
    (_("1"), GridSpec(1, 1)),
    (_("2 (stacked)"), GridSpec(1, 2)),
    (_("2 (side by side)"), GridSpec(2, 1)),
    (_("4 (2×2)"), GridSpec(2, 2)),
    (_("6 (portrait)"), GridSpec(2, 3)),
    (_("6 (landscape)"), GridSpec(3, 2)),
    (_("8 (portrait)"), GridSpec(2, 4)),
    (_("8 (landscape)"), GridSpec(4, 2)),
    (_("9 (3×3)"), GridSpec(3, 3)),
    (_("16 (4×4)"), GridSpec(4, 4)),
]

PAPER_OPTIONS = ["A4", "A5", "A3", "Letter", "Legal", "10x15", "13x18", "20x30"]

ORIENTATION_OPTIONS = [
    (_("Auto"), Orientation.AUTO),
    (_("Portrait"), Orientation.PORTRAIT),
    (_("Landscape"), Orientation.LANDSCAPE),
]

FIT_OPTIONS = [
    (_("Fit (contain)"), FitMode.FIT),
    (_("Fill (cover, crops)"), FitMode.FILL),
    (_("Stretch (distorts)"), FitMode.STRETCH),
]

ORDER_OPTIONS = [
    (_("Left→right, top→bottom"), FillOrder.ROW_MAJOR),
    (_("Top→bottom, left→right"), FillOrder.COLUMN_MAJOR),
]

CAPTION_OPTIONS = [
    (_("None"), CaptionSource.NONE),
    (_("File name"), CaptionSource.FILENAME),
    (_("EXIF date"), CaptionSource.EXIF_DATE),
]


class SettingsSidebar(Gtk.Box):
    """The settings panel. Emits ``params-changed`` on every change."""

    __gsignals__ = {"params-changed": (GObject.SignalFlags.RUN_FIRST, None, ())}

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        # 360 — минимум, чтобы локализованные подписи (например, русские)
        # в Adw.ComboRow помещались без переноса. Реальную ширину пользователь
        # подбирает через Gtk.Paned-сплиттер в главном окне.
        self.set_size_request(360, -1)
        self._suppress = False  # True пока программно синхронизируем UI из params

        scroller = Gtk.ScrolledWindow()
        scroller.set_vexpand(True)
        clamp = Adw.Clamp()
        clamp.set_maximum_size(520)
        clamp.set_tightening_threshold(360)

        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        page.set_margin_top(12)
        page.set_margin_bottom(12)
        page.set_margin_start(12)
        page.set_margin_end(12)
        clamp.set_child(page)
        scroller.set_child(clamp)
        self.append(scroller)

        # -- Paper group --------------------------------------------------
        paper_group = Adw.PreferencesGroup(title=_("Paper"))
        self._paper_row = Adw.ComboRow(title=_("Size"))
        self._paper_row.set_model(Gtk.StringList.new(PAPER_OPTIONS))
        self._paper_row.connect("notify::selected", self._on_changed)
        paper_group.add(self._paper_row)

        self._orient_row = Adw.ComboRow(title=_("Orientation"))
        self._orient_row.set_model(Gtk.StringList.new([n for n, _o in ORIENTATION_OPTIONS]))
        self._orient_row.connect("notify::selected", self._on_changed)
        paper_group.add(self._orient_row)
        page.append(paper_group)

        # -- Layout group -------------------------------------------------
        layout_group = Adw.PreferencesGroup(title=_("Layout"))
        self._grid_row = Adw.ComboRow(title=_("Photos per page"))
        self._grid_row.set_model(Gtk.StringList.new([n for n, _g in GRID_OPTIONS]))
        self._grid_row.set_selected(3)  # default 4 (2×2)
        self._grid_row.connect("notify::selected", self._on_changed)
        layout_group.add(self._grid_row)

        self._fit_row = Adw.ComboRow(title=_("Fit mode"))
        self._fit_row.set_model(Gtk.StringList.new([n for n, _f in FIT_OPTIONS]))
        self._fit_row.connect("notify::selected", self._on_changed)
        layout_group.add(self._fit_row)

        self._order_row = Adw.ComboRow(title=_("Fill order"))
        self._order_row.set_subtitle(
            _("Visible only on grids with 2+ rows and 2+ columns")
        )
        self._order_row.set_model(Gtk.StringList.new([n for n, _o in ORDER_OPTIONS]))
        self._order_row.connect("notify::selected", self._on_changed)
        layout_group.add(self._order_row)

        self._auto_rot_row = Adw.SwitchRow(
            title=_("Auto-rotate to fit"),
            subtitle=_("Turn photos 90° when it improves cell coverage"),
        )
        self._auto_rot_row.set_active(True)
        self._auto_rot_row.connect("notify::active", self._on_changed)
        layout_group.add(self._auto_rot_row)
        page.append(layout_group)

        # -- Margins + gutter ---------------------------------------------
        spacing_group = Adw.PreferencesGroup(title=_("Spacing (mm)"))
        self._margin_top = self._spin_row(spacing_group, _("Margin: top"), 5.0)
        self._margin_right = self._spin_row(spacing_group, _("Margin: right"), 5.0)
        self._margin_bottom = self._spin_row(spacing_group, _("Margin: bottom"), 5.0)
        self._margin_left = self._spin_row(spacing_group, _("Margin: left"), 5.0)
        self._gutter = self._spin_row(spacing_group, _("Gutter between cells"), 2.0)
        page.append(spacing_group)

        # -- Border -------------------------------------------------------
        border_group = Adw.PreferencesGroup(title=_("Photo border"))
        self._border_width = self._spin_row(
            border_group, _("Width (pt)"), 0.0, lower=0.0, upper=10.0, step=0.5
        )
        self._border_color = Gtk.ColorDialogButton()
        cd = Gtk.ColorDialog()
        cd.set_with_alpha(False)
        self._border_color.set_dialog(cd)
        rgba = Gdk.RGBA()
        rgba.parse("#000000")
        self._border_color.set_rgba(rgba)
        self._border_color.connect("notify::rgba", self._on_changed)
        color_row = Adw.ActionRow(title=_("Border colour"))
        color_row.add_suffix(self._border_color)
        border_group.add(color_row)
        page.append(border_group)

        # -- Caption ------------------------------------------------------
        cap_group = Adw.PreferencesGroup(title=_("Caption"))
        self._caption_row = Adw.ComboRow(title=_("Show under photo"))
        self._caption_row.set_model(Gtk.StringList.new([n for n, _c in CAPTION_OPTIONS]))
        self._caption_row.connect("notify::selected", self._on_changed)
        cap_group.add(self._caption_row)

        self._caption_size = self._spin_row(
            cap_group, _("Font size (pt)"), 8.0, lower=4.0, upper=24.0, step=0.5
        )
        page.append(cap_group)

    # -- Helpers -------------------------------------------------------------

    def _spin_row(
        self,
        group: Adw.PreferencesGroup,
        title: str,
        initial: float,
        *,
        lower: float = 0.0,
        upper: float = 100.0,
        step: float = 1.0,
    ) -> Gtk.SpinButton:
        adj = Gtk.Adjustment(value=initial, lower=lower, upper=upper, step_increment=step)
        spin = Gtk.SpinButton(adjustment=adj, digits=1)
        spin.set_valign(Gtk.Align.CENTER)
        spin.connect("value-changed", self._on_changed)
        row = Adw.ActionRow(title=title)
        row.add_suffix(spin)
        row.set_activatable_widget(spin)
        group.add(row)
        return spin

    def _on_changed(self, *_args) -> None:
        if not self._suppress:
            self.emit("params-changed")

    # -- Public API ----------------------------------------------------------

    def get_params(self) -> LayoutParams:
        """Build a :class:`LayoutParams` from the current UI state."""
        paper_key = PAPER_OPTIONS[self._paper_row.get_selected()]
        paper = PAPER_SIZES[paper_key]
        orient = ORIENTATION_OPTIONS[self._orient_row.get_selected()][1]
        grid = GRID_OPTIONS[self._grid_row.get_selected()][1]
        fit = FIT_OPTIONS[self._fit_row.get_selected()][1]
        order = ORDER_OPTIONS[self._order_row.get_selected()][1]
        rgba = self._border_color.get_rgba()
        border = BorderSpec(
            width_pt=self._border_width.get_value(),
            color_rgb=(rgba.red, rgba.green, rgba.blue),
        )
        cap_source = CAPTION_OPTIONS[self._caption_row.get_selected()][1]
        caption = CaptionSpec(
            source=cap_source,
            font_size_pt=self._caption_size.get_value(),
            reserved_mm=max(3.0, self._caption_size.get_value() * 0.5),
        )
        return LayoutParams(
            paper=paper,
            orientation=orient,
            grid=grid,
            margins=Margins(
                top=self._margin_top.get_value(),
                right=self._margin_right.get_value(),
                bottom=self._margin_bottom.get_value(),
                left=self._margin_left.get_value(),
            ),
            gutter_mm=self._gutter.get_value(),
            fit_mode=fit,
            order=order,
            auto_rotate=self._auto_rot_row.get_active(),
            border=border,
            caption=caption,
        )

    def set_params(self, p: LayoutParams) -> None:
        """Apply a :class:`LayoutParams` to the UI without re-emitting signals."""
        self._suppress = True
        try:
            # Paper
            try:
                pi = PAPER_OPTIONS.index(p.paper.name)
            except ValueError:
                pi = 0
            self._paper_row.set_selected(pi)

            oi = next(
                (i for i, (_, o) in enumerate(ORIENTATION_OPTIONS) if o is p.orientation),
                0,
            )
            self._orient_row.set_selected(oi)

            gi_ = next(
                (i for i, (_, g) in enumerate(GRID_OPTIONS) if g == p.grid),
                3,
            )
            self._grid_row.set_selected(gi_)

            fi = next((i for i, (_, f) in enumerate(FIT_OPTIONS) if f is p.fit_mode), 0)
            self._fit_row.set_selected(fi)

            ri = next((i for i, (_, o) in enumerate(ORDER_OPTIONS) if o is p.order), 0)
            self._order_row.set_selected(ri)

            self._auto_rot_row.set_active(p.auto_rotate)
            self._margin_top.set_value(p.margins.top)
            self._margin_right.set_value(p.margins.right)
            self._margin_bottom.set_value(p.margins.bottom)
            self._margin_left.set_value(p.margins.left)
            self._gutter.set_value(p.gutter_mm)
            self._border_width.set_value(p.border.width_pt)

            rgba = Gdk.RGBA()
            rgba.red, rgba.green, rgba.blue = p.border.color_rgb
            rgba.alpha = 1.0
            self._border_color.set_rgba(rgba)

            ci = next(
                (i for i, (_, s) in enumerate(CAPTION_OPTIONS) if s is p.caption.source),
                0,
            )
            self._caption_row.set_selected(ci)
            self._caption_size.set_value(p.caption.font_size_pt)
        finally:
            self._suppress = False
