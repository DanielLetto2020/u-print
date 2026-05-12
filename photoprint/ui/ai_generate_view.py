"""Вкладка «AI Generate»: генерация и итеративная правка картинок через OpenRouter.

Архитектура:
* Сессии — это локальная история «prompt → картинки» (см.
  :mod:`photoprint.core.ai_sessions`). Каждая сессия привязана к одной
  выбранной модели; внутри пользователь может уточнять prompt, и каждое
  новое сообщение автоматически использует последнюю сгенерированную
  картинку как референс — отсюда «изменять картинки».
* Все сетевые вызовы идут в worker-треде, UI обновляется через
  :func:`GLib.idle_add`. Кнопки на время генерации блокируются.
* Сразу после успешной генерации мы эмитим сигнал ``output-folder-ready``
  с путём к папке — :class:`MainWindow` пробрасывает его в Search, и фото
  появляются в индексе без отдельных действий.
"""

from __future__ import annotations

import contextlib
import logging
import threading
from datetime import datetime
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gio, GLib, GObject, Gtk  # noqa: E402

from photoprint.core.ai_generate import (  # noqa: E402
    AIModel,
    GeneratedImage,
    OpenRouterError,
    generate_image,
    list_image_models,
    save_image_bytes,
)
from photoprint.core.ai_sessions import (  # noqa: E402
    AIMessage,
    AISession,
    load_all,
    save_all,
    touch,
)
from photoprint.core.settings import AppSettings, save_settings  # noqa: E402
from photoprint.i18n import gettext as _  # noqa: E402
from photoprint.ui.file_manager import open_in_file_manager  # noqa: E402
from photoprint.ui.thumbnail_loader import get_default as get_thumbnail_loader  # noqa: E402

logger = logging.getLogger(__name__)

GALLERY_THUMB = 240    # сторона миниатюры в галерее сессии


def _default_output_dir() -> Path:
    """Папка по умолчанию: ``~/Pictures/PhotoPrint AI`` (создаётся при сохранении)."""
    return Path.home() / "Pictures" / "PhotoPrint AI"


class AIGenerateView(Gtk.Box):
    """Корневой виджет вкладки AI Generate."""

    __gsignals__ = {
        # Эмитим путь к папке с генерациями, когда юзер её выбирает ИЛИ когда
        # очередная картинка сохраняется. MainWindow подцепляет это и говорит
        # Search-вкладке проиндексировать (если ещё не) и пересканировать.
        "output-folder-ready": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
    }

    def __init__(self, settings: AppSettings) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.set_hexpand(True)
        self.set_vexpand(True)

        self._settings = settings
        self._loader = get_thumbnail_loader()
        self._models: list[AIModel] = []
        self._sessions: list[AISession] = load_all()
        self._active: AISession | None = self._sessions[0] if self._sessions else None
        self._busy = False  # активная генерация / загрузка списка моделей

        # -- Stack: либо «нет ключа», либо «нет моделей», либо рабочая UI ----
        self._stack = Gtk.Stack()
        self._stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self._stack.set_vexpand(True)
        self._stack.add_named(self._build_no_key_page(), "no-key")
        self._stack.add_named(self._build_loading_page(), "loading")
        self._stack.add_named(self._build_error_page(), "error")
        self._stack.add_named(self._build_main_page(), "main")
        self.append(self._stack)

        # Стартовая логика: если ключ есть — пробуем подтянуть модели сразу.
        if self._settings.openrouter_api_key.strip():
            self._start_load_models()
        else:
            self._stack.set_visible_child_name("no-key")
        # Подсунем сразу сессии и историю, чтобы при появлении ключа всё было.
        self._rebuild_sessions_list()
        self._rebuild_conversation()
        self._update_action_state()

    # -- Pages ----------------------------------------------------------------

    def _build_no_key_page(self) -> Gtk.Widget:
        """Заглушка с просьбой ввести ключ OpenRouter."""
        page = Adw.StatusPage()
        page.set_icon_name("dialog-password-symbolic")
        page.set_title(_("OpenRouter key required"))
        page.set_description(
            _(
                "Enter your OpenRouter API key to load image-capable models. "
                "The key is stored locally in your settings."
            )
        )
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_halign(Gtk.Align.CENTER)
        box.set_size_request(420, -1)

        self._no_key_entry = Gtk.Entry()
        self._no_key_entry.set_visibility(False)
        self._no_key_entry.set_input_purpose(Gtk.InputPurpose.PASSWORD)
        self._no_key_entry.set_placeholder_text(_("sk-or-v1-…"))
        # Глазик «показать пароль» — встроенный, по клику переключает видимость.
        self._no_key_entry.set_icon_from_icon_name(
            Gtk.EntryIconPosition.SECONDARY, "view-conceal-symbolic"
        )
        self._no_key_entry.connect("icon-press", self._toggle_key_visibility)
        box.append(self._no_key_entry)

        save_btn = Gtk.Button.new_with_label(_("Save and load models"))
        save_btn.add_css_class("pill")
        save_btn.add_css_class("suggested-action")
        save_btn.set_halign(Gtk.Align.CENTER)
        save_btn.connect("clicked", self._on_save_no_key_clicked)
        box.append(save_btn)

        hint = Gtk.Label()
        # Не используем set_markup — переводы могут содержать `&`/`<`, что
        # сломает разбор. Делаем визуально мельче через CSS-класс.
        hint.set_text(_("Get a key at openrouter.ai/keys (free models are available)."))
        hint.add_css_class("dim-label")
        hint.add_css_class("caption")
        hint.set_halign(Gtk.Align.CENTER)
        box.append(hint)

        page.set_child(box)
        return page

    def _build_loading_page(self) -> Gtk.Widget:
        """Спиннер «грузим каталог моделей»."""
        page = Adw.StatusPage()
        page.set_icon_name("emblem-synchronizing-symbolic")
        page.set_title(_("Loading models…"))
        page.set_description(_("Querying OpenRouter for image-capable models."))
        spinner = Gtk.Spinner()
        spinner.set_size_request(48, 48)
        spinner.start()
        spinner.set_halign(Gtk.Align.CENTER)
        page.set_child(spinner)
        return page

    def _build_error_page(self) -> Gtk.Widget:
        """Страница с описанием ошибки и кнопкой повтора."""
        page = Adw.StatusPage()
        page.set_icon_name("dialog-error-symbolic")
        page.set_title(_("Could not load models"))
        self._error_page = page

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_halign(Gtk.Align.CENTER)

        self._error_detail = Gtk.Label()
        self._error_detail.add_css_class("dim-label")
        self._error_detail.set_wrap(True)
        self._error_detail.set_max_width_chars(64)
        self._error_detail.set_justify(Gtk.Justification.CENTER)
        box.append(self._error_detail)

        retry_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        retry_row.set_halign(Gtk.Align.CENTER)
        retry_btn = Gtk.Button.new_with_label(_("Retry"))
        retry_btn.add_css_class("pill")
        retry_btn.connect("clicked", lambda *args: self._start_load_models())
        retry_row.append(retry_btn)
        change_key_btn = Gtk.Button.new_with_label(_("Change API key"))
        change_key_btn.add_css_class("pill")
        change_key_btn.connect("clicked", lambda *args: self._show_no_key_page())
        retry_row.append(change_key_btn)
        box.append(retry_row)

        page.set_child(box)
        return page

    def _build_main_page(self) -> Gtk.Widget:
        """Основной интерфейс: toolbar + paned (sessions | conversation)."""
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        outer.set_hexpand(True)
        outer.set_vexpand(True)

        # -- Toolbar -----------------------------------------------------------
        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        toolbar.set_margin_top(8)
        toolbar.set_margin_bottom(6)
        toolbar.set_margin_start(8)
        toolbar.set_margin_end(8)

        model_label = Gtk.Label(label=_("Model"))
        model_label.add_css_class("dim-label")
        toolbar.append(model_label)

        self._model_dropdown = Gtk.DropDown()
        self._model_dropdown.set_hexpand(True)
        self._model_dropdown.connect(
            "notify::selected", self._on_model_dropdown_changed
        )
        toolbar.append(self._model_dropdown)

        self._folder_btn = Gtk.Button.new_with_label(_("Output folder…"))
        self._folder_btn.set_tooltip_text(_("Choose where generated images are saved"))
        self._folder_btn.connect("clicked", lambda *args: self._pick_output_dir())
        toolbar.append(self._folder_btn)

        key_btn = Gtk.Button.new_from_icon_name("dialog-password-symbolic")
        key_btn.set_tooltip_text(_("Change API key"))
        key_btn.connect("clicked", lambda *args: self._show_no_key_page())
        toolbar.append(key_btn)

        outer.append(toolbar)

        self._folder_hint = Gtk.Label()
        self._folder_hint.add_css_class("dim-label")
        self._folder_hint.add_css_class("caption")
        self._folder_hint.set_halign(Gtk.Align.START)
        self._folder_hint.set_margin_start(12)
        self._folder_hint.set_margin_end(12)
        self._folder_hint.set_margin_bottom(4)
        self._folder_hint.set_ellipsize(3)
        outer.append(self._folder_hint)

        # -- Paned: sessions | conversation -----------------------------------
        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        paned.set_vexpand(True)
        paned.set_resize_start_child(False)
        paned.set_shrink_start_child(False)
        paned.set_position(260)

        # Левая колонка — список сессий + «новая сессия».
        sessions_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        sessions_box.set_size_request(240, -1)
        sessions_box.set_margin_top(6)
        sessions_box.set_margin_bottom(6)
        sessions_box.set_margin_start(6)
        sessions_box.set_margin_end(6)

        new_session_btn = Gtk.Button.new_with_label(_("New session"))
        new_session_btn.add_css_class("suggested-action")
        new_session_btn.connect("clicked", lambda *args: self._on_new_session())
        sessions_box.append(new_session_btn)

        self._sessions_scroller = Gtk.ScrolledWindow()
        self._sessions_scroller.set_policy(
            Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC
        )
        self._sessions_scroller.set_vexpand(True)
        self._sessions_listbox = Gtk.ListBox()
        self._sessions_listbox.add_css_class("boxed-list")
        self._sessions_listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self._sessions_listbox.connect("row-selected", self._on_session_row_selected)
        self._sessions_scroller.set_child(self._sessions_listbox)
        sessions_box.append(self._sessions_scroller)

        paned.set_start_child(sessions_box)

        # Правая колонка — заголовок сессии, история, prompt-инпут.
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        # Заголовок сессии: имя + меню (переименовать/удалить).
        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        head.set_margin_top(6)
        head.set_margin_start(12)
        head.set_margin_end(12)
        head.set_margin_bottom(2)
        self._session_title = Gtk.Label()
        self._session_title.add_css_class("title-3")
        self._session_title.set_halign(Gtk.Align.START)
        self._session_title.set_hexpand(True)
        self._session_title.set_ellipsize(3)
        head.append(self._session_title)

        rename_btn = Gtk.Button.new_from_icon_name("document-edit-symbolic")
        rename_btn.add_css_class("flat")
        rename_btn.set_tooltip_text(_("Rename session"))
        rename_btn.connect("clicked", lambda *args: self._rename_active_session())
        head.append(rename_btn)
        del_btn = Gtk.Button.new_from_icon_name("user-trash-symbolic")
        del_btn.add_css_class("flat")
        del_btn.set_tooltip_text(_("Delete session"))
        del_btn.connect("clicked", lambda *args: self._delete_active_session())
        head.append(del_btn)
        right.append(head)

        # Подсказка под заголовком: «Сессия пуста — введите prompt».
        self._session_hint = Gtk.Label()
        self._session_hint.add_css_class("dim-label")
        self._session_hint.set_halign(Gtk.Align.START)
        self._session_hint.set_margin_start(12)
        self._session_hint.set_margin_end(12)
        self._session_hint.set_margin_bottom(4)
        right.append(self._session_hint)

        # Прокручиваемая лента сообщений.
        self._conv_scroller = Gtk.ScrolledWindow()
        self._conv_scroller.set_vexpand(True)
        self._conv_scroller.set_policy(
            Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC
        )
        self._conv_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self._conv_box.set_margin_top(4)
        self._conv_box.set_margin_bottom(8)
        self._conv_box.set_margin_start(12)
        self._conv_box.set_margin_end(12)
        self._conv_scroller.set_child(self._conv_box)
        right.append(self._conv_scroller)

        # Inline-прогресс генерации.
        self._gen_progress = Gtk.ProgressBar()
        self._gen_progress.set_visible(False)
        self._gen_progress.set_margin_start(12)
        self._gen_progress.set_margin_end(12)
        right.append(self._gen_progress)

        # Prompt + кнопка генерации.
        prompt_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        prompt_card.set_margin_top(6)
        prompt_card.set_margin_bottom(10)
        prompt_card.set_margin_start(12)
        prompt_card.set_margin_end(12)
        prompt_card.add_css_class("card")

        self._prompt_view = Gtk.TextView()
        self._prompt_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self._prompt_view.set_top_margin(8)
        self._prompt_view.set_bottom_margin(8)
        self._prompt_view.set_left_margin(8)
        self._prompt_view.set_right_margin(8)
        self._prompt_view.set_accepts_tab(False)
        prompt_scroll = Gtk.ScrolledWindow()
        prompt_scroll.set_min_content_height(70)
        prompt_scroll.set_max_content_height(140)
        prompt_scroll.set_propagate_natural_height(True)
        prompt_scroll.set_child(self._prompt_view)
        prompt_card.append(prompt_scroll)

        actions_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        actions_row.set_margin_start(6)
        actions_row.set_margin_end(6)
        actions_row.set_margin_bottom(6)

        self._use_prev_check = Gtk.CheckButton.new_with_label(
            _("Use previous image as reference")
        )
        self._use_prev_check.set_active(True)
        self._use_prev_check.set_tooltip_text(
            _("When checked, the latest image of this session is sent along with your "
              "prompt so the model edits it instead of starting from scratch.")
        )
        actions_row.append(self._use_prev_check)

        self._gen_btn = Gtk.Button.new_with_label(_("Generate"))
        self._gen_btn.add_css_class("suggested-action")
        self._gen_btn.set_hexpand(True)
        self._gen_btn.set_halign(Gtk.Align.END)
        self._gen_btn.connect("clicked", lambda *args: self._on_generate_clicked())
        actions_row.append(self._gen_btn)

        prompt_card.append(actions_row)
        right.append(prompt_card)

        paned.set_end_child(right)
        outer.append(paned)
        return outer

    # -- Models / key handling -----------------------------------------------

    def _show_no_key_page(self) -> None:
        """Открыть страницу ввода ключа, очистив поле и скрыв символы.

        Поле НЕ префилим существующим ключом — это плохая практика
        безопасности (пароль рядом с глазиком «показать»). Юзер должен
        либо снова ввести тот же ключ, либо новый.
        """
        self._no_key_entry.set_text("")
        self._no_key_entry.set_visibility(False)
        self._no_key_entry.set_icon_from_icon_name(
            Gtk.EntryIconPosition.SECONDARY, "view-conceal-symbolic"
        )
        self._stack.set_visible_child_name("no-key")
        self._no_key_entry.grab_focus()

    def _toggle_key_visibility(
        self, entry: Gtk.Entry, _pos: Gtk.EntryIconPosition
    ) -> None:
        """Глазик в правом краю поля ключа — показать/спрятать символы."""
        shown = not entry.get_visibility()
        entry.set_visibility(shown)
        entry.set_icon_from_icon_name(
            Gtk.EntryIconPosition.SECONDARY,
            "view-reveal-symbolic" if shown else "view-conceal-symbolic",
        )

    def _on_save_no_key_clicked(self, _button: Gtk.Button) -> None:
        key = self._no_key_entry.get_text().strip()
        if not key:
            return
        self._settings.openrouter_api_key = key
        save_settings(self._settings)
        self._start_load_models()

    def _start_load_models(self) -> None:
        """Уйти в фон, забрать каталог моделей, обновить дропдаун."""
        if self._busy:
            return
        self._busy = True
        self._stack.set_visible_child_name("loading")
        key = self._settings.openrouter_api_key

        def worker() -> None:
            try:
                models = list_image_models(key)
                err: str | None = None
            except OpenRouterError as exc:
                models, err = [], str(exc)
            GLib.idle_add(self._on_models_loaded, models, err)

        threading.Thread(target=worker, daemon=True).start()

    def _on_models_loaded(self, models: list[AIModel], err: str | None) -> bool:
        self._busy = False
        if err is not None:
            self._error_detail.set_text(err)
            self._stack.set_visible_child_name("error")
            return False
        if not models:
            self._error_detail.set_text(
                _("OpenRouter did not return any image-capable models for this key.")
            )
            self._stack.set_visible_child_name("error")
            return False
        self._models = models
        self._populate_model_dropdown()
        self._stack.set_visible_child_name("main")
        self._update_folder_hint()
        self._update_action_state()
        return False

    def _populate_model_dropdown(self) -> None:
        store = Gtk.StringList.new([m.name for m in self._models])
        self._model_dropdown.set_model(store)
        # Восстанавливаем последнюю выбранную модель, иначе берём первую.
        target = self._settings.last_ai_model
        chosen_index = 0
        if target:
            for i, m in enumerate(self._models):
                if m.id == target:
                    chosen_index = i
                    break
        self._model_dropdown.set_selected(chosen_index)

    def _on_model_dropdown_changed(self, *_args) -> None:
        idx = self._model_dropdown.get_selected()
        if 0 <= idx < len(self._models):
            self._settings.last_ai_model = self._models[idx].id
            save_settings(self._settings)
            if self._active is not None and not self._active.messages:
                # Только для пустой сессии — иначе модель должна остаться той,
                # с которой реально велась переписка.
                self._active.model = self._models[idx].id
                save_all(self._sessions)

    # -- Output folder --------------------------------------------------------

    def _effective_output_dir(self) -> Path:
        """Текущая папка для сохранения — настройка или дефолт ``~/Pictures``."""
        cfg = self._settings.ai_output_dir.strip()
        return Path(cfg) if cfg else _default_output_dir()

    def _update_folder_hint(self) -> None:
        self._folder_hint.set_text(
            _("Saves to: {path}").format(path=str(self._effective_output_dir()))
        )

    def _pick_output_dir(self) -> None:
        dialog = Gtk.FileDialog()
        dialog.set_title(_("Choose output folder for AI images"))
        current = self._effective_output_dir()
        if current.exists():
            dialog.set_initial_folder(Gio.File.new_for_path(str(current)))
        dialog.select_folder(self.get_root(), None, self._on_output_dir_picked)

    def _on_output_dir_picked(self, dialog: Gtk.FileDialog, result) -> None:
        try:
            gfile = dialog.select_folder_finish(result)
        except GLib.Error:
            return
        path = Path(gfile.get_path())
        path.mkdir(parents=True, exist_ok=True)
        self._settings.ai_output_dir = str(path)
        save_settings(self._settings)
        self._update_folder_hint()
        # Сообщаем главному окну — пусть Search его проиндексирует.
        self.emit("output-folder-ready", str(path))

    # -- Sessions list --------------------------------------------------------

    def _rebuild_sessions_list(self) -> None:
        if not hasattr(self, "_sessions_listbox"):
            return  # main UI ещё не построена (нет ключа)
        child = self._sessions_listbox.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self._sessions_listbox.remove(child)
            child = nxt
        active_row: Gtk.ListBoxRow | None = None
        for session in self._sessions:
            row = Adw.ActionRow()
            row.set_title(session.name)
            count = len(session.messages)
            row.set_subtitle(
                _("{n} message").format(n=count) if count == 1
                else _("{n} messages").format(n=count)
            )
            row.session = session  # type: ignore[attr-defined]
            self._sessions_listbox.append(row)
            if self._active is not None and session.id == self._active.id:
                active_row = row
        if active_row is not None:
            self._sessions_listbox.select_row(active_row)

    def _on_session_row_selected(self, _listbox, row) -> None:
        if row is None:
            return
        session: AISession | None = getattr(row, "session", None)
        if session is None or session is self._active:
            return
        self._active = session
        # Переключаем дропдаун на модель сессии, если она в каталоге.
        if self._models:
            for i, m in enumerate(self._models):
                if m.id == session.model:
                    self._model_dropdown.set_selected(i)
                    break
        self._rebuild_conversation()
        self._update_action_state()

    def _current_model_id(self) -> str:
        """Какую модель сейчас собирается использовать UI для новой сессии."""
        idx = self._model_dropdown.get_selected()
        if 0 <= idx < len(self._models):
            return self._models[idx].id
        return self._settings.last_ai_model

    def _on_new_session(self) -> None:
        # Если есть пустая активная сессия — не плодим клоны, просто остаёмся.
        if self._active is not None and not self._active.messages:
            self._rebuild_conversation()
            return
        model = self._current_model_id()
        if not model:
            return
        name = self._next_session_name()
        session = AISession.new(name=name, model=model)
        self._sessions.insert(0, session)
        self._active = session
        save_all(self._sessions)
        self._rebuild_sessions_list()
        self._rebuild_conversation()
        self._update_action_state()

    def _next_session_name(self) -> str:
        n = len(self._sessions) + 1
        return _("Session {n}").format(n=n)

    def _rename_active_session(self) -> None:
        if self._active is None:
            return
        dialog = Adw.AlertDialog(
            heading=_("Rename session"),
            body=_("Choose a new name:"),
        )
        entry = Gtk.Entry()
        entry.set_text(self._active.name)
        dialog.set_extra_child(entry)
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("ok", _("Rename"))
        dialog.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("ok")

        def on_response(_d, response):
            if response != "ok":
                return
            new_name = entry.get_text().strip()
            if not new_name or self._active is None:
                return
            self._active.name = new_name
            self._active.auto_named = False
            touch(self._active)
            save_all(self._sessions)
            self._rebuild_sessions_list()

        dialog.connect("response", on_response)
        dialog.present(self.get_root())

    def _delete_active_session(self) -> None:
        if self._active is None:
            return
        target = self._active
        dialog = Adw.AlertDialog(
            heading=_("Delete this session?"),
            body=_("Generated image files on disk stay; only the chat history is removed."),
        )
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("delete", _("Delete"))
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")

        def on_response(_d, response):
            if response != "delete":
                return
            self._sessions = [s for s in self._sessions if s.id != target.id]
            self._active = self._sessions[0] if self._sessions else None
            save_all(self._sessions)
            self._rebuild_sessions_list()
            self._rebuild_conversation()
            self._update_action_state()

        dialog.connect("response", on_response)
        dialog.present(self.get_root())

    # -- Conversation rendering ----------------------------------------------

    def _rebuild_conversation(self) -> None:
        if not hasattr(self, "_conv_box"):
            return
        child = self._conv_box.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self._conv_box.remove(child)
            child = nxt
        if self._active is None:
            self._session_title.set_text(_("No session"))
            self._session_hint.set_text(
                _("Click “New session” on the left to start.")
            )
            return
        self._session_title.set_text(self._active.name)
        model_label = self._active.model or _("(model not set)")
        if self._active.messages:
            self._session_hint.set_text(
                _("Model: {model}").format(model=model_label)
            )
        else:
            self._session_hint.set_text(
                _("Empty session — type a prompt below and press Generate.")
            )
        for msg in self._active.messages:
            self._conv_box.append(self._make_message_card(msg))

    def _make_message_card(self, msg: AIMessage) -> Gtk.Widget:
        """Один блок: текст prompt-а + миниатюра результата."""
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        card.add_css_class("card")
        card.set_margin_top(2)
        card.set_margin_bottom(2)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        header.set_margin_top(8)
        header.set_margin_start(10)
        header.set_margin_end(10)
        prompt_label = Gtk.Label(label=msg.prompt or _("(empty prompt)"))
        prompt_label.add_css_class("heading")
        prompt_label.set_halign(Gtk.Align.START)
        prompt_label.set_xalign(0.0)
        prompt_label.set_wrap(True)
        prompt_label.set_hexpand(True)
        header.append(prompt_label)
        if msg.created_at:
            ts = Gtk.Label(label=msg.created_at)
            ts.add_css_class("caption")
            ts.add_css_class("dim-label")
            ts.set_valign(Gtk.Align.START)
            header.append(ts)
        card.append(header)

        if msg.text_reply:
            note = Gtk.Label(label=msg.text_reply)
            note.add_css_class("dim-label")
            note.set_wrap(True)
            note.set_halign(Gtk.Align.START)
            note.set_xalign(0.0)
            note.set_margin_start(10)
            note.set_margin_end(10)
            card.append(note)

        images_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        images_row.set_margin_start(10)
        images_row.set_margin_end(10)
        images_row.set_margin_bottom(10)
        for path_str in msg.image_paths:
            tile = self._make_image_tile(Path(path_str))
            images_row.append(tile)
        card.append(images_row)
        return card

    def _make_image_tile(self, path: Path) -> Gtk.Widget:
        """Карточка с миниатюрой + подписью имени файла (клик — открыть в проводнике)."""
        wrapper = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        pic = Gtk.Picture()
        pic.set_size_request(GALLERY_THUMB, GALLERY_THUMB)
        pic.set_can_shrink(True)
        pic.set_content_fit(Gtk.ContentFit.CONTAIN)
        pic.add_css_class("card")
        wrapper.append(pic)

        name = Gtk.Label(label=path.name)
        name.add_css_class("caption")
        name.add_css_class("dim-label")
        name.set_ellipsize(3)
        name.set_max_width_chars(28)
        wrapper.append(name)

        def apply(tex, _pic=pic):
            if tex is not None:
                _pic.set_paintable(tex)

        if path.exists():
            self._loader.get(path, GALLERY_THUMB, apply)
        else:
            pic.set_paintable(None)

        # Клик по миниатюре — открыть в проводнике, как в Search.
        gesture = Gtk.GestureClick.new()
        gesture.set_button(1)

        def on_pressed(_g, n_press, _x, _y, _p=path):
            if n_press == 2:
                open_in_file_manager(_p)

        gesture.connect("pressed", on_pressed)
        wrapper.add_controller(gesture)
        return wrapper

    # -- Generation -----------------------------------------------------------

    def _on_generate_clicked(self) -> None:
        if self._busy:
            return
        if self._active is None:
            self._on_new_session()
            if self._active is None:
                return
        buf = self._prompt_view.get_buffer()
        prompt = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), True).strip()
        if not prompt:
            self._toast(_("Type a prompt first."))
            return
        if not self._settings.openrouter_api_key.strip():
            self._stack.set_visible_child_name("no-key")
            return

        # Если сессия пустая — закрепляем за ней текущую модель.
        chosen_model = self._current_model_id()
        if not chosen_model:
            self._toast(_("Pick a model first."))
            return
        if not self._active.messages:
            self._active.model = chosen_model

        # Подбираем reference-картинку: последний результат сессии.
        reference: list[Path] = []
        if (
            self._use_prev_check.get_active()
            and self._active.messages
            and self._active.messages[-1].image_paths
        ):
            last = Path(self._active.messages[-1].image_paths[-1])
            if last.exists():
                reference = [last]

        self._busy = True
        self._gen_btn.set_sensitive(False)
        self._gen_progress.set_visible(True)
        self._gen_progress.pulse()
        self._pulse_id = GLib.timeout_add(120, self._pulse_progress)

        out_dir = self._effective_output_dir()
        api_key = self._settings.openrouter_api_key
        model = self._active.model or chosen_model
        # История для OpenRouter: только текстовые prompt-ы прошлых сообщений
        # + один последний картинку, чтобы не раздувать payload до десятков MB.
        history = self._build_history_for_request()

        def worker(
            _api_key=api_key,
            _model=model,
            _prompt=prompt,
            _refs=reference,
            _history=history,
            _out=out_dir,
        ) -> None:
            try:
                image = generate_image(
                    api_key=_api_key,
                    model=_model,
                    prompt=_prompt,
                    reference_images=_refs,
                    history=_history,
                )
                saved = save_image_bytes(image, _out, prompt_hint=_prompt)
                err: str | None = None
            except OpenRouterError as exc:
                image, saved, err = None, None, str(exc)
            except OSError as exc:
                image, saved, err = None, None, _("Cannot save image: {err}").format(
                    err=exc
                )
            GLib.idle_add(
                self._on_generation_done, _prompt, _refs, image, saved, err
            )

        threading.Thread(target=worker, daemon=True).start()

    def _pulse_progress(self) -> bool:
        self._gen_progress.pulse()
        return self._busy  # ставим в False — таймер сам выпиливается

    def _build_history_for_request(self) -> list[dict]:
        """Подготовить сжатую историю переписки для следующего запроса.

        Текстовые пары «user prompt → assistant reply» отдаём целиком, чтобы
        модель сохраняла контекст беседы. Картинки прошлых ответов в историю
        НЕ кладём — самая свежая всё равно идёт как ``reference_images``,
        старые только раздуют запрос.
        """
        history: list[dict] = []
        if self._active is None:
            return history
        for m in self._active.messages:
            history.append({"role": "user", "content": m.prompt})
            if m.text_reply:
                history.append({"role": "assistant", "content": m.text_reply})
        return history

    def _on_generation_done(
        self,
        prompt: str,
        references: list[Path],
        image: GeneratedImage | None,
        saved_path: Path | None,
        err: str | None,
    ) -> bool:
        self._busy = False
        self._gen_progress.set_visible(False)
        if hasattr(self, "_pulse_id"):
            with contextlib.suppress(Exception):
                GLib.source_remove(self._pulse_id)
            del self._pulse_id
        self._gen_btn.set_sensitive(True)
        if err is not None or image is None or saved_path is None:
            self._show_error(_("Generation failed"), err or _("Unknown error"))
            return False
        if self._active is None:
            return False
        msg = AIMessage(
            prompt=prompt,
            image_paths=[str(saved_path)],
            created_at=datetime.now().isoformat(timespec="seconds"),
            text_reply=image.text_reply,
            reference_paths=[str(p) for p in references],
        )
        self._active.messages.append(msg)
        touch(self._active)
        # Если сессия безымянная и это первый запрос — переименуем по prompt-у.
        if len(self._active.messages) == 1 and self._active.auto_named:
            short = prompt.strip().splitlines()[0][:40].strip() or self._active.name
            self._active.name = short
            self._active.auto_named = False
        save_all(self._sessions)
        self._rebuild_sessions_list()
        # Дочерчиваем — только новую карточку, не пересобираем всё.
        self._conv_box.append(self._make_message_card(msg))
        GLib.idle_add(self._scroll_conversation_to_bottom)
        # Очистим поле ввода — следующий prompt пишем с нуля.
        self._prompt_view.get_buffer().set_text("", -1)
        # Сообщаем главному окну: появилась новая папка/файл — пусть Search видит.
        self.emit("output-folder-ready", str(self._effective_output_dir()))
        return False

    def _scroll_conversation_to_bottom(self) -> bool:
        adj = self._conv_scroller.get_vadjustment()
        if adj is not None:
            adj.set_value(adj.get_upper())
        return False

    # -- Misc ----------------------------------------------------------------

    def _update_action_state(self) -> None:
        """Sensitivity кнопок в зависимости от состояния (модели/сессия/busy)."""
        if not hasattr(self, "_gen_btn"):
            return
        ready = (
            bool(self._models)
            and bool(self._settings.openrouter_api_key.strip())
            and not self._busy
        )
        self._gen_btn.set_sensitive(ready)
        # Папку можно выбрать всегда, чтобы юзер мог подготовиться заранее.
        self._folder_btn.set_sensitive(True)
        # Имя сессии и подсказка — освежим, если рендер главной страницы уже был.
        if self._active is not None:
            self._session_title.set_text(self._active.name)

    def _toast(self, message: str) -> None:
        logger.info(message)

    def _show_error(self, heading: str, body: str) -> None:
        dialog = Adw.AlertDialog(heading=heading, body=body)
        dialog.add_response("ok", _("OK"))
        dialog.set_default_response("ok")
        dialog.present(self.get_root())
