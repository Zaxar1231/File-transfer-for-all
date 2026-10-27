# File Transfer for All

[![CI](https://github.com/Zaxar1231/File-transfer-for-all/actions/workflows/ci.yml/badge.svg)](https://github.com/Zaxar1231/File-transfer-for-all/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](pyproject.toml)

Приложение для передачи файлов в стиле **Material You (Material Design 3)**. Работает с
SFTP, FTP/FTPS, WebDAV, Amazon S3 и S3-совместимыми хранилищами, HTTP(S) и файлами на
самом устройстве. Собирается под Windows, macOS, Linux, Android и iOS.

![Файлы: две панели, слева устройство, справа FTP-сервер](docs/screenshots/files.png)

| Передачи | Подключения | Телефон, тёмная тема |
|---|---|---|
| ![Очередь передач с прогрессом](docs/screenshots/transfers.png) | ![Сохранённые подключения](docs/screenshots/connections.png) | ![Телефонная раскладка в тёмной теме](docs/screenshots/phone-dark.png) |

## Возможности

- **Две панели, как в файловом менеджере.** Каждая показывает это устройство или любое
  сохранённое подключение. Файлы и папки копируются между панелями в обе стороны, в том
  числе напрямую сервер → сервер (например, FTP → S3).
- **Очередь передач.** Прогресс, скорость, отмена, повтор после ошибки, несколько передач
  одновременно. Бейдж на вкладке показывает, сколько передач идёт сейчас.
- **Подключения.** Форма подстраивается под протокол. Кнопка «Проверить» соединяется с
  сервером до сохранения. Пароль сохраняется только с галочкой «Запомнить», иначе
  приложение спросит его один раз за сеанс.
- **Работа с файлами.** Создание папок, удаление (папки — вместе с содержимым), скрытые
  файлы по желанию, выбор папки системным диалогом.
- **Material You.** Вся палитра строится из выбранного цвета акцента, есть светлая, тёмная
  и системная темы. На широком экране навигация слева (navigation rail с FAB), на телефоне
  внизу (navigation bar) и панели переключаются по одной.

## Запуск

```bash
python -m venv .venv && . .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -e .
fta-app                                          # или: python -m fta.app
```

При первом запуске Flet сам скачает свой десктопный клиент.

Настройки и подключения хранятся в `~/.config/fta/config.json` (Windows:
`%APPDATA%\fta`, macOS: `~/Library/Application Support/fta`, в собранном приложении —
в его каталоге данных). Файл доступен только текущему пользователю. Сохранённые пароли
лежат в нём открытым текстом, поэтому «Запомнить пароль» выключено по умолчанию.

## Сборка приложения

Сборкой занимается [Flet](https://flet.dev): Python-приложение упаковывается во Flutter.
Flutter SDK `flet build` скачивает сам при первой сборке.

```bash
pip install "flet[cli]==1.0.5"
flet build linux       # или: windows, macos, apk, aab, ipa
```

Готовая сборка появится в `build/<платформа>`. Настройки сборки (название, идентификатор,
путь к коду) лежат в `[tool.flet]` в `pyproject.toml`, иконка — в `src/assets/icon.png`.
Windows, macOS и iOS собираются на своей ОС; требования к каждой платформе описаны в
[документации Flet](https://flet.dev/docs/publish).

## Протоколы

| Протокол | Схема | Что умеет |
|---|---|---|
| Это устройство | путь, `file://` | всё |
| SFTP | `sftp` | всё; ключ хоста проверяется по `~/.ssh/known_hosts` |
| FTP / FTPS | `ftp`, `ftps` | всё; листинг требует MLSD (RFC 3659) |
| WebDAV | `dav`, `davs` | всё; стандартный WebDAV (RFC 4918) |
| Amazon S3 и совместимые | `s3` | всё; папки — это префиксы ключей |
| HTTP / HTTPS | `http`, `https` | скачивание, загрузка через PUT, без листинга |
| SMB | `smb` | пока не реализован |

## Командная строка

То же ядро доступно без интерфейса, например для скриптов:

```bash
fta cp -r ./photos sftp://me@nas.local/backup/ --dst-opt password=secret
fta cp ftp://user@host/pub/data.zip ./ --src-opt password=secret
fta ls s3://my-bucket/archive
fta stat dav://localhost:8080/docs/a.txt
fta mkdir -p ftp://user@host/new/dir --opt password=secret
fta rm sftp://me@nas.local/tmp/old.log
fta schemes
```

## Архитектура

```
src/
├── main.py                  # точка входа для flet build
├── assets/icon.png
└── fta/
    ├── app/                 # приложение (Flet, Material 3)
    │   ├── main.py          #   оболочка: тема, адаптивная навигация, диалоги
    │   ├── storage.py       #   подключения и настройки (JSON)
    │   ├── transfers.py     #   очередь передач на фоновых потоках
    │   ├── ui.py            #   общие элементы и форматирование
    │   └── views/           #   экраны: browser, transfers, connections, settings
    ├── core/                # ядро, не зависит от интерфейса
    │   ├── transport.py     #   интерфейс Transport, прогресс, ошибки библиотек
    │   ├── registry.py      #   схема URL → транспорт, плагины
    │   ├── manager.py       #   copy(): файлы и папки между любыми двумя транспортами
    │   └── endpoint.py, errors.py
    ├── transports/          # local, http, webdav, ftp, sftp, s3, planned (smb)
    └── cli.py               # fta cp / ls / stat / mkdir / rm
```

- **Ядро отделено от интерфейса.** Приложение и CLI используют одни и те же транспорты и
  одну функцию `copy()`. Модель приложения (`storage.py`, `transfers.py`) тоже не зависит
  от Flet и покрыта обычными тестами.
- **Любой протокол с любым.** Транспорту достаточно уметь загрузку и скачивание: передача
  сервер → сервер идёт через временный файл.
- **Новый протокол** — это класс `Transport` с декоратором `@register` в `transports/`
  или пакет с entry point `fta.transports`. Ни ядро, ни интерфейс менять не нужно: схема
  появится в списке протоколов.

## Разработка

```bash
pip install -e ".[dev]"
pytest
```

Тесты протоколов поднимают настоящие серверы прямо в процессе: FTP (pyftpdlib), WebDAV и
HTTP (WsgiDAV + cheroot), SFTP (asyncssh) и S3 (moto). Сеть не нужна. CI запускает тесты на
Python 3.10–3.13.

Приложение можно открыть в браузере (удобно для отладки интерфейса):

```bash
python -c "import flet as ft; from fta.app.main import main; ft.run(main, view=ft.AppView.WEB_BROWSER)"
```

## Ограничения и планы

- SMB, а также облачные диски с OAuth (Google Drive, Dropbox, OneDrive) пока не поддерживаются.
- Нет докачки (resume) и автоматических повторов при обрыве; повторить можно вручную.
- Передача сервер → сервер требует места на диске под самый большой файл.
- На Android и iOS доступ к файлам устройства ограничен песочницей приложения.

## Лицензия

[MIT](LICENSE)
