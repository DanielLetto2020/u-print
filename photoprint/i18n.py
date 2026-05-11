"""Translation setup.

Each UI module does ``from photoprint.i18n import gettext as _`` and wraps
user-visible strings as ``_("…")``. The lookup is dispatched through this
module's :func:`gettext`, which calls into the currently-installed
``Translation`` object. Re-calling :func:`install` swaps catalogues
transparently for any modules that imported ``gettext``.

Language selection priority:
1. ``language`` field in ``~/.config/photoprint/settings.json`` (if not ``auto``).
2. The ``LANGUAGE`` / ``LANG`` environment variables.
3. Hard default: English.

Catalogue search path: the source tree's ``po/`` directory (handy during
development), then ``~/.local/share/locale``, then the system locations.
"""

from __future__ import annotations

import gettext as _gettext
import os
from pathlib import Path

DOMAIN = "photoprint"
DEFAULT_LANGUAGE = "en"

# Active translation. Updated by install(). Indirected through this module
# so all `from photoprint.i18n import gettext as _` callers re-read it on
# every translation call.
_translation: _gettext.NullTranslations = _gettext.NullTranslations()


def _candidate_locale_dirs() -> list[Path]:
    """Locations to search for compiled ``.mo`` files."""
    here = Path(__file__).resolve().parent
    return [
        here.parent / "po",                                # repo checkout
        Path.home() / ".local" / "share" / "locale",       # XDG user
        Path("/usr/local/share/locale"),                   # /usr/local
        Path("/usr/share/locale"),                         # system
    ]


def _settings_language() -> str:
    """Read the saved language from ``settings.json`` without raising."""
    try:
        from photoprint.core.settings import load_settings

        return (load_settings().language or "").strip()
    except Exception:  # noqa: BLE001 — never block i18n setup on settings IO
        return ""


def _resolve_language() -> str:
    """Return the effective language code."""
    lang = _settings_language()
    if lang and lang != "auto":
        return lang
    env = os.environ.get("LANGUAGE") or os.environ.get("LANG", "")
    if env:
        env = env.split(".", 1)[0].split("_", 1)[0]
        if env:
            return env
    return DEFAULT_LANGUAGE


def install() -> str:
    """Load the resolved catalogue and store it as the active translation.

    Returns the language code that was actually selected (useful for tests
    and logging).
    """
    global _translation
    lang = _resolve_language()
    # "en" is the source language — use identity translation, no .mo lookup.
    if lang.startswith("en"):
        _translation = _gettext.NullTranslations()
        _translation.install()
        return lang
    for d in _candidate_locale_dirs():
        if not d.exists():
            continue
        try:
            _translation = _gettext.translation(
                DOMAIN, localedir=str(d), languages=[lang], fallback=False
            )
            _translation.install()
            return lang
        except FileNotFoundError:
            continue
    # No catalogue found — fall back to identity.
    _translation = _gettext.NullTranslations()
    _translation.install()
    return lang


def gettext(message: str) -> str:
    """Translate ``message`` via the currently-installed catalogue."""
    return _translation.gettext(message)


# Eager install so simply importing the package wires up translations.
install()
