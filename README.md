# PhotoPrint

A GTK4 / libadwaita app for **N-up photo printing** on Linux — pick a batch of
photos, choose a grid (1, 2, 4, 6, 8, 9 or 16 per sheet), tune margins / fit
mode / borders / captions, preview the result page-by-page, and send it to a
CUPS printer (or export as a PDF).

Targets Ubuntu 24.04 (GNOME 46, Wayland & X11). Also works on 22.04.

> 🇷🇺 Документация на русском: [README.ru.md](./README.ru.md)

---

## Features

- Drag-and-drop photo grid with thumbnails, multi-select, reorder, removal.
- Grid presets 1×1 / 1×2 / 2×1 / 2×2 / 2×3 / 3×2 / 2×4 / 4×2 / 3×3 / 4×4.
- Paper sizes A3 / A4 / A5 / Letter / Legal / 10×15 / 13×18 / 20×30 cm.
- Fit modes: **fit** (contain), **fill** (cover, crops), **stretch**.
- Configurable margins per side (mm), gutter, photo border, caption.
- Auto-orientation and auto-rotate to maximise cell coverage.
- Live preview powered by `pypdfium2` (the actual print PDF rendered at lower DPI).
- HEIC / HEIF support via `libheif` + `pillow-heif`; EXIF orientation honoured.
- CUPS printing with copies / colour / quality; “Save as PDF” export.
- Named presets, session restore on next launch, common shortcuts
  (`Ctrl+O`, `Ctrl+P`, `Ctrl+S`, `Ctrl+A`, `Delete`).
- libadwaita native look — follows the system light/dark theme.
- English + Russian (`gettext`).

## Architecture

Pure-function core (no GUI dependency) so it can be tested in isolation:

```
photoprint/
├── core/
│   ├── layout.py        # planner: photos + params → LayoutPlan (mm)
│   ├── renderer.py      # LayoutPlan + reportlab → PDF
│   ├── image_loader.py  # PIL + EXIF + HEIC + thumbnail LRU cache
│   ├── printer.py       # pycups wrapper
│   └── settings.py      # JSON persistence (~/.config/photoprint/)
├── ui/                  # GTK4 / Adw widgets
└── i18n.py              # gettext bootstrap
```

Layout uses millimetres with a top-left origin. The renderer flips that into
PDF's bottom-left points only when emitting. This keeps every coordinate
sensible in the GUI side.

## Quick start (development, Ubuntu 24.04)

```bash
sudo apt install -y python3-gi python3-cups gir1.2-gtk-4.0 gir1.2-adw-1 \
                    libheif1 libheif-plugin-libde265 libcups2-dev

python3 -m venv --system-site-packages .venv
source .venv/bin/activate
pip install -e ".[dev]"

# Compile translation catalogues (pure-Python script, no msgfmt needed):
python po/build_mo.py

# Run
python -m photoprint              # or just `photoprint` after install
python -m photoprint *.jpg        # preload files
```

Run the test suite:

```bash
pytest -q
ruff check photoprint tests
```

### System dependencies — why

| Library             | What for                                  | Apt package                      |
|---------------------|-------------------------------------------|----------------------------------|
| GTK 4 + libadwaita  | Window + widgets                          | `gir1.2-gtk-4.0`, `gir1.2-adw-1` |
| PyGObject           | Python ↔ GTK bridge                       | `python3-gi`                     |
| pycups              | CUPS bindings (print, list printers)      | `python3-cups`                   |
| libheif             | HEIC / HEIF decoding                      | `libheif1`, `libheif-plugin-libde265` |
| libcups2-dev        | Only if installing pycups from pip        | `libcups2-dev`                   |

PyGObject and pycups are pulled in from the system Python via
`--system-site-packages`. Everything else (`Pillow`, `pillow-heif`,
`reportlab`, `pypdfium2`, `platformdirs`) is installed in the venv.

## Packaging

Two recommended routes — pick one:

### 1) Flatpak  *(recommended for distribution)*

A manifest is provided at
[`packaging/io.github.photoprint.PhotoPrint.yml`](packaging/io.github.photoprint.PhotoPrint.yml).
Flatpak handles the GTK runtime and isolates dependencies; CUPS is exposed
via `--socket=cups`.

```bash
flatpak install flathub org.gnome.Platform//46 org.gnome.Sdk//46
flatpak-builder --user --install --force-clean build-flatpak \
    packaging/io.github.photoprint.PhotoPrint.yml
flatpak run io.github.photoprint.PhotoPrint
```

> Why Flatpak over PyInstaller? PyInstaller has poor support for GObject
> Introspection and GTK4 typelibs — getting libadwaita to load in a frozen
> binary is fragile. Flatpak ships a proper GNOME runtime where GTK4 / Adw
> "just work".

### 2) Plain install on Ubuntu

```bash
sudo packaging/install-deb.sh
```

That installs a venv into `/opt/photoprint`, links `photoprint` into
`/usr/local/bin`, and drops the `.desktop` + icon + translations into
`/usr/local/share/`. It's not a proper `.deb`, but it's the same content one
would put in a `.deb` — a real one is easy with
[`dh-virtualenv`](https://github.com/spotify/dh-virtualenv) on top of this.

## Gotchas worth flagging

- **Wayland DnD** uses `Gtk.DropTarget` with `Gdk.FileList`, not the old
  `Gtk.TargetList` — handled.
- **EXIF orientation** is normalised at load time via
  `PIL.ImageOps.exif_transpose`; the planner sees already-oriented dimensions.
- **DPI sanity**: photos are downscaled to no more than 300 DPI at their
  printed size before being embedded in the PDF — a 50 MP DSLR shot put into
  a 5×7 cm cell does not bloat the file or stall the printer.
- **PDF coordinates** are bottom-left; conversion is centralised in
  `core/renderer.py`.

## License

MIT — see source headers. Translations are derivative under the same terms.
