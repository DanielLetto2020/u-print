"""Application entry point."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gio  # noqa: E402

from photoprint.ui.window import MainWindow  # noqa: E402

APP_ID = "io.github.photoprint.PhotoPrint"


class PhotoPrintApp(Adw.Application):
    """Application object: one window per launch."""

    def __init__(self) -> None:
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_OPEN)
        self.connect("activate", self._on_activate)
        self.connect("open", self._on_open)

    def _on_activate(self, _app):
        win = self.props.active_window or MainWindow(self)
        win.present()

    def _on_open(self, _app, files, _n_files, _hint):
        win = self.props.active_window or MainWindow(self)
        paths = [Path(f.get_path()) for f in files if f.get_path()]
        if paths:
            win.add_photos(paths)
        win.present()


def main(argv: list[str] | None = None) -> int:
    """CLI entry point referenced by ``[project.scripts]``."""
    parser = argparse.ArgumentParser(description="PhotoPrint — N-up photo printing")
    parser.add_argument("-v", "--verbose", action="store_true", help="enable debug logging")
    parser.add_argument("files", nargs="*", help="photos to load on startup")
    args, gtk_argv = parser.parse_known_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    app = PhotoPrintApp()
    if args.files:
        app.register()
        gio_files = [Gio.File.new_for_path(str(Path(p).resolve())) for p in args.files]
        app.open(gio_files, "")
        return app.run(gtk_argv)
    return app.run([sys.argv[0], *gtk_argv])


if __name__ == "__main__":
    raise SystemExit(main())
