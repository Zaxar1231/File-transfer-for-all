# fta — File Transfer for All

[![CI](https://github.com/Zaxar1231/File-transfer-for-all/actions/workflows/ci.yml/badge.svg)](https://github.com/Zaxar1231/File-transfer-for-all/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](pyproject.toml)

Универсальный инструмент для передачи файлов: один API и одна команда для всех протоколов.
Ядро ничего не знает о конкретных протоколах. Каждый протокол — это отдельный плагин
(класс `Transport`), который регистрируется под одной или несколькими URL-схемами.

Это **скелет**: архитектура, интерфейсы, рабочие HTTP/FTP и заглушки для остальных протоколов.

## Быстрый старт

```bash
pip install -e ".[dev]"
pytest

fta schemes
fta cp ./report.pdf https://example.com/upload/report.pdf
fta cp ftp://user:pass@ftp.example.com/pub/data.zip ./data.zip
fta cp ./photo.jpg ftps://ftp.example.com/photos/photo.jpg --dst-opt timeout=60
fta ls ftp://ftp.example.com/pub
```

Копирование работает в любом направлении: локальный путь → протокол, протокол → локальный
путь, протокол → протокол (через временный файл на диске).

## Архитектура

```
src/fta/
├── cli.py                 # fta cp / ls / schemes
├── core/
│   ├── endpoint.py        # разбор "путь или URL" → Endpoint
│   ├── transport.py       # абстрактный Transport, RemoteEntry, Progress
│   ├── registry.py        # схема → класс транспорта; загрузка плагинов
│   ├── manager.py         # copy(src, dst): маршрутизация и staging
│   └── errors.py
└── transports/
    ├── http.py            # http, https          — готово
    ├── ftp.py             # ftp, ftps            — готово
    └── planned.py         # sftp, dav, smb, s3   — заглушки
```

Ключевые решения:

- **Протокол = класс.** Операции (`connect`, `upload`, `download`, `listdir`, `mkdir`,
  `delete`) опциональны, кроме сессии. Нереализованная операция бросает `UnsupportedOperation`.
- **N×M через staging.** Протокол умеет только загрузку и скачивание. Передача между двумя
  удалёнными протоколами идёт через временный локальный файл. Это дороже по диску, но
  добавляет протокол сразу со всеми остальными.
- **Плагины.** Транспорт регистрируется декоратором `@register` или через entry point
  группы `fta.transports` в чужом пакете. Ядро менять не нужно.
- **Прогресс.** Везде один и тот же колбэк `(done, total)`. Обёртки `Progress` и
  `wrap_reader` считают байты на уровне протокольной библиотеки.

## Статус протоколов

| Протокол | Схемы | Статус | Библиотека / основа |
|---|---|---|---|
| Локальные пути | `""`, `file://` | готово (ядро) | `shutil` |
| HTTP / HTTPS | `http`, `https` | готово: GET, PUT; Basic-авторизация | `urllib` |
| FTP / FTPS | `ftp`, `ftps` | готово: upload, download, list, mkdir, delete | `ftplib` |
| SFTP | `sftp` | заглушка | paramiko |
| WebDAV | `dav`, `davs` | заглушка | HTTP-методы поверх `http` |
| SMB / CIFS | `smb` | заглушка | smbprotocol |
| Amazon S3 и совместимые | `s3` | заглушка | boto3 |

Протоколов «все существующие» много: SCP, NFS, rsync, облачные диски с OAuth
(Google Drive, Dropbox, OneDrive), GCS, Azure Blob, BitTorrent и magnet-ссылки, IPFS,
Syncthing, Bluetooth OBEX, Wi-Fi Direct, QUIC/HTTP3, WebRTC, Magic Wormhole, Tor/onion.
Схема уже открыта для всех них: каждый — это отдельный модуль в `transports/`.

## Как добавить протокол

```python
# src/fta/transports/sftp.py
from fta.core.registry import register
from fta.core.transport import Transport

@register
class SftpTransport(Transport):
    schemes = ("sftp",)

    def connect(self):
        ...  # paramiko.SSHClient().connect(...), open_sftp()

    def close(self):
        ...

    def upload(self, local, remote, progress=None):
        ...

    def download(self, remote, local, progress=None):
        ...
```

Затем добавьте модуль в `BUILTIN_MODULES` в `core/registry.py` или опубликуйте пакет
с entry point:

```toml
[project.entry-points."fta.transports"]
myproto = "my_pkg.transport:MyTransport"
```

## Ограничения скелета и планы

- Копируется один файл. Рекурсивная передача директорий — следующий шаг.
- Нет повторных попыток, возобновления (resume) и параллельной передачи.
- Пароль можно передать в URL, но он попадёт в историю shell. Лучше передавать
  секреты через `--src-opt password=...` и переменные окружения.
- Протоколы вида «отправитель и получатель договариваются кодом» (Magic Wormhole,
  обнаружение в LAN) не укладываются в модель «путь → путь». Для них понадобится
  второй интерфейс, например `send(file)` / `receive(code)`.
- Для SFTP, S3 и других заглушек нужны опциональные зависимости: `pip install -e ".[sftp,s3]"`.

## Разработка

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest
```

Перед открытием PR убедитесь, что тесты проходят. CI запускает их на Python 3.10–3.13.
Новый протокол — это новый модуль в `src/fta/transports/` и тесты, которые не ходят в сеть
(см. `tests/test_manager.py`, там описан in-memory транспорт).

## Лицензия

[MIT](LICENSE)
