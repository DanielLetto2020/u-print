"""Preview widget — renders a single PDF page to a Gdk.Texture.

We re-render the active layout into a temp PDF at a moderate DPI (96–120) and
then rasterise the requested page via :mod:`pypdfium2`. Each invocation
overwrites the previous temp file. The PDF generation is short for typical
N-up layouts; if it turns out to be slow we can switch to incremental
in-process rendering, but the temp-file approach is simple and reuses the
same code path as the actual print job.
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")

import pypdfium2 as pdfium  # noqa: E402
from gi.repository import Gdk, GLib, GObject, Gtk  # noqa: E402

from photoprint.core.layout import LayoutPlan  # noqa: E402
from photoprint.core.renderer import PREVIEW_DPI, render_plan_to_pdf  # noqa: E402
from photoprint.i18n import gettext as _  # noqa: E402

logger = logging.getLogger(__name__)


class PreviewWidget(Gtk.Box):
    """Pagination strip + the rendered page image."""

    __gsignals__ = {
        # Просим главное окно напечатать только текущую страницу.
        # Аргумент — индекс страницы (0-based).
        "print-page-requested": (GObject.SignalFlags.RUN_FIRST, None, (int,)),
    }

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.set_margin_top(6)
        self.set_margin_bottom(6)
        self.set_margin_start(6)
        self.set_margin_end(6)

        # Pagination + per-page print bar -----------------------------------
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        bar.set_halign(Gtk.Align.CENTER)
        self._prev_btn = Gtk.Button.new_from_icon_name("go-previous-symbolic")
        self._prev_btn.set_tooltip_text(_("Previous page"))
        self._prev_btn.connect("clicked", self._on_prev)
        self._next_btn = Gtk.Button.new_from_icon_name("go-next-symbolic")
        self._next_btn.set_tooltip_text(_("Next page"))
        self._next_btn.connect("clicked", self._on_next)
        self._label = Gtk.Label(label="—")
        self._label.add_css_class("dim-label")

        self._print_page_btn = Gtk.Button.new_from_icon_name(
            "document-print-symbolic"
        )
        self._print_page_btn.set_tooltip_text(_("Print this page only"))
        self._print_page_btn.connect("clicked", self._on_print_page)

        bar.append(self._prev_btn)
        bar.append(self._label)
        bar.append(self._next_btn)
        # Небольшой разделитель и кнопка печати одной страницы.
        sep = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        sep.set_margin_start(4)
        sep.set_margin_end(4)
        bar.append(sep)
        bar.append(self._print_page_btn)
        self.append(bar)

        # Picture in a scroller -----------------------------------------------
        self._picture = Gtk.Picture()
        self._picture.set_can_shrink(True)
        self._picture.set_content_fit(Gtk.ContentFit.CONTAIN)
        self._picture.set_vexpand(True)
        self._picture.set_hexpand(True)
        scroller = Gtk.ScrolledWindow()
        scroller.set_child(self._picture)
        scroller.set_vexpand(True)
        scroller.set_hexpand(True)
        scroller.add_css_class("card")
        self.append(scroller)

        self._tmp_path: Path | None = None
        self._pdf: pdfium.PdfDocument | None = None
        self._page_index = 0
        self._page_count = 0
        self._update_controls()

    # -- Public API ----------------------------------------------------------

    def render(self, plan: LayoutPlan | None) -> None:
        """Re-render the preview from a fresh :class:`LayoutPlan`."""
        self._close_pdf()
        if plan is None or plan.page_count == 0:
            self._picture.set_paintable(None)
            self._page_count = 0
            self._page_index = 0
            self._label.set_text("—")
            self._update_controls()
            return

        if self._tmp_path is None:
            fd, name = tempfile.mkstemp(prefix="photoprint-preview-", suffix=".pdf")
            self._tmp_path = Path(name)
            import os

            os.close(fd)
        try:
            render_plan_to_pdf(plan, self._tmp_path, dpi=PREVIEW_DPI)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Preview rendering failed: %s", exc)
            self._picture.set_paintable(None)
            return

        self._pdf = pdfium.PdfDocument(str(self._tmp_path))
        self._page_count = len(self._pdf)
        self._page_index = min(self._page_index, self._page_count - 1)
        self._show_current_page()
        self._update_controls()

    def cleanup(self) -> None:
        """Release the pdfium handle and remove the temp file."""
        import contextlib

        self._close_pdf()
        if self._tmp_path and self._tmp_path.exists():
            with contextlib.suppress(OSError):
                self._tmp_path.unlink()
        self._tmp_path = None

    # -- Internal ------------------------------------------------------------

    def _close_pdf(self) -> None:
        import contextlib

        if self._pdf is not None:
            with contextlib.suppress(Exception):
                self._pdf.close()
            self._pdf = None

    def _show_current_page(self) -> None:
        if self._pdf is None or self._page_count == 0:
            self._picture.set_paintable(None)
            return
        page = self._pdf[self._page_index]
        # 1.5× the PDF nominal size for a crisp on-screen preview without
        # blowing up memory. The PDF was already rendered at PREVIEW_DPI.
        pil_image = page.render(scale=1.5).to_pil()
        if pil_image.mode != "RGB":
            pil_image = pil_image.convert("RGB")
        import io

        buf = io.BytesIO()
        pil_image.save(buf, "PNG")
        gbytes = GLib.Bytes.new(buf.getvalue())
        try:
            texture = Gdk.Texture.new_from_bytes(gbytes)
        except GLib.Error as exc:
            logger.error("Failed to decode preview texture: %s", exc)
            return
        self._picture.set_paintable(texture)
        self._label.set_text(f"Page {self._page_index + 1} of {self._page_count}")

    def _update_controls(self) -> None:
        self._prev_btn.set_sensitive(self._page_index > 0)
        self._next_btn.set_sensitive(self._page_index + 1 < self._page_count)
        self._print_page_btn.set_sensitive(self._page_count > 0)

    def _on_prev(self, _btn) -> None:
        if self._page_index > 0:
            self._page_index -= 1
            self._show_current_page()
            self._update_controls()

    def _on_next(self, _btn) -> None:
        if self._page_index + 1 < self._page_count:
            self._page_index += 1
            self._show_current_page()
            self._update_controls()

    def _on_print_page(self, _btn) -> None:
        if self._page_count == 0:
            return
        self.emit("print-page-requested", self._page_index)
