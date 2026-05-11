"""Main application window.

Layout: an :class:`Adw.OverlaySplitView` with the photo list on the left side
and the preview + settings sidebar on the right (right sidebar is implemented
with a :class:`Gtk.Paned`).

State is owned here: the current photo list (via :class:`PhotoListWidget`) and
the current layout params (via :class:`SettingsSidebar`). Whenever either
changes we re-plan and re-render the preview. Print and Save-as-PDF render a
fresh PDF on demand at the full target DPI.
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path

import gi

from photoprint.i18n import gettext as _

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from photoprint.core.layout import LayoutPlan, plan_layout  # noqa: E402
from photoprint.core.printer import PrintOptions, print_pdf  # noqa: E402
from photoprint.core.renderer import DEFAULT_DPI, render_plan_to_pdf  # noqa: E402
from photoprint.core.settings import (  # noqa: E402
    AppSettings,
    layout_from_dict,
    layout_to_dict,
    load_presets,
    load_session,
    load_settings,
    save_presets,
    save_session,
    save_settings,
)
from photoprint.ui.photo_list import PhotoListWidget  # noqa: E402
from photoprint.ui.preview import PreviewWidget  # noqa: E402
from photoprint.ui.print_dialog import PrintDialog  # noqa: E402
from photoprint.ui.search_view import SearchView  # noqa: E402
from photoprint.ui.sidebar import SettingsSidebar  # noqa: E402

logger = logging.getLogger(__name__)


class MainWindow(Adw.ApplicationWindow):
    """The single top-level window."""

    def __init__(self, app: Adw.Application) -> None:
        super().__init__(application=app)
        self.set_title(_("PhotoPrint"))
        self.set_default_size(1280, 800)

        self._settings: AppSettings = load_settings()
        self._presets = load_presets()
        self._render_timeout: int | None = None
        self._last_plan: LayoutPlan | None = None

        # -- Action group (must exist before any menu binding references it) --
        self._install_action_group()

        # -- HeaderBar with ViewSwitcher as title -------------------------
        header = Adw.HeaderBar()

        self._presets_menu_btn = Gtk.MenuButton()
        self._presets_menu_btn.set_icon_name("starred-symbolic")
        self._presets_menu_btn.set_tooltip_text(_("Presets"))
        self._rebuild_presets_menu()
        header.pack_start(self._presets_menu_btn)

        self._save_pdf_btn = Gtk.Button.new_with_label(_("Save PDF…"))
        self._save_pdf_btn.set_tooltip_text(_("Save layout as PDF (Ctrl+S)"))
        self._save_pdf_btn.connect("clicked", lambda *args: self._on_save_pdf())
        header.pack_end(self._save_pdf_btn)

        self._print_btn = Gtk.Button.new_with_label(_("Print…"))
        self._print_btn.add_css_class("suggested-action")
        self._print_btn.set_tooltip_text(_("Print (Ctrl+P)"))
        self._print_btn.connect("clicked", lambda *args: self._on_print())
        header.pack_end(self._print_btn)

        # Hamburger menu: language switcher (+ About in a future iteration)
        self._main_menu_btn = Gtk.MenuButton()
        self._main_menu_btn.set_icon_name("open-menu-symbolic")
        self._main_menu_btn.set_tooltip_text(_("Main menu"))
        self._build_main_menu()
        header.pack_end(self._main_menu_btn)

        # -- Print page content (photo list | preview | sidebar) ---------
        self._photo_list = PhotoListWidget()
        self._photo_list.connect("photos-changed", self._on_photos_changed)

        self._preview = PreviewWidget()
        self._sidebar = SettingsSidebar()
        self._sidebar.connect("params-changed", self._on_params_changed)

        right_paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        right_paned.set_start_child(self._preview)
        right_paned.set_end_child(self._sidebar)
        right_paned.set_resize_start_child(True)
        right_paned.set_shrink_start_child(False)
        right_paned.set_shrink_end_child(False)
        right_paned.set_position(900)

        main_paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        photo_panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        photo_panel.set_size_request(280, -1)
        photo_panel.append(self._photo_list)
        main_paned.set_start_child(photo_panel)
        main_paned.set_end_child(right_paned)
        main_paned.set_resize_start_child(False)
        main_paned.set_position(360)

        # -- Search page -------------------------------------------------
        self._search_view = SearchView()
        self._search_view.connect("send-to-print", self._on_send_to_print)

        # -- ViewStack switcher ------------------------------------------
        self._view_stack = Adw.ViewStack()
        self._view_stack.add_titled_with_icon(
            self._search_view, "search", _("Search"), "system-search-symbolic"
        )
        self._view_stack.add_titled_with_icon(
            main_paned, "print", _("Print"), "printer-symbolic"
        )
        # Стартуем на вкладке Print — то, что собирали раньше.
        self._view_stack.set_visible_child_name("print")
        self._view_stack.connect("notify::visible-child-name", self._on_view_changed)

        switcher = Adw.ViewSwitcher()
        switcher.set_stack(self._view_stack)
        switcher.set_policy(Adw.ViewSwitcherPolicy.WIDE)
        header.set_title_widget(switcher)

        toolbar_view = Adw.ToolbarView()
        toolbar_view.add_top_bar(header)
        toolbar_view.set_content(self._view_stack)
        self.set_content(toolbar_view)

        # Сразу синхронизируем видимость print-only кнопок.
        self._on_view_changed()

        # -- Keyboard shortcuts ------------------------------------------
        self._install_shortcuts()

        # -- Restore session ---------------------------------------------
        if self._settings.last_layout:
            try:
                self._sidebar.set_params(layout_from_dict(self._settings.last_layout))
            except (KeyError, ValueError) as exc:
                logger.warning("Failed to restore layout: %s", exc)

        session = load_session()
        if session and session.photo_paths:
            self._pending_session = session
            GLib.idle_add(self._offer_restore_session_idle)

        # First render (just to display empty state)
        self._schedule_render()
        self.connect("close-request", self._on_close_request)

    # -- Public --------------------------------------------------------------

    def add_photos(self, paths: list[Path]) -> None:
        """Append photos by path. Triggers a re-render."""
        self._photo_list.add_paths(paths)

    # -- Views ---------------------------------------------------------------

    def _on_view_changed(self, *_args) -> None:
        """Show print-only action buttons only on the Print page."""
        is_print = self._view_stack.get_visible_child_name() == "print"
        self._save_pdf_btn.set_visible(is_print)
        self._print_btn.set_visible(is_print)
        self._presets_menu_btn.set_visible(is_print)

    def _on_send_to_print(self, _view, paths) -> None:
        """Принять выбранные фото из Search и переключиться на Print."""
        self._photo_list.add_paths([Path(p) for p in paths])
        self._view_stack.set_visible_child_name("print")

    # -- Shortcuts -----------------------------------------------------------

    def _install_shortcuts(self) -> None:
        controller = Gtk.ShortcutController()
        controller.set_scope(Gtk.ShortcutScope.GLOBAL)

        def add(accel: str, cb) -> None:
            controller.add_shortcut(
                Gtk.Shortcut.new(
                    Gtk.ShortcutTrigger.parse_string(accel),
                    Gtk.CallbackAction.new(lambda *_a, _cb=cb: (_cb(), True)[1]),
                )
            )

        add("<Control>p", self._on_print)
        add("<Control>s", self._on_save_pdf)
        add("<Control>o", lambda: self._photo_list._on_add_clicked(None))
        add("<Control>a", lambda: self._photo_list.select_all())
        add("Delete", lambda: self._photo_list.remove_selected())
        self.add_controller(controller)

    # -- Re-planning / rendering --------------------------------------------

    def _on_photos_changed(self, *_args) -> None:
        self._schedule_render()

    def _on_params_changed(self, *_args) -> None:
        self._schedule_render()

    def _schedule_render(self) -> None:
        """Coalesce rapid changes (slider drag, etc.) into one render."""
        if self._render_timeout is not None:
            GLib.source_remove(self._render_timeout)
        self._render_timeout = GLib.timeout_add(120, self._do_render)

    def _do_render(self) -> bool:
        self._render_timeout = None
        photos = self._photo_list.photos
        params = self._sidebar.get_params()
        try:
            plan = plan_layout(photos, params)
        except ValueError as exc:
            logger.warning("Layout error: %s", exc)
            plan = None
        self._last_plan = plan
        self._preview.render(plan)
        # Enable / disable action buttons
        any_pages = plan is not None and plan.page_count > 0
        self._print_btn.set_sensitive(any_pages)
        self._save_pdf_btn.set_sensitive(any_pages)
        return False  # one-shot

    # -- Print ---------------------------------------------------------------

    def _on_print(self) -> None:
        if not self._last_plan or self._last_plan.page_count == 0:
            return

        dialog = PrintDialog(
            default_printer=self._settings.last_printer,
            on_result=self._handle_print_result,
        )
        dialog.present(self)

    def _handle_print_result(self, printer: str | None, opts: PrintOptions) -> None:
        if printer is None or self._last_plan is None:
            return
        try:
            fd, name = tempfile.mkstemp(prefix="photoprint-", suffix=".pdf")
            import os

            os.close(fd)
            pdf_path = Path(name)
            render_plan_to_pdf(self._last_plan, pdf_path, dpi=DEFAULT_DPI)
            job_id = print_pdf(printer, pdf_path, opts, title="PhotoPrint")
            logger.info("Submitted CUPS job %s on %s", job_id, printer)
            self._settings.last_printer = printer
            save_settings(self._settings)
            self._toast(_("Submitted print job #{job_id}").format(job_id=job_id))
        except Exception as exc:  # noqa: BLE001
            logger.exception("Print failed")
            self._error_dialog(_("Print failed"), str(exc))

    # -- Save PDF ------------------------------------------------------------

    def _on_save_pdf(self) -> None:
        if not self._last_plan or self._last_plan.page_count == 0:
            return
        dialog = Gtk.FileDialog()
        dialog.set_title(_("Save layout as PDF"))
        dialog.set_initial_name("photoprint.pdf")
        dialog.save(self, None, self._on_save_pdf_done)

    def _on_save_pdf_done(self, dialog, result):
        try:
            f = dialog.save_finish(result)
        except GLib.Error:
            return
        path = Path(f.get_path())
        try:
            render_plan_to_pdf(self._last_plan, path, dpi=DEFAULT_DPI)
            self._toast(_("Saved to {name}").format(name=path.name))
        except Exception as exc:  # noqa: BLE001
            logger.exception("Save PDF failed")
            self._error_dialog(_("Save failed"), str(exc))

    # -- Presets -------------------------------------------------------------

    def _install_action_group(self) -> None:
        """Build the ``win.*`` action group once. Stable for the window's lifetime."""
        group = Gio.SimpleActionGroup()

        save_act = Gio.SimpleAction.new("save-preset", None)
        save_act.connect("activate", lambda *args: self._save_current_as_preset())
        group.add_action(save_act)

        load_act = Gio.SimpleAction.new("load-preset", GLib.VariantType.new("i"))
        load_act.connect("activate", self._load_preset_action)
        group.add_action(load_act)

        lang_act = Gio.SimpleAction.new_stateful(
            "set-language",
            GLib.VariantType.new("s"),
            GLib.Variant.new_string(self._settings.language or "ru"),
        )
        lang_act.connect("activate", self._on_set_language)
        group.add_action(lang_act)

        self.insert_action_group("win", group)

    def _rebuild_presets_menu(self) -> None:
        """Rebuild only the preset menu model. Actions themselves are stable."""
        menu = Gio.Menu()
        section_load = Gio.Menu()
        for i, p in enumerate(self._presets):
            section_load.append(p.name, f"win.load-preset({i})")
        menu.append_section(_("Apply preset"), section_load)

        section_manage = Gio.Menu()
        section_manage.append(_("Save current as preset…"), "win.save-preset")
        menu.append_section(None, section_manage)
        self._presets_menu_btn.set_menu_model(menu)

    def _build_main_menu(self) -> None:
        """Build the hamburger menu — currently the language switcher."""
        menu = Gio.Menu()
        lang_menu = Gio.Menu()
        # Stateful action — GTK renders these as radio items with the active
        # state checked. Action target encodes the language code.
        lang_menu.append(_("Auto (system locale)"), "win.set-language::auto")
        lang_menu.append("Русский", "win.set-language::ru")
        lang_menu.append("English", "win.set-language::en")
        menu.append_submenu(_("Language"), lang_menu)
        self._main_menu_btn.set_menu_model(menu)

    def _on_set_language(self, action, value: GLib.Variant) -> None:
        """Persist the chosen language and ask the user to restart."""
        lang = value.get_string()
        if lang not in {"auto", "ru", "en"}:
            return
        action.set_state(value)
        self._settings.language = lang
        save_settings(self._settings)
        # Persist current photos as a session so they reappear after restart.
        photos = self._photo_list.photos
        if photos:
            from photoprint.core.settings import Session

            save_session(
                Session(
                    photo_paths=[str(p.path) for p in photos],
                    layout=layout_to_dict(self._sidebar.get_params()),
                )
            )
        labels = {"auto": _("Auto (system locale)"), "ru": "Русский", "en": "English"}
        dialog = Adw.AlertDialog(
            heading=_("Restart required"),
            body=_("Language set to {name}. Restart PhotoPrint to apply.").format(
                name=labels[lang]
            ),
        )
        dialog.add_response("ok", _("OK"))
        dialog.set_default_response("ok")
        dialog.present(self)

    def _save_current_as_preset(self) -> None:
        dialog = Adw.AlertDialog(
            heading=_("Save preset"), body=_("Enter a name for this preset:")
        )
        entry = Gtk.Entry()
        entry.set_placeholder_text(_("e.g. Passport 4×6"))
        dialog.set_extra_child(entry)
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("save", _("Save"))
        dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("save")

        def on_response(_d, response):
            if response != "save":
                return
            name = entry.get_text().strip()
            if not name:
                return
            from photoprint.core.settings import Preset

            self._presets.append(Preset(name=name, layout=layout_to_dict(self._sidebar.get_params())))
            save_presets(self._presets)
            self._rebuild_presets_menu()
            self._toast(_("Preset “{name}” saved").format(name=name))

        dialog.connect("response", on_response)
        dialog.present(self)

    def _load_preset_action(self, _action, param) -> None:
        idx = param.get_int32()
        if 0 <= idx < len(self._presets):
            try:
                self._sidebar.set_params(layout_from_dict(self._presets[idx].layout))
                self._schedule_render()
                self._toast(
                    _("Applied preset “{name}”").format(name=self._presets[idx].name)
                )
            except (KeyError, ValueError) as exc:
                logger.warning("Bad preset: %s", exc)

    # -- Session -------------------------------------------------------------

    def _offer_restore_session_idle(self) -> bool:
        """Defer the restore prompt until the window has been presented."""
        if getattr(self, "_pending_session", None):
            self._offer_restore_session(self._pending_session)
            self._pending_session = None
        return False

    def _offer_restore_session(self, session) -> None:
        dialog = Adw.AlertDialog(
            heading=_("Restore previous session?"),
            body=_("You had {n} photo(s) loaded last time.").format(
                n=len(session.photo_paths)
            ),
        )
        dialog.add_response("discard", _("Discard"))
        dialog.add_response("restore", _("Restore"))
        dialog.set_response_appearance("restore", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("restore")

        def on_response(_d, response):
            if response == "restore":
                paths = [Path(p) for p in session.photo_paths if Path(p).exists()]
                self._photo_list.add_paths(paths)
                if session.layout:
                    import contextlib

                    with contextlib.suppress(KeyError, ValueError):
                        self._sidebar.set_params(layout_from_dict(session.layout))

        dialog.connect("response", on_response)
        dialog.present(self)

    def _on_close_request(self, *_args) -> bool:
        photos = self._photo_list.photos
        params = self._sidebar.get_params()
        self._settings.last_layout = layout_to_dict(params)
        save_settings(self._settings)
        if photos:
            from photoprint.core.settings import Session

            save_session(
                Session(
                    photo_paths=[str(p.path) for p in photos],
                    layout=layout_to_dict(params),
                )
            )
        else:
            from photoprint.core.settings import clear_session

            clear_session()
        self._preview.cleanup()
        return False  # let the window close

    # -- Toast / error helpers ----------------------------------------------

    def _toast(self, message: str) -> None:
        # Minimal: log; full Adw.Toast support would need an Adw.ToastOverlay
        # wrapping the content, which we'll add when overlay/inline UX matures.
        logger.info(message)

    def _error_dialog(self, heading: str, body: str) -> None:
        dialog = Adw.AlertDialog(heading=heading, body=body)
        dialog.add_response("ok", _("OK"))
        dialog.set_default_response("ok")
        dialog.present(self)


