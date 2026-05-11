# u-print

**Приложение на GTK4 / libadwaita для Linux: печать нескольких фото на
одном листе (N-up) и поиск этих фото в системе.**

В Windows есть встроенный мастер «Печать изображений» с раскладками, в
GNOME удобного аналога нет. `u-print` (в системе называется
**PhotoPrint**) закрывает эту дыру: индексирует папки, где у вас реально
лежат фотографии, ищет по ним, отправляет выделенную пачку прямо в
раскладку для печати, показывает живое превью, печатает через CUPS или
сохраняет PDF.

Целевая платформа — **Ubuntu 24.04 LTS** (GNOME 46, Wayland & X11).
Собирается и работает на 22.04+ с теми же системными пакетами.

> Репозиторий: <https://github.com/DanielLetto2020/u-print>
> Лицензия: [MIT](./LICENSE)

---

## Возможности

### Вкладка Search — ваша библиотека фото с индексом

- Добавляйте сколько угодно папок: они рекурсивно сканируются (`.Trash`,
  `.thumbnails` и прочий мусор пропускаются).
- Метаданные кешируются в SQLite (`~/.config/photoprint/photo_index.db`):
  путь, размер, mtime, разрешение, EXIF-дата — повторный запуск
  моментально показывает фото без пересканинга диска.
- **Инкрементальный показ**: фото появляются в UI *по мере того, как
  индексатор их находит*, а не в конце прохода. 1500+ файлов не вешают
  окно.
- **Виртуальный скроллинг** через `Gtk.GridView` / `Gtk.ColumnView`:
  стоимость отрисовки зависит от того, что видно, а не от общего
  количества. 15 000 фото ведут себя так же, как 150.
- **Ленивые миниатюры** в пуле из 4 потоков с LRU-кешем — main-тред
  никогда не блокируется на PIL.
- Регистронезависимый поиск по имени (живой).
- Два режима: **Grid** (миниатюры) и **List** (таблица с сортировкой).
  В list-режиме клик по шапке колонки сортирует, рядом с тоглом —
  поповер с галочками: какие колонки показывать (по умолчанию видны
  `Preview` / `Name` / `Date` / `Size` / `Resolution`, скрыты — `Type` /
  `Folder` / `Path` / `Modified`).
- **Полоса выбранных** (tray): как только вы что-то выделили, сверху
  появляется горизонтальная лента с миниатюрами выбора. Клик по `×` или
  по плитке в полосе — снимает выделение. Кнопка-метла — очистить всё.
- **Toggle-клик**: одиночный клик добавляет или убирает фото из выбора —
  Ctrl держать не нужно. Можно набрать 50 фото из тысячи, не теряя
  предыдущие.
- **Send to Print** переносит выделение во вкладку печати.

### Вкладка Print — выбор раскладки, превью, печать

- Перетащите фото из Файлов (Nautilus), кнопкой *Add photos…* или
  передайте из Search. EXIF-ориентация применяется при загрузке.
- Сетки: **1 / 2 (1×2 или 2×1) / 4 / 6 (2×3 или 3×2) / 8 (2×4 или 4×2)
  / 9 / 16** фото на лист.
- Размеры: A3, A4, A5, Letter, Legal, 10×15 см, 13×18 см, 20×30 см.
- Ориентация: книжная / альбомная / **авто** (по аспектам фото и
  средней «полноте» в ячейке).
- Режимы заполнения: **Fit** (вписать, белые поля), **Fill** (заполнить,
  обрезать центром), **Stretch**.
- Настраиваемые поля по сторонам (мм), gutter между ячейками, рамка
  фото (толщина в пт + цвет), подпись под фото (имя файла или дата
  EXIF), авто-поворот для максимального заполнения.
- Живое превью через `pypdfium2`: настоящий PDF рендерится на пониженном
  DPI и сразу растеризуется в `Gdk.Texture`.
- **Save as PDF…** и **Print…** (последний — диалог CUPS: принтер,
  копии, ч/б, качество).
- **Именованные пресеты**: сохранил текущий paper + grid + margins под
  именем «Фото на документы 4×6» — потом применяешь одним кликом.

### Общие функции

- Переключение языка: English (по умолчанию), Русский, Auto (по
  системной локали) — через гамбургер-меню; применяется после
  перезапуска.
- Восстановление сессии: если на закрытии в списке были фото — при
  следующем запуске предложит их вернуть.
- Горячие клавиши: `Ctrl+O` (добавить), `Ctrl+P` (печать), `Ctrl+S`
  (сохранить PDF), `Ctrl+A` (выделить всё), `Delete` (удалить выделение).
- Следует системной светлой/тёмной теме через libadwaita.

## Скриншоты

> *Добавить скриншоты при первом релизе — Search с загруженными фото,
> Print с раскладкой 2×2 и превью, открытое меню языка.*

---

## Установка

### Из исходников (для разработки)

Проверено на Ubuntu 24.04. Python 3.12, GTK 4.14, libadwaita 1.5.

```bash
sudo apt install -y python3-gi python3-cups gir1.2-gtk-4.0 gir1.2-adw-1 \
                    libheif1 libheif-plugin-libde265 libcups2-dev \
                    python3-venv python3-pip

git clone https://github.com/DanielLetto2020/u-print.git
cd u-print

python3 -m venv --system-site-packages .venv
source .venv/bin/activate
pip install -e ".[dev]"

# Компиляция переводов (чистый Python, msgfmt не нужен)
python po/build_mo.py

# Запуск
python -m photoprint
```

Передайте пути к фото, чтобы предзагрузить:

```bash
python -m photoprint /path/to/*.jpg
```

### Установка для пользователя (без sudo)

```bash
mkdir -p ~/.local/bin ~/.local/share/applications \
         ~/.local/share/icons/hicolor/scalable/apps \
         ~/.local/share/locale/ru/LC_MESSAGES

# Лаунчер
cat > ~/.local/bin/photoprint <<EOF
#!/usr/bin/env bash
exec "$(pwd)/.venv/bin/python" -m photoprint "\$@"
EOF
chmod +x ~/.local/bin/photoprint

# Десктоп-интеграция
install -m 644 data/desktop/io.github.photoprint.PhotoPrint.desktop \
               ~/.local/share/applications/io.github.photoprint.PhotoPrint.desktop
install -m 644 data/icons/io.github.photoprint.PhotoPrint.svg \
               ~/.local/share/icons/hicolor/scalable/apps/io.github.photoprint.PhotoPrint.svg
install -m 644 po/ru/LC_MESSAGES/photoprint.mo \
               ~/.local/share/locale/ru/LC_MESSAGES/photoprint.mo

# Освежить кеши GNOME, чтобы иконка появилась в Activities
update-desktop-database ~/.local/share/applications
gtk-update-icon-cache -t ~/.local/share/icons/hicolor
```

После этого `photoprint` доступен из `PATH`, **PhotoPrint** появится в
Activities.

### Системная установка на Ubuntu

```bash
sudo packaging/install-deb.sh
```

Скрипт создаёт `/opt/photoprint/` с venv, кладёт лаунчер в
`/usr/local/bin/photoprint`, разворачивает `.desktop` / иконку /
переводы в `/usr/local/share/`. Это не настоящий `.deb`; для него
оберните это через [`dh-virtualenv`](https://github.com/spotify/dh-virtualenv).

### Flatpak (рекомендую для распространения)

Манифест: [`packaging/io.github.photoprint.PhotoPrint.yml`](packaging/io.github.photoprint.PhotoPrint.yml).

```bash
flatpak install flathub org.gnome.Platform//46 org.gnome.Sdk//46
flatpak-builder --user --install --force-clean build-flatpak \
    packaging/io.github.photoprint.PhotoPrint.yml
flatpak run io.github.photoprint.PhotoPrint
```

Flatpak тянет GTK-runtime, изолирует зависимости, CUPS пробрасывается
через `--socket=cups`.

---

## Как пользоваться

1. **Запустите** приложение. По умолчанию открыта вкладка Print, в шапке
   — переключатель **Search ↔ Print**.
2. **Search** → **🗂 иконка папки** → **Add folder…** → выберите,
   например, `~/Pictures`. Индексатор стартует сразу, фото появляются
   по мере нахождения, прогресс-бар показывает текущую папку и счётчик.
3. Введите в строку поиска часть имени файла — список фильтруется в
   реальном времени. Переключитесь в **list-режим**, чтобы видеть
   таблицу с сортировкой; кнопка **⋯** рядом с тоглом — поповер
   видимости колонок.
4. Кликайте по фото, чтобы добавлять или убирать из выделения. Сверху
   разворачивается полоса выбранных миниатюр.
5. **Send N to Print** перебрасывает выделение во вкладку печати,
   заполняет левый список фото и перерисовывает превью.
6. Настройте раскладку в правой панели — бумага, сетка, поля, режим
   заполнения, рамка, подпись.
7. **Save PDF…** или **Print…** (диалог CUPS) — оба используют тот же
   итоговый PDF на 300 DPI.

Подсказка: в меню гамбургера в шапке можно переключить язык на
English / Русский / Auto — применится после перезапуска.

---

## Файлы конфигурации

Все лежат в `~/.config/photoprint/`:

| Файл              | Назначение                                            |
|-------------------|-------------------------------------------------------|
| `settings.json`   | Последний принтер, последняя раскладка, язык UI       |
| `presets.json`    | Именованные пресеты раскладок                         |
| `session.json`    | Фото и параметры последней незакрытой сессии          |
| `photo_index.db`  | SQLite-индекс отслеживаемых папок                     |

Удалите файл, чтобы сбросить соответствующий аспект — приложение
пересоздаст его при следующем запуске.

---

## Архитектура (верхнеуровнево)

```
photoprint/
├── core/
│   ├── layout.py        Чистый планировщик: фото + параметры → LayoutPlan (мм)
│   ├── renderer.py      LayoutPlan + reportlab → PDF
│   ├── image_loader.py  PIL + EXIF + HEIC + кеш миниатюр
│   ├── photo_index.py   SQLite-индекс папок
│   ├── printer.py       Обёртка над pycups
│   └── settings.py      JSON-настройки (~/.config/photoprint/)
├── ui/
│   ├── window.py        Adw.ApplicationWindow с Adw.ViewStack
│   ├── search_view.py   Вкладка Search с GridView/ColumnView + tray
│   ├── photo_list.py    Колонка фото в Print
│   ├── sidebar.py       Настройки раскладки
│   ├── preview.py       pypdfium2 → Gdk.Texture превью
│   ├── print_dialog.py  Диалог CUPS принтера + опций
│   ├── thumbnail_loader.py  ThreadPool + LRU загрузчик
│   └── photo_item_model.py  GObject-обёртка для Gio.ListStore
└── i18n.py              Gettext bootstrap + DEFAULT_LANGUAGE = "en"
```

Координаты раскладки — **мм, начало в верхнем-левом углу**. Перевод в
PDF-точки (нижний-левый) делается только в рендерере.

---

## Локализация

Строки обёрнуты `_()` из `photoprint.i18n`. Русский каталог — в
`po/ru/`. Компиляция:

```bash
python po/build_mo.py
```

Чтобы добавить язык: скопируйте `po/photoprint.pot` в
`po/<lang>/LC_MESSAGES/photoprint.po`, переведите `msgstr`, снова
запустите скрипт.

Активный язык хранится как `language` в `settings.json` и меняется в
рантайме через меню (применится после рестарта).

---

## Разработка

```bash
# Тесты (31, быстрые)
pytest -q

# Линт + форматирование
ruff check photoprint tests
ruff format photoprint tests
```

Покрытие: чистый планировщик (`core/layout.py`), PDF-рендерер
(`core/renderer.py`), SQLite-индекс (`core/photo_index.py`).

CI-friendly:

```bash
pytest -q && ruff check photoprint tests
```

---

## Зачем нужны системные пакеты

| Библиотека          | Назначение                                  | Пакет apt                              |
|---------------------|---------------------------------------------|----------------------------------------|
| GTK 4 + libadwaita  | Окно + виджеты                              | `gir1.2-gtk-4.0`, `gir1.2-adw-1`       |
| PyGObject           | Питон ↔ GTK мост                            | `python3-gi`                           |
| pycups              | Бинды CUPS (печать, список принтеров)       | `python3-cups`                         |
| libheif             | Декодирование HEIC / HEIF                   | `libheif1`, `libheif-plugin-libde265`  |
| libcups2-dev        | Только если ставить `pycups` через pip      | `libcups2-dev`                         |

PyGObject и pycups берутся из системного Python через
`--system-site-packages`. Остальное (`Pillow`, `pillow-heif`,
`reportlab`, `pypdfium2`, `platformdirs`) ставится в venv проекта.

---

## Контрибуция

1. Создайте ветку от `main`.
2. Сообщения коммитов — на русском (как в текущем `git log`) или на
   английском, главное, чтобы единообразно.
3. Перед пушем — `pytest && ruff check`.
4. Откройте PR.

---

## Подводные камни

- **Drag-and-drop в Wayland** работает через `Gtk.DropTarget` с
  `Gdk.FileList`, не через старый `Gtk.TargetList`.
- **EXIF-ориентация** применяется при загрузке через
  `PIL.ImageOps.exif_transpose`; планировщик получает уже корректные
  размеры.
- **Здравый DPI**: фото даунскейлятся до не более 300 DPI на печатном
  размере перед вставкой в PDF — 50-мегапиксельный кадр в ячейке 5×7 см
  не раздувает файл и не вешает принтер.
- **PDF-координаты** — нижний-левый угол; конверсия только в
  `core/renderer.py`.
- **SQLite и потоки**: индекс открывает соединение с
  `check_same_thread=False`. Сканер работает в worker-треде, UI делает
  запросы из main-треда. Одновременных писателей нет — кнопка Rescan
  блокируется на время прохода.

---

## Лицензия

MIT — см. [LICENSE](./LICENSE). Переводы — производные под той же
лицензией.
