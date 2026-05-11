# CLAUDE.md

Гид для Claude Code (и других ИИ-ассистентов) по работе с этим репо.
Что НЕ очевидно из чтения кода — здесь.

## Что это

`u-print` (в десктопе — **PhotoPrint**) — GTK4/libadwaita-приложение для
Linux: индексирует папки с фотографиями, ищет по ним и печатает выделенные
файлы в раскладке N-up через CUPS или сохраняет PDF.

Репозиторий: <https://github.com/DanielLetto2020/u-print>.

## Стек и окружение

- **Python 3.12** через системный `/usr/bin/python3` (НЕ pyenv —
  PyGObject привязан к системным GIR-биндингам).
- **venv с `--system-site-packages`** — тянет `python3-gi`, `python3-cups`
  из системы, остальное (`Pillow`, `pillow-heif`, `reportlab`,
  `pypdfium2`, `platformdirs`) ставится через pip в `.venv/`.
- **GTK 4.14 + libadwaita 1.5**, Adwaita-Native виджеты предпочтительнее
  «голых» Gtk: `Adw.PreferencesGroup`, `Adw.ComboRow`, `Adw.AlertDialog`,
  `Adw.StatusPage`, `Adw.ViewStack`, `Adw.HeaderBar`, `Adw.Toast`.
- Целевая ОС — **Ubuntu 24.04 LTS** (GNOME 46). 22.04 тоже работает с
  теми же apt-пакетами.

## Структура

```
photoprint/
├── core/                     # БЕЗ GUI-зависимостей, легко тестируется
│   ├── layout.py             # планировщик: фото + params → LayoutPlan (мм)
│   ├── renderer.py           # LayoutPlan + reportlab → PDF
│   ├── image_loader.py       # PIL + EXIF + HEIC + LRU миниатюр
│   ├── photo_index.py        # SQLite-индекс + SHA-256 поиск дубликатов
│   ├── printer.py            # обёртка над pycups
│   └── settings.py           # JSON-настройки и пресеты
├── ui/                       # GTK4/Adw виджеты
│   ├── window.py             # MainWindow + Adw.ViewStack (Search по умолчанию)
│   ├── search_view.py        # Search: Grid + List + Duplicates + tray
│   ├── photo_list.py         # колонка фото в Print
│   ├── sidebar.py            # настройки раскладки
│   ├── preview.py            # pypdfium2 → Gdk.Texture + «print this page»
│   ├── print_dialog.py       # CUPS-диалог
│   ├── photo_item_model.py   # PhotoEntryItem + DuplicateGroupItem (GObject)
│   ├── thumbnail_loader.py   # ThreadPoolExecutor + LRU кеш текстур
│   └── file_manager.py       # «Открыть в проводнике» через D-Bus
├── i18n.py                   # gettext bootstrap, DEFAULT_LANGUAGE = "en"
├── main.py                   # Adw.Application
└── __main__.py               # python -m photoprint
po/
├── photoprint.pot            # шаблон
├── ru/LC_MESSAGES/*.po       # русский каталог (en — identity)
└── build_mo.py               # pure-Python .po → .mo (msgfmt не нужен)
tests/                        # pytest, 35 тестов на core
data/                         # .desktop, иконка, AppStream
packaging/                    # install-deb.sh, Flatpak-манифест
```

## Команды

```bash
# Окружение
python3 -m venv --system-site-packages .venv
.venv/bin/pip install -e ".[dev]"

# Запуск (из корня репо)
.venv/bin/python -m photoprint
.venv/bin/python -m photoprint -v *.jpg     # с фото и логами

# Тесты + линт ОБЯЗАТЕЛЬНО перед коммитом
.venv/bin/python -m pytest tests/ -q
.venv/bin/ruff check photoprint tests

# Smoke-тест GUI (запускается на 4 сек и убивается)
timeout 4 .venv/bin/python -m photoprint -v 2>&1 | head -20

# Пересобрать .mo после правки .po (msgfmt не нужен)
.venv/bin/python po/build_mo.py
install -m 644 po/ru/LC_MESSAGES/photoprint.mo \
               ~/.local/share/locale/ru/LC_MESSAGES/photoprint.mo
```

## Соглашения проекта

### Git и коммиты

- Ветка по умолчанию — **`main`**. `master` удалена.
- **Сообщения коммитов — на русском**. Стиль: короткий заголовок без
  префиксов в духе `feat:` / `fix:`, краткое описание абзацем,
  Co-Authored-By в конце для Claude-сессий.
- Не пушим force без явной необходимости. Один раз делали
  `filter-branch` чтобы вычистить локальный `idea.txt` из истории —
  это исключение.
- `idea.txt` в `.gitignore`, **никогда** не коммитим (личные заметки).

### Локализация

- ВСЕ user-visible строки через `_()` из `photoprint.i18n`, не из
  `gettext` напрямую (важно: модульный `gettext.gettext` не
  отслеживает `Translation.install()` — поэтому делегат в `i18n.py`).
- При добавлении новой строки:
  1. Обернуть `_("…")` в коде.
  2. Добавить `msgid`/`msgstr` в `po/ru/LC_MESSAGES/photoprint.po`.
  3. Добавить `msgid`/`msgstr ""` в `po/photoprint.pot`.
  4. `python po/build_mo.py` + установить .mo в `~/.local/share/locale/`.
- НЕ использовать `lambda *_:` — `_` это имя gettext-функции, шадовится.
  Используем `lambda *args:` или `lambda *_a:`.

### Координаты

- В `core/layout.py` и UI — **мм, верхний-левый угол**.
- В `core/renderer.py` (и только там) — **PDF-точки, нижний-левый**.
- Конверсия `mm * 2.8346` локализована в одной функции.

### Чистое ядро

- В `core/` НЕЛЬЗЯ импортировать `gi.repository`, `gtk`, `gdk`.
- Pure-функции с dataclass-входом/выходом: `plan_layout`,
  `render_plan_to_pdf`, `PhotoIndex` API. Поэтому их легко тестировать
  без GTK.

### Стиль

- Type hints везде.
- Docstrings Google-style для публичных функций.
- `ruff check` без ошибок (см. правила в `pyproject.toml`).
- `dataclass(frozen=True)` для всего, что является value object
  (`PaperSize`, `LayoutParams`, `PhotoEntry`, `CellPlacement` и т.д.).
- Никаких глобальных переменных, кроме явных синглтонов
  (`ThumbnailLoader.get_default()`).

## Архитектурные нюансы

### SQLite + потоки

`PhotoIndex` открывает соединение с `check_same_thread=False`.
Сканирование (`rescan`) и хеширование (`compute_missing_hashes`) идут в
`threading.Thread`, UI делает `search()` / `find_duplicates()` из
main-треда. **Одновременных писателей нет** — кнопки Rescan и
Duplicates блокируются на время прохода.

### Миграция схемы (v1 → v2)

`SCHEMA_VERSION = 2`. На свежей БД `CREATE TABLE` ставит колонку
`content_hash` сразу. На старых (v1) — ленивая `ALTER TABLE ADD COLUMN
content_hash TEXT` в `__init__`, обёрнутая в `try/except OperationalError`
для идемпотентности. Индексы по `size` и `content_hash` создаются
**после** ALTER, а НЕ в `_SCHEMA_SQL`, иначе на v1 `CREATE INDEX` ссылался
бы на ещё не существующую колонку и падал. Если будешь добавлять новые
колонки — пользуйся тем же паттерном.

### Поиск дубликатов

1. `compute_missing_hashes` — SHA-256 (потоково, чанками по 64 КБ)
   ТОЛЬКО для файлов с «соседом по размеру» (`size IN (SELECT size
   GROUP BY HAVING COUNT > 1)`). Одиночки по байтам дубликатами быть не
   могут, поэтому экономим IO.
2. `find_duplicates` — `GROUP BY content_hash HAVING COUNT > 1`,
   возвращает список списков `PhotoEntry`, отсортированных по
   количеству копий по убыванию.
3. В `_upsert` для новых/изменённых файлов `content_hash` сбрасывается в
   NULL — пересчёт при следующем заходе на вкладку Duplicates.

### Инкрементальный показ при scan

`PhotoIndex.rescan()` принимает `on_entry: Callable[[PhotoEntry], None]`,
который дёргается на каждой добавленной/обновлённой записи. SearchView
складывает их в `_pending_entries` под `threading.Lock`, а
`GLib.timeout_add(150, ...)` каждые 150 мс переливает буфер в
`Gio.ListStore` — фото появляются по мере индексации, главный тред не
блокируется.

### Виртуальный скроллинг

`Gtk.GridView` и `Gtk.ColumnView` ОБЕ привязаны к одной
`Gtk.MultiSelection`, поверх одной `Gtk.SortListModel`. Поэтому выбор
сохраняется при переключении grid/list, сортировка (по умолчанию
`mtime DESC`) тоже одна. Виджеты создаются только для видимых ячеек
через `Gtk.SignalListItemFactory`.

У вкладки Duplicates **своя** модель — отдельный `Gio.ListStore` из
`DuplicateGroupItem` с `Gtk.NoSelection` (одиночные клики раскрывают
детальную панель, а не выделяют). Поэтому переключение в Duplicates не
сбрасывает выделение, накопленное в Grid/List.

### Скролл-в-верх

`SearchView.scroll_to_top()` сбрасывает `vadjustment` обоих скроллеров
(`_grid_scroller`, `_list_scroller`) в начало. Зовётся через
`GLib.idle_add` (а не сразу) в двух случаях:
1. `_reload_from_index()` — после полной перезаливки `Gio.ListStore`;
2. `MainWindow._on_view_changed` — когда юзер кликнул на вкладку Search.

Без `idle_add` `set_value(0)` отыгрывает до layout-фазы и «откатывается»
обратно — GridView пересчитывает виртуальный диапазон только после
обработки изменений модели.

### Toggle-клик в GridView/ColumnView

По умолчанию клик в GridView/ColumnView без модификаторов делает
«выбрать только этот». Чтобы клик переключал (как Ctrl-клик), на каждый
ячеистый виджет в `setup` фабрики ставится `Gtk.GestureClick` с
`PROPAGATION_PHASE.CAPTURE`. Жест читает `list_item.get_position()`,
дёргает `selection.select_item(pos, False)` или `unselect_item(pos)` и
помечает sequence как `CLAIMED`, чтобы встроенный обработчик не сбросил
остальные выделенные позиции.

### Ленивые миниатюры

`photoprint.ui.thumbnail_loader.ThumbnailLoader` — пул из 4 потоков с
LRU-кешем на 600 текстур. UI-виджеты в `bind` запоминают `_current_path`
и в callback проверяют, что виджет всё ещё показывает то же фото
(между bind и callback он мог быть переиспользован для другой строки —
тогда текстуру не применяем).

### Превью

`pypdfium2` рендерит готовый PDF на пониженном DPI (96–120), результат
→ `PIL.Image` → PNG-байты → `Gdk.Texture.new_from_bytes` →
`Gtk.Picture.set_paintable`. Тот же `core/renderer.py` используется и
для превью, и для печати — единый кодпуть, просто разный DPI.

### Печать одной страницы

`PreviewWidget` эмитит сигнал `print-page-requested(int)` с индексом.
MainWindow собирает урезанный `LayoutPlan(params=..., pages=(одна,))`
и шлёт через общий `_open_print_dialog(plan)`. `Print…` в шапке шлёт
весь план. Текущий «улетающий в CUPS» план хранится в
`MainWindow._plan_to_print` и читается из `_handle_print_result`.

### «Открыть в проводнике»

`photoprint.ui.file_manager.open_in_file_manager(path)` сначала пробует
D-Bus `org.freedesktop.FileManager1.ShowItems` — Nautilus, Nemo, Caja и
прочие GTK-проводники подсветят файл в открывшейся папке. При неудаче
(нет шины, файл-менеджер не подписан) — фолбэк на `xdg-open` родительской
папки. URI обязательно через `Path.as_uri()`, иначе пробелы и юникод
ломают вызов.

## Чего НЕ делать

- НЕ загружать миниатюры синхронно в bind/setup — только через
  `ThumbnailLoader`.
- НЕ хранить ссылки на PhotoEntry в виджетах напрямую — используем
  `_current_path` и проверку.
- НЕ добавлять GUI-импорты в `core/`.
- НЕ использовать `lambda *_:` (шадовит `_` из i18n).
- НЕ добавлять `CREATE INDEX` по новой колонке в `_SCHEMA_SQL` без
  условия — на старых БД упадёт. Создавай индекс отдельным выражением
  ПОСЛЕ `ALTER TABLE ADD COLUMN`.
- НЕ хешировать все файлы подряд при поиске дубликатов — выбирай только
  size-коллизии, иначе на больших библиотеках можно ждать минутами.
- НЕ создавать `master`, `develop` и т.д. — только feature-branches от
  `main`, потом PR.
- НЕ коммитить `idea.txt`, `.venv/`, `*.egg-info/`, `__pycache__/`
  (есть в `.gitignore`).
- НЕ ослаблять `ruff` без явной причины — текущие правила в
  `pyproject.toml`.

## Тесты, которые ОБЯЗАНЫ оставаться зелёными

- `tests/test_layout.py` — 15 тестов планировщика.
- `tests/test_renderer.py` — 5 тестов рендерера.
- `tests/test_photo_index.py` — 14 тестов индекса (включая дубликаты).
- `tests/test_e2e.py` — 1 end-to-end (план + рендер реального JPEG).

Итого 35. Если меняешь поведение — добавь тест.

## Когда что-то изменено в UI

После любых правок в `photoprint/ui/`:

```bash
timeout 4 .venv/bin/python -m photoprint -v 2>&1 | grep -vE "DEBUG|PIL\.|pypdfium2" | head -20
```

Должно завершиться `exit: 0` без трейсбэков (timeout-убийство — норма).

## Установка иконки в систему

Под пользователя:
```bash
~/.local/bin/photoprint                                      # лаунчер
~/.local/share/applications/io.github.photoprint.PhotoPrint.desktop
~/.local/share/icons/hicolor/scalable/apps/io.github.photoprint.PhotoPrint.svg
~/.local/share/locale/ru/LC_MESSAGES/photoprint.mo
```

После изменений в `.desktop` или иконке:
```bash
update-desktop-database ~/.local/share/applications
gtk-update-icon-cache -t ~/.local/share/icons/hicolor
```
