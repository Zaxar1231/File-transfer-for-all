# File Transfer for All

[![CI](https://github.com/Zaxar1231/File-transfer-for-all/actions/workflows/ci.yml/badge.svg)](https://github.com/Zaxar1231/File-transfer-for-all/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/Zaxar1231/File-transfer-for-all)](https://github.com/Zaxar1231/File-transfer-for-all/releases/latest)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Приложение для передачи файлов в стиле **Material You (Material Design 3)**. Работает с
SFTP, SMB (Windows, Samba, NAS), FTP/FTPS, WebDAV, Amazon S3 и S3-совместимыми
хранилищами, HTTP(S) и файлами на самом устройстве. Есть для Windows, macOS, Linux и Android.

## Скачать

Ссылки всегда ведут на последнюю версию.

| Платформа | Файл |
|---|---|
| **Windows** 10/11, x64 | [FileTransferForAll-windows-x64.zip](https://github.com/Zaxar1231/File-transfer-for-all/releases/latest/download/FileTransferForAll-windows-x64.zip) |
| **macOS**, Apple Silicon (M1–M4) | [FileTransferForAll-macos-arm64.zip](https://github.com/Zaxar1231/File-transfer-for-all/releases/latest/download/FileTransferForAll-macos-arm64.zip) |
| **macOS**, Intel | [FileTransferForAll-macos-x64.zip](https://github.com/Zaxar1231/File-transfer-for-all/releases/latest/download/FileTransferForAll-macos-x64.zip) |
| **Linux** x64 | [FileTransferForAll-linux-x64.tar.gz](https://github.com/Zaxar1231/File-transfer-for-all/releases/latest/download/FileTransferForAll-linux-x64.tar.gz) |
| **Linux** ARM64 (Raspberry Pi 4/5 и т. п.) | [FileTransferForAll-linux-arm64.tar.gz](https://github.com/Zaxar1231/File-transfer-for-all/releases/latest/download/FileTransferForAll-linux-arm64.tar.gz) |
| **Android**, любой процессор | [FileTransferForAll-android.apk](https://github.com/Zaxar1231/File-transfer-for-all/releases/latest/download/FileTransferForAll-android.apk) |
| Android, только ARM64 (большинство телефонов) | [FileTransferForAll-android-arm64-v8a.apk](https://github.com/Zaxar1231/File-transfer-for-all/releases/latest/download/FileTransferForAll-android-arm64-v8a.apk) |
| Android, ARMv7 (старые телефоны) | [FileTransferForAll-android-armeabi-v7a.apk](https://github.com/Zaxar1231/File-transfer-for-all/releases/latest/download/FileTransferForAll-android-armeabi-v7a.apk) |
| Android, x86_64 (эмуляторы, Chromebook) | [FileTransferForAll-android-x86_64.apk](https://github.com/Zaxar1231/File-transfer-for-all/releases/latest/download/FileTransferForAll-android-x86_64.apk) |

Все версии и контрольные суммы (`SHA256SUMS.txt`) — на [странице релизов](https://github.com/Zaxar1231/File-transfer-for-all/releases).

Сборки пока не подписаны, поэтому система предупредит при первом запуске:

- **Windows:** распакуйте архив и запустите `FileTransferForAll.exe`; в окне SmartScreen
  нажмите «Подробнее» → «Выполнить в любом случае».
- **macOS:** распакуйте архив и перетащите приложение в «Программы». Первый раз откройте
  его через правый клик → «Открыть» или выполните
  `xattr -dr com.apple.quarantine "/Applications/FileTransferForAll.app"`.
- **Linux:** `tar -xzf FileTransferForAll-linux-x64.tar.gz -C ~/FileTransferForAll` и
  запустите `~/FileTransferForAll/FileTransferForAll`. Сборка работает на дистрибутивах с
  glibc 2.35 и новее (Ubuntu 22.04+, Debian 12+, Fedora 36+).
- **Android:** откройте APK и разрешите установку из этого источника.

### Обновления

Приложение само проверяет при запуске, вышла ли новая версия, и предлагает обновиться.
На Android новая версия APK скачивается и сразу открывается системным установщиком. На
компьютере архив скачивается в «Загрузки» с прогрессом, и приложение показывает его в
файловом менеджере. Проверку можно выключить или запустить вручную в «Настройки →
Обновления».

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
- **Докачка и проверка целостности.** Оборванная передача продолжается с места обрыва
  (HTTP Range, FTP REST, SFTP, SMB, S3, WebDAV). SHA-256 считается по всему файлу и по
  блокам 4 МиБ; с включённой проверкой копия сверяется с оригиналом.
- **Очередь с приоритетами.** Пауза и продолжение, «поднять наверх», автоповтор с
  нарастающей паузой (2, 4, 8… 60 с). Журнал всех передач с экспортом в CSV.
- **Рядом (как LocalSend).** Устройства в локальной сети находят друг друга сами
  (mDNS/Bonjour). Файлы и папки уходят напрямую по HTTPS, сохраняются структура папок,
  время изменения и права. Получатель подтверждает каждую передачу, по желанию с PIN.
  Сертификат устройства закрепляется по отпечатку из mDNS или QR-кода, чужое устройство
  с тем же адресом не примет файлы. Обрыв — передача продолжится без повторного вопроса.
- **Встроенный SSH-терминал и подключение по IP.** Кнопка «По IP» на вкладке
  «Подключения»: введите адрес (`192.168.1.20`, `nas.local:2222`), логин и пароль — и
  откройте файлы сервера (SFTP, SMB, FTP, WebDAV) или терминал SSH, ничего не сохраняя.
  Терминал — полноценный xterm: цвета, `top`, `mc`, `nano`, `vim`, изменение размера
  окна, панель клавиш Esc/Tab/Ctrl+C/стрелки для телефона. Из терминала одной кнопкой
  открываются файлы того же сервера. Ключ сервера при первом входе показывается с
  отпечатком и запоминается, как в PuTTY; если он изменится, приложение предупредит.
- **SSH/SFTP-сервер для PuTTY.** Включается в настройках: PuTTY или `ssh` открывают
  оболочку передач (`send`, `recv`, `queue`, `peers`, `history`), psftp, WinSCP и
  FileZilla работают с общей папкой по SFTP. Вход по паролю (хранится только хеш) или
  по ключам из `authorized_keys`.
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

### Выпуск релиза

Все сборки делает workflow [`release.yml`](.github/workflows/release.yml) на GitHub Actions.

Релизы непрерывные: каждый pull request, который поднимает версию, после слияния в `main`
сам становится релизом.

1. Поднимите версию в `pyproject.toml` (`version`) и `src/fta/__init__.py` (`__version__`);
   тест проверяет, что они совпадают.
2. Влейте изменения в `main`. Workflow видит, что тега `v<версия>` ещё нет, собирает
   Windows, macOS, Linux x64/arm64 и Android, создаёт тег и релиз с файлами и
   `SHA256SUMS.txt`. Ссылки из раздела «Скачать» и проверка обновлений в приложении сразу
   начинают указывать на него.
3. Слияния без смены версии релиз не выпускают. Тег, поставленный вручную
   (`git tag v1.2.3 && git push origin v1.2.3`), тоже публикует релиз.

## Протоколы

| Протокол | Схема | Что умеет |
|---|---|---|
| Это устройство | путь, `file://` | всё |
| SFTP | `sftp` | всё; ключ хоста проверяется по `~/.ssh/known_hosts` |
| SMB 2/3 | `smb://server/share/...` | всё; первый сегмент пути — общий ресурс |
| FTP / FTPS | `ftp`, `ftps` | всё; листинг требует MLSD (RFC 3659) |
| WebDAV | `dav`, `davs` | всё; стандартный WebDAV (RFC 4918) |
| Amazon S3 и совместимые | `s3` | всё; папки — это префиксы ключей |
| HTTP / HTTPS | `http`, `https` | скачивание, загрузка через PUT, без листинга |

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

# докачка, проверка SHA-256 и три повтора
fta cp --resume --verify --retries 3 https://example.com/big.iso ./

# устройства рядом
fta peers
fta receive --qr                 # показать QR-код и принимать файлы
fta send "Ноутбук Ольги" ./photos  # по имени из fta peers
fta send "fta://192.168.1.20:53318/?fp=…" report.pdf  # по ссылке из QR-кода

# SSH/SFTP-сервер для PuTTY, WinSCP, psftp
fta serve --set-password
fta history -n 20 --csv journal.csv
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
    │   ├── updates.py       #   проверка и скачивание обновлений из GitHub Releases
    │   ├── ui.py            #   общие элементы и форматирование
    │   └── views/           #   экраны: browser, transfers, connections, settings, update
    ├── core/                # ядро, не зависит от интерфейса
    │   ├── transport.py     #   интерфейс Transport, прогресс, ошибки библиотек
    │   ├── registry.py      #   схема URL → транспорт, плагины
    │   ├── manager.py       #   copy(): файлы и папки между любыми двумя транспортами
    │   └── endpoint.py, errors.py
    ├── transports/          # local, http, webdav, ftp, sftp, smb, s3
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
HTTP (WsgiDAV + cheroot), SFTP (asyncssh), S3 (moto) и SMB (Samba). Сеть не нужна. Тест SMB
запускает `smbd`, поэтому нужен установленный Samba (`apt-get install samba`) и root или
`sudo` без пароля; иначе он пропускается. CI запускает тесты на Python 3.10–3.13.

Приложение можно открыть в браузере (удобно для отладки интерфейса):

```bash
python -c "import flet as ft; from fta.app.main import main; ft.run(main, view=ft.AppView.WEB_BROWSER)"
```

## Ограничения и планы

- Облачные диски с OAuth (Google Drive, Dropbox, OneDrive) пока не поддерживаются.
- На компьютере обновление скачивается автоматически, но заменить старую версию нужно
  вручную: сборки не подписаны, и бесшумная замена самого себя без подписи ненадёжна.
- Сборки для iOS нет: для неё нужен платный аккаунт Apple Developer.
- Передача «Рядом» работает внутри одной сети; через интернет и NAT — в следующих этапах.
- Передача сервер → сервер требует места на диске под самый большой файл.
- На Android и iOS доступ к файлам устройства ограничен песочницей приложения.

## Лицензия

[MIT](LICENSE)
