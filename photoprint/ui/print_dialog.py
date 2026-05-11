"""Adwaita dialog for selecting printer + CUPS options.

Used by the main window's Print action. Returns a :class:`PrintOptions` plus
the chosen printer name through a callback rather than a blocking call —
Gtk4 dialogs are asynchronous.
"""

from __future__ import annotations

from collections.abc import Callable

import gi

from photoprint.i18n import gettext as _

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: E402

from photoprint.core.printer import PrinterInfo, PrintOptions, list_printers  # noqa: E402

ResultCallback = Callable[[str | None, PrintOptions], None]


class PrintDialog(Adw.Dialog):
    """Modal printer + options picker."""

    def __init__(self, default_printer: str = "", on_result: ResultCallback | None = None) -> None:
        super().__init__()
        self.set_title(_("Print"))
        self.set_content_width(420)
        self._on_result = on_result
        self._printers: list[PrinterInfo] = list_printers()

        toolbar_view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_show_end_title_buttons(False)
        toolbar_view.add_top_bar(header)

        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        page.set_margin_top(12)
        page.set_margin_bottom(12)
        page.set_margin_start(12)
        page.set_margin_end(12)

        group = Adw.PreferencesGroup()

        # Printer combo
        self._printer_row = Adw.ComboRow(title=_("Printer"))
        names = [p.display for p in self._printers] or [_("No printers found")]
        self._printer_row.set_model(Gtk.StringList.new(names))
        if self._printers:
            default_idx = next(
                (i for i, p in enumerate(self._printers) if p.name == default_printer),
                next(
                    (i for i, p in enumerate(self._printers) if p.is_default),
                    0,
                ),
            )
            self._printer_row.set_selected(default_idx)
        else:
            self._printer_row.set_sensitive(False)
        group.add(self._printer_row)

        # Copies
        copies_adj = Gtk.Adjustment(value=1, lower=1, upper=99, step_increment=1)
        self._copies_spin = Gtk.SpinButton(adjustment=copies_adj)
        self._copies_spin.set_valign(Gtk.Align.CENTER)
        copies_row = Adw.ActionRow(title=_("Copies"))
        copies_row.add_suffix(self._copies_spin)
        copies_row.set_activatable_widget(self._copies_spin)
        group.add(copies_row)

        # Color mode
        self._color_row = Adw.ComboRow(title=_("Color"))
        self._color_row.set_model(Gtk.StringList.new([_("Colour"), _("Mono (B/W)")]))
        group.add(self._color_row)

        # Quality
        self._quality_row = Adw.ComboRow(title=_("Quality"))
        self._quality_row.set_model(Gtk.StringList.new([_("Draft"), _("Normal"), _("Best")]))
        self._quality_row.set_selected(1)
        group.add(self._quality_row)

        page.append(group)

        # Action buttons
        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        actions.set_halign(Gtk.Align.END)
        cancel = Gtk.Button.new_with_label(_("Cancel"))
        cancel.connect("clicked", self._on_cancel)
        actions.append(cancel)
        print_btn = Gtk.Button.new_with_label(_("Print"))
        print_btn.add_css_class("suggested-action")
        print_btn.set_sensitive(bool(self._printers))
        print_btn.connect("clicked", self._on_print)
        actions.append(print_btn)
        page.append(actions)

        toolbar_view.set_content(page)
        self.set_child(toolbar_view)

    def _selected_printer_name(self) -> str | None:
        if not self._printers:
            return None
        return self._printers[self._printer_row.get_selected()].name

    def _make_options(self) -> PrintOptions:
        return PrintOptions(
            copies=int(self._copies_spin.get_value()),
            color_mode="mono" if self._color_row.get_selected() == 1 else "color",
            quality={0: "draft", 1: "normal", 2: "best"}.get(
                self._quality_row.get_selected(), "normal"
            ),
        )

    def _on_cancel(self, _btn) -> None:
        if self._on_result:
            self._on_result(None, self._make_options())
        self.close()

    def _on_print(self, _btn) -> None:
        if self._on_result:
            self._on_result(self._selected_printer_name(), self._make_options())
        self.close()
