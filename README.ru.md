# PhotoPrint

Приложение на GTK4 / libadwaita для **N-up печати фото** в Linux. Выберите
пакет фото, выберите сетку (1, 2, 4, 6, 8, 9 или 16 на лист), задайте поля
/ режим заполнения / рамки / подписи, посмотрите превью каждой страницы и
отправьте на принтер CUPS (или экспортируйте в PDF).

Целевая платформа — Ubuntu 24.04 (GNOME 46, Wayland & X11). Работает и
на 22.04.

> 🇬🇧 English docs: [README.md](./README.md)

---

## Возможности

- Перетаскивание фото мышью с превью-миниатюрами, мульти-выделение,
  переупорядочивание, удаление.
- Сетки 1×1 / 1×2 / 2×1 / 2×2 / 2×3 / 3×2 / 2×4 / 4×2 / 3×3 / 4×4.
- Размеры бумаги A3 / A4 / A5 / Letter / Legal / 10×15 / 13×18 / 20×30 см.
- Режимы заполнения: **Вписать**, **Заполнить** (с обрезкой),
  **Растянуть**.
- Настраиваемые поля по сторонам (мм), gutter между ячейками, рамка,
  подпись.
- Авто-ориентация листа и авто-поворот фото под ячейку.
- Превью через `pypdfium2` (тот же PDF, что и при печати, только меньший DPI).
- HEIC / HEIF через `libheif` + `pillow-heif`; EXIF-ориентация учитывается.
- Печать через CUPS с настройкой копий / цвета / качества; экспорт в PDF.
- Именованные пресеты, восстановление сессии, горячие клавиши
  (`Ctrl+O`, `Ctrl+P`, `Ctrl+S`, `Ctrl+A`, `Delete`).
- Нативный внешний вид libadwaita — следует системной светлой/тёмной теме.
- Английский и русский (`gettext`).

## Архитектура

Ядро — чистые функции без GUI-зависимостей, легко тестируется:

```
photoprint/
├── core/
│   ├── layout.py        # планировщик: фото + параметры → LayoutPlan (мм)
│   ├── renderer.py      # LayoutPlan + reportlab → PDF
│   ├── image_loader.py  # PIL + EXIF + HEIC + кэш миниатюр
│   ├── printer.py       # обёртка над pycups
│   └── settings.py      # JSON-настройки (~/.config/photoprint/)
├── ui/                  # виджеты GTK4 / Adw
└── i18n.py              # инициализация gettext
```

Логика раскладки оперирует мм с началом координат в верхнем-левом углу.
Перевод в PDF (точки, нижний-левый угол) делает только рендерер.

## Быстрый старт (Ubuntu 24.04)

```bash
sudo apt install -y python3-gi python3-cups gir1.2-gtk-4.0 gir1.2-adw-1 \
                    libheif1 libheif-plugin-libde265 libcups2-dev

python3 -m venv --system-site-packages .venv
source .venv/bin/activate
pip install -e ".[dev]"

# Скомпилировать .po → .mo (чистый Python, msgfmt не нужен):
python po/build_mo.py

# Запуск
python -m photoprint                # или просто `photoprint` после установки
python -m photoprint *.jpg          # с файлами сразу
```

Тесты и линт:

```bash
pytest -q
ruff check photoprint tests
```

### Зачем нужны системные пакеты

| Библиотека          | Назначение                                  | Пакет apt                        |
|---------------------|---------------------------------------------|----------------------------------|
| GTK 4 + libadwaita  | Окно + виджеты                              | `gir1.2-gtk-4.0`, `gir1.2-adw-1` |
| PyGObject           | Питон ↔ GTK мост                            | `python3-gi`                     |
| pycups              | Бинды CUPS (печать, список принтеров)       | `python3-cups`                   |
| libheif             | Декодирование HEIC / HEIF                   | `libheif1`, `libheif-plugin-libde265` |
| libcups2-dev        | Только если ставить pycups через pip        | `libcups2-dev`                   |

PyGObject и pycups берутся из системного Python через
`--system-site-packages`. Остальное (`Pillow`, `pillow-heif`,
`reportlab`, `pypdfium2`, `platformdirs`) ставится в venv.

## Сборка

Два рекомендованных пути:

### 1) Flatpak — рекомендую для распространения

Манифест: [`packaging/io.github.photoprint.PhotoPrint.yml`](packaging/io.github.photoprint.PhotoPrint.yml).
Flatpak подтягивает GTK-runtime и изолирует зависимости; CUPS пробрасывается
через `--socket=cups`.

```bash
flatpak install flathub org.gnome.Platform//46 org.gnome.Sdk//46
flatpak-builder --user --install --force-clean build-flatpak \
    packaging/io.github.photoprint.PhotoPrint.yml
flatpak run io.github.photoprint.PhotoPrint
```

> Почему Flatpak, а не PyInstaller? У PyInstaller плохо с GObject
> Introspection и typelib для GTK4 — упаковать libadwaita в один бинарь
> хрупко и тяжело сопровождать. Flatpak ставит честный GNOME runtime, где
> GTK4 и Adw работают сразу.

### 2) Установка на Ubuntu без сборки .deb

```bash
sudo packaging/install-deb.sh
```

Скрипт создаёт venv в `/opt/photoprint`, кладёт `photoprint` в
`/usr/local/bin`, ставит `.desktop`-файл, иконку и переводы в
`/usr/local/share/`. Это не настоящий `.deb`, но содержимое то же — поверх
этого легко собрать честный deb с помощью
[`dh-virtualenv`](https://github.com/spotify/dh-virtualenv).

## Подводные камни

- **Drag-and-drop в Wayland** работает только через `Gtk.DropTarget` с
  `Gdk.FileList`, не через старый `Gtk.TargetList` — учтено.
- **EXIF-ориентация** применяется при загрузке через
  `PIL.ImageOps.exif_transpose`; планировщик получает уже корректные
  размеры.
- **Здравый DPI**: фото даунскейлятся до не более 300 DPI на печатном
  размере перед вставкой в PDF — 50-мегапиксельная фотка в ячейке 5×7 см
  не раздувает файл и не зависает принтер.
- **PDF-координаты** — нижний-левый угол; конверсия — только в
  `core/renderer.py`.

## Лицензия

MIT — см. заголовки файлов.
