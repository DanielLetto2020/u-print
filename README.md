# u-print

**A GTK4 / libadwaita app for Linux that prints multiple photos per sheet
(N-up) and helps you find them in the first place.**

The Windows "Print Pictures" wizard with grid layouts has no comfortable
equivalent on GNOME. `u-print` (the desktop entry is named **PhotoPrint**)
fills that gap: index the folders where your photos actually live, search
across them, send a selection straight into a print-layout view, tune the
sheet, preview it, send to a CUPS printer or export as PDF.

Targets **Ubuntu 24.04 LTS** (GNOME 46, Wayland & X11). Builds and runs on
22.04+ with the same system packages.

> 🇷🇺 Документация на русском: [README.ru.md](./README.ru.md)
> Repository: <https://github.com/DanielLetto2020/u-print>
> License: [MIT](./LICENSE)

---

## Features

### Search tab — your photo library, indexed

- Add any number of folders to be watched; they are scanned recursively
  (`.Trash`, `.thumbnails`, dotfiles ignored).
- Metadata cached in SQLite (`~/.config/photoprint/photo_index.db`):
  path, size, mtime, dimensions, EXIF date — so a re-launch shows photos
  instantly without re-walking the filesystem.
- **Incremental scan**: photos appear in the UI *as the scanner finds them*,
  not at the end. Thousands of files don't lock up the window.
- **Virtual scrolling** with `Gtk.GridView` / `Gtk.ColumnView`: rendering
  cost depends on what's visible, not on the total. 15 000 photos behave
  the same as 150.
- **Lazy thumbnails** in a 4-thread pool with an LRU cache — main thread
  never blocks on PIL decoding.
- Case-insensitive search by filename (live).
- Two view modes: **Grid** (thumbnails) and **List** (sortable, columned
  table). In list mode every column header click sorts; a popover toggles
  which columns are visible (`Preview` / `Name` / `Date` / `Size` /
  `Resolution` are on by default; `Type` / `Folder` / `Path` / `Modified`
  hidden, one click away).
- **Selection tray**: as soon as you select anything, a strip with the
  picked thumbnails slides down above the results. Click `×` (or anywhere
  on a strip tile) to deselect; the broom button clears all.
- **Click toggles**: a single click adds or removes the photo from the
  selection — no need to hold Ctrl. Pick five photos across hundreds
  without losing the first four.
- **Send to Print** moves the selection into the Print tab.

### Print tab — pick a layout, preview, print

- Drag photos in from Files (Nautilus), the *Add photos…* button, or
  hand them over from Search. EXIF orientation is normalised at load.
- Grid presets: **1 / 2 (1×2 or 2×1) / 4 / 6 (2×3 or 3×2) / 8 (2×4 or
  4×2) / 9 / 16** photos per page.
- Paper sizes: A3, A4, A5, Letter, Legal, 10×15 cm, 13×18 cm, 20×30 cm.
- Orientation: portrait / landscape / **auto** (picked from photo aspect
  + grid mean fit).
- Fit modes: **Fit** (contain, white margins), **Fill** (cover, crops
  centred), **Stretch**.
- Configurable margins per side (mm), gutter between cells, photo border
  (point width + colour), caption under the photo (filename or EXIF
  date), auto-rotate to maximise cell coverage.
- Live preview via `pypdfium2`: the real PDF is rendered at a low DPI
  and rasterised straight into a `Gdk.Texture`.
- **Save as PDF…** and **Print…** (the latter opens a printer / copies
  / colour / quality dialog backed by CUPS).
- **Named presets**: save the current paper + grid + margins set as
  e.g. "Passport 4×6" and apply later in a click.

### Across both tabs

- Live language switch between English (default), Russian and Auto
  (system locale) — via the hamburger menu, restart applies.
- Session restore on next launch when the last batch is non-empty.
- Common shortcuts: `Ctrl+O` (add), `Ctrl+P` (print), `Ctrl+S` (save PDF),
  `Ctrl+A` (select all), `Delete` (remove selection).
- Follows the system light/dark theme via libadwaita.

## Screenshots

> *Add screenshots here on first release — a clean one of the Search tab
> with photos loaded, one of the Print tab with a 2×2 layout previewing,
> and the language menu open.*

---

## Install

### From source (developer install)

Tested on Ubuntu 24.04. Python 3.12, GTK 4.14, libadwaita 1.5.

```bash
sudo apt install -y python3-gi python3-cups gir1.2-gtk-4.0 gir1.2-adw-1 \
                    libheif1 libheif-plugin-libde265 libcups2-dev \
                    python3-venv python3-pip

git clone https://github.com/DanielLetto2020/u-print.git
cd u-print

python3 -m venv --system-site-packages .venv
source .venv/bin/activate
pip install -e ".[dev]"

# Compile translations (pure-Python, no msgfmt required)
python po/build_mo.py

# Run from the project directory
python -m photoprint
```

Pass photo paths to preload them:

```bash
python -m photoprint /path/to/*.jpg
```

### As a desktop application (user install, no sudo)

```bash
# Create a launcher in ~/.local/bin and a .desktop entry in
# ~/.local/share/applications:
mkdir -p ~/.local/bin ~/.local/share/applications \
         ~/.local/share/icons/hicolor/scalable/apps \
         ~/.local/share/locale/ru/LC_MESSAGES

# Launcher
cat > ~/.local/bin/photoprint <<EOF
#!/usr/bin/env bash
exec "$(pwd)/.venv/bin/python" -m photoprint "\$@"
EOF
chmod +x ~/.local/bin/photoprint

# Desktop integration
install -m 644 data/desktop/io.github.photoprint.PhotoPrint.desktop \
               ~/.local/share/applications/io.github.photoprint.PhotoPrint.desktop
install -m 644 data/icons/io.github.photoprint.PhotoPrint.svg \
               ~/.local/share/icons/hicolor/scalable/apps/io.github.photoprint.PhotoPrint.svg
install -m 644 po/ru/LC_MESSAGES/photoprint.mo \
               ~/.local/share/locale/ru/LC_MESSAGES/photoprint.mo

# Refresh GNOME caches so the icon appears in Activities
update-desktop-database ~/.local/share/applications
gtk-update-icon-cache -t ~/.local/share/icons/hicolor
```

After this `photoprint` is on your `PATH` and **PhotoPrint** appears in
the Activities overview.

### System-wide install on Ubuntu

```bash
sudo packaging/install-deb.sh
```

The script creates `/opt/photoprint/` with a venv, links a launcher into
`/usr/local/bin/photoprint` and drops the `.desktop` / icon / translations
into `/usr/local/share/`. It's not a real `.deb`; for that, wrap this with
[`dh-virtualenv`](https://github.com/spotify/dh-virtualenv).

### Flatpak (recommended for distribution)

A manifest is provided at
[`packaging/io.github.photoprint.PhotoPrint.yml`](packaging/io.github.photoprint.PhotoPrint.yml).

```bash
flatpak install flathub org.gnome.Platform//46 org.gnome.Sdk//46
flatpak-builder --user --install --force-clean build-flatpak \
    packaging/io.github.photoprint.PhotoPrint.yml
flatpak run io.github.photoprint.PhotoPrint
```

Flatpak handles GTK runtime and isolates dependencies; CUPS is exposed
via `--socket=cups`.

---

## How to use it

1. **Launch** the app. The Print tab is shown by default. The header
   switches between **Search** and **Print**.
2. **Search** → **🗂 Folder icon** → **Add folder…** → pick e.g.
   `~/Pictures`. The indexer starts immediately, photos appear as they
   are found, the progress bar shows the current folder and counter.
3. Type a query in the search bar to filter by filename. Switch to
   **list view** to see a sortable table; the **⋯ button** next to the
   view toggle reveals the column-visibility popover.
4. Click photos to add or remove from the selection — the selection
   strip above the results shows the running pick.
5. **Send N to Print** moves the selection into the Print tab, populates
   the left photo list, and rebuilds the layout preview.
6. Tune the layout in the right sidebar — paper, grid, margins, fit
   mode, border, caption.
7. **Save PDF…** or **Print…** (CUPS dialog) — both work off the same
   rendered PDF at 300 DPI.

Tip: in the language menu (hamburger icon in the header) you can switch
between English / Русский / Auto; the new language applies after restart.

---

## Configuration files

All under `~/.config/photoprint/`:

| File              | Purpose                                                |
|-------------------|--------------------------------------------------------|
| `settings.json`   | Last printer, last layout, UI language                 |
| `presets.json`    | Named layout presets                                   |
| `session.json`    | Photos and layout of the last unfinished batch         |
| `photo_index.db`  | SQLite index of watched folders                        |

Delete a file to reset that aspect; the app re-creates it on next
launch.

---

## Architecture (high level)

```
photoprint/
├── core/
│   ├── layout.py        Pure planner: photos + params → LayoutPlan (mm)
│   ├── renderer.py      LayoutPlan + reportlab → PDF
│   ├── image_loader.py  PIL + EXIF + HEIC + thumbnail LRU
│   ├── photo_index.py   SQLite-backed folder index
│   ├── printer.py       pycups wrapper
│   └── settings.py      JSON persistence (~/.config/photoprint/)
├── ui/
│   ├── window.py        Adw.ApplicationWindow with Adw.ViewStack
│   ├── search_view.py   Search tab with GridView/ColumnView + tray
│   ├── photo_list.py    PrintView photo column
│   ├── sidebar.py       Layout settings
│   ├── preview.py       pypdfium2 → Gdk.Texture preview
│   ├── print_dialog.py  CUPS printer + options dialog
│   ├── thumbnail_loader.py  Thread-pool LRU loader
│   └── photo_item_model.py  GObject wrapper for Gio.ListStore
└── i18n.py              Gettext bootstrap + DEFAULT_LANGUAGE = "en"
```

Layout coordinates are in **millimetres, top-left origin**. The renderer
flips to PDF's bottom-left points only when emitting. Everything else in
the UI stays in mm.

---

## Translations

Strings are wrapped with `_()` from `photoprint.i18n`. Russian catalogue
ships in `po/ru/`. To compile:

```bash
python po/build_mo.py
```

To add a new language, copy `po/photoprint.pot` into
`po/<lang>/LC_MESSAGES/photoprint.po`, fill in `msgstr`, run the build
script again.

The active language is stored as `language` in `settings.json` and can
be changed at runtime through the hamburger menu (restart applies).

---

## Development

```bash
# Tests (31, fast)
pytest -q

# Lint + format
ruff check photoprint tests
ruff format photoprint tests
```

Tests cover the pure-function planner (`core/layout.py`), the PDF
renderer (`core/renderer.py`), and the SQLite index
(`core/photo_index.py`).

CI-friendly checks:

```bash
pytest -q && ruff check photoprint tests
```

---

## System dependencies — why each one

| Library             | What for                                  | Apt package                            |
|---------------------|-------------------------------------------|----------------------------------------|
| GTK 4 + libadwaita  | Window + widgets                          | `gir1.2-gtk-4.0`, `gir1.2-adw-1`       |
| PyGObject           | Python ↔ GTK bridge                       | `python3-gi`                           |
| pycups              | CUPS bindings (print, list printers)      | `python3-cups`                         |
| libheif             | HEIC / HEIF decoding                      | `libheif1`, `libheif-plugin-libde265`  |
| libcups2-dev        | Only if installing `pycups` from pip      | `libcups2-dev`                         |

PyGObject and pycups come from the system Python via
`--system-site-packages`. Everything else (`Pillow`, `pillow-heif`,
`reportlab`, `pypdfium2`, `platformdirs`) lives in the project venv.

---

## Roadmap

Curated next-step ideas live in [`idea.txt`](./idea.txt) — 20 entries
covering passport-photo templates, crop marks, ICC profiles, soft
proofing, per-photo cropping editor and so on. Priorities are at the
bottom of that file.

If you implement one of them, the recommended workflow is:

1. Create a feature branch off `main`.
2. Keep commit messages in Russian (the project conventions) or English —
   whichever is consistent with what's already in `git log`.
3. Run `pytest && ruff check` before pushing.
4. Open a PR.

---

## Gotchas worth flagging

- **Wayland drag-and-drop** uses `Gtk.DropTarget` with `Gdk.FileList`,
  not the old `Gtk.TargetList`.
- **EXIF orientation** is normalised at load via
  `PIL.ImageOps.exif_transpose`; the planner sees already-oriented
  dimensions.
- **DPI sanity**: photos are downscaled to no more than 300 DPI at their
  printed size before being embedded in the PDF — a 50 MP DSLR shot put
  into a 5×7 cm cell does not bloat the file or stall the printer.
- **PDF coordinates** are bottom-left; conversion is centralised in
  `core/renderer.py`.
- **SQLite + threads**: the photo index opens its connection with
  `check_same_thread=False`. The scanner runs in a worker thread; the UI
  performs queries on the main thread. No concurrent writers — the UI
  disables the Rescan button while a scan is in progress.

---

## License

MIT — see [LICENSE](./LICENSE). Translations are derivative under the
same terms.
