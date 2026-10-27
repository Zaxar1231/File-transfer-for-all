# fta — File Transfer for All

[![CI](https://github.com/Zaxar1231/File-transfer-for-all/actions/workflows/ci.yml/badge.svg)](https://github.com/Zaxar1231/File-transfer-for-all/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](pyproject.toml)

Универсальный инструмент для передачи файлов: один API и одна команда для всех протоколов.
Ядро ничего не знает о конкретных протоколах. Каждый протокол — это отдельный плагин
(класс `Transport`), который регистрируется под одной или несколькими URL-схемами.

Сейчас работают локальная ФС, HTTP(S), WebDAV, FTP(S), SFTP и S3. Файлы и целые
директории копируются между любой парой протоколов.

## Установка

```bash
pip install -e .            # ядро: локальная ФС, HTTP, WebDAV, FTP — без зависимостей
pip install -e ".[sftp]"    # + SFTP (paramiko)
pip install -e ".[s3]"      # + S3 (boto3)
pip install -e ".[all]"     # всё сразу
```

## Использование

```bash
fta schemes                                              # какие протоколы доступны

fta cp ./report.pdf https://example.com/upload/report.pdf
fta cp ftp://user@ftp.example.com/pub/data.zip ./ --src-opt password=secret
fta cp -r ./photos sftp://me@nas.local/backup/ --dst-opt key_file=~/.ssh/id_ed25519
fta cp -r sftp://me@nas.local/backup/photos s3://my-bucket/archive/   # протокол → протокол
fta cp ./notes.md davs://cloud.example.com/remote.php/dav/files/me/ --dst-opt username=me

fta ls s3://my-bucket/archive
fta stat dav://localhost:8080/docs/a.txt
fta mkdir -p ftp://user@host/new/dir --opt password=secret
fta rm sftp://me@nas.local/tmp/old.log
```

Правила копирования:

- Если назначение — существующая директория или путь заканчивается на `/`, источник
  кладётся внутрь под своим именем.
- `-r` копирует директорию целиком. Без `-r` копируется один файл.
- Передача между двумя удалёнными протоколами идёт через временный файл на локальном диске.

Опции передаются как `KEY=VALUE`: `--src-opt` для источника, `--dst-opt` для назначения,
`--opt` для `ls`/`stat`/`mkdir`/`rm`. Пароль в URL (`ftp://user:pass@host`) тоже работает,
но попадает в историю shell, поэтому лучше `--opt password=...`.

### Из Python

```python
from fta import copy, open_transport

copy("./photos", "sftp://me@nas.local/backup/", recursive=True,
     destination_options={"key_file": "/home/me/.ssh/id_ed25519"})

with open_transport("s3://my-bucket", region="eu-central-1") as s3:
    for entry in s3.listdir("/archive"):
        print(entry.name, entry.size)
```

## Протоколы

| Протокол | Схемы | Операции | Библиотека | Опции |
|---|---|---|---|---|
| Локальная ФС | путь, `file://` | все | stdlib | — |
| HTTP / HTTPS | `http`, `https` | download (GET), upload (PUT), stat (HEAD), delete | stdlib | `username`, `password`, `timeout` |
| WebDAV | `dav`, `davs` (= http/https) | все | stdlib | `username`, `password`, `timeout` |
| FTP / FTPS | `ftp`, `ftps` (explicit TLS) | все; list/stat требуют MLSD/MLST | stdlib | `username`, `password`, `timeout` |
| SFTP | `sftp` | все | paramiko | `username`, `password`, `key_file`, `known_hosts`, `accept_unknown_host`, `timeout` |
| S3 и совместимые (MinIO, R2, Ceph) | `s3://bucket/key` | все; `mkdir` ничего не делает | boto3 | `endpoint_url`, `region`, `profile`, `access_key`, `secret_key` |
| SMB / CIFS | `smb` | заглушка | smbprotocol (план) | — |

Особенности:

- **SFTP** проверяет ключ хоста по `~/.ssh/known_hosts`. Неизвестный хост отклоняется,
  пока не указано `accept_unknown_host=true`.
- **S3** берёт учётные данные из стандартной цепочки AWS (переменные окружения, `~/.aws`,
  роль инстанса), если они не заданы опциями. Директорий в S3 нет, поэтому пустые
  директории не копируются.
- **HTTP** не умеет листинг директорий; для этого есть WebDAV.

Дальше в планах: SMB, SCP, NFS, rsync, облачные диски с OAuth (Google Drive, Dropbox,
OneDrive), GCS, Azure Blob, BitTorrent и magnet-ссылки, IPFS, Bluetooth OBEX,
Wi-Fi Direct, QUIC/HTTP3, WebRTC, Magic Wormhole. Каждый — отдельный модуль в `transports/`.

## Архитектура

```
src/fta/
├── cli.py                 # fta cp / ls / stat / mkdir / rm / schemes
├── core/
│   ├── endpoint.py        # разбор "путь или URL" → Endpoint
│   ├── transport.py       # абстрактный Transport, RemoteEntry, Progress, translate_errors
│   ├── registry.py        # схема → класс транспорта; загрузка плагинов
│   ├── manager.py         # copy(): файлы и деревья, маршрутизация, staging
│   └── errors.py          # TransferError, NotFound, UnsupportedScheme, UnsupportedOperation
└── transports/
    ├── local.py           # "", file
    ├── http.py            # http, https
    ├── webdav.py          # dav, davs
    ├── ftp.py             # ftp, ftps
    ├── sftp.py            # sftp
    ├── s3.py              # s3
    └── planned.py         # smb — заглушка
```

Ключевые решения:

- **Протокол = класс.** Операции `upload`, `download`, `listdir`, `stat`, `mkdir`, `delete`
  опциональны. Нереализованная операция бросает `UnsupportedOperation`.
- **Локальная ФС — тоже транспорт.** Менеджер копирования работает с любыми двумя
  транспортами одинаково.
- **N×M через staging.** Протоколу достаточно уметь загрузку и скачивание. Передача между
  двумя удалёнными протоколами идёт через временный локальный файл. Это дороже по диску,
  но новый протокол сразу работает со всеми остальными.
- **Единые ошибки.** Исключения библиотек (`ftplib`, paramiko, botocore) превращаются в
  `TransferError`, отсутствие пути — в `NotFound`. CLI печатает их одной строкой.
- **Плагины.** Транспорт регистрируется декоратором `@register` или через entry point
  группы `fta.transports` в стороннем пакете. Ядро менять не нужно.
- **Прогресс.** Везде один колбэк `(done, total)`. `Progress` потокобезопасен, потому что
  boto3 передаёт части файла параллельно.
- **Тяжёлые зависимости опциональны.** paramiko и boto3 импортируются только при
  подключении, поэтому ядро ставится без них.

## Как добавить протокол

```python
# src/fta/transports/myproto.py
from fta.core.registry import register
from fta.core.transport import Transport, translate_errors

@register
class MyTransport(Transport):
    schemes = ("myproto",)
    library_errors = (MyLibError,)      # станут TransferError

    def connect(self):
        ...                             # self.endpoint, self.username, self.password, self.options

    def close(self):
        ...

    @translate_errors
    def upload(self, local, remote, progress=None):
        ...

    @translate_errors
    def download(self, remote, local, progress=None):
        ...
```

Затем добавьте модуль в `BUILTIN_MODULES` в `core/registry.py` или опубликуйте пакет
с entry point:

```toml
[project.entry-points."fta.transports"]
myproto = "my_pkg.transport:MyTransport"
```

Для теста добавьте фикстуру с сервером в `tests/test_protocols.py` и вызовите
`roundtrip_tree`: он загружает дерево, проверяет листинг и `stat`, скачивает обратно и
удаляет файл.

## Ограничения и планы

- Нет повторных попыток, возобновления (resume) и параллельной передачи файлов.
- Передача между двумя удалёнными протоколами требует места на локальном диске под
  самый большой файл, а прогресс показывается дважды (скачивание, затем загрузка).
- Протоколы вида «отправитель и получатель договариваются кодом» (Magic Wormhole,
  обнаружение в LAN) не укладываются в модель «путь → путь». Для них понадобится
  второй интерфейс, например `send(file)` / `receive(code)`.

## Разработка

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest
```

`tests/test_protocols.py` поднимает настоящие серверы прямо в процессе: FTP (pyftpdlib),
WebDAV и HTTP (WsgiDAV + cheroot), SFTP (asyncssh) и S3 (moto). Сеть не нужна.
CI запускает тесты на Python 3.10–3.13.

## Лицензия

[MIT](LICENSE)
