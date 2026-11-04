# deploy — сервер весов и заметки для T-371

> TL;DR: Что T-370 оставляет для установки в T-371: отдельный экземпляр nginx для весов (`nginx-weights.conf`), валидатор токенов `llmhub-weights-auth`, сертификат `gen_weights_cert.py`, два секрета контроллера. Здесь — пользователи, права, таймауты и порядок установки; на сервере в T-370 ничего не открыто и не установлено.

## Состав
| Файл | Что |
|---|---|
| `nginx-weights.conf` | шаблон отдельного nginx (свой pid, только публичный IPv4, TLS 1.3, `auth_request`, `limit_rate`, `limit_conn`) |
| `gen_weights_cert.py <ip> <outdir>` | self-signed EC P-256, IP SAN, CA:FALSE, 10 лет; ключ 0600; печатает sha256 |
| `python -m llmhub.weights.auth_app --config …` | валидатор токенов на `weights.auth_listen` (127.0.0.1:8096) |

Плейсхолдеры шаблона: `__PUBLIC_IP__ __PORT__(9443) __RATE__(80m) __SEGMENTS__(1) __MODELS__(/opt/models) __AUTH__(127.0.0.1:8096) __LOGDIR__(/var/log/llmhub) __CERT__ __KEY__ __PID__ __TMP__`.

## Пользователи и права (план T-370, раздел 2a п. 5)
| Объект | Владелец / права |
|---|---|
| контроллер `llmhub.service` | `llmhub:llmhub`, `UMask=0027` |
| каталог данных | 0750 `llmhub:llmhub` |
| `state.db`, `-wal`, `-shm` | 0640 `llmhub:llmhub` (валидатор читает через группу) |
| `llmhub-weights-auth.service` | пользователь `llmhub-wauth`, `SupplementaryGroups=llmhub`, `StateDirectory=llmhub-weights-auth` (0700) |
| nginx весов | `www-data`, читает `/opt/models`, пишет `/var/log/llmhub` |
| `/var/log/llmhub` | 0750 `www-data:llmhub` |
| `weights-acct.log` | 0640, **группа `llmhub`** (иначе валидатор не видит объём; проверить после первой ротации — `logrotate create 0640 www-data llmhub`) |
| `/etc/llmhub/secrets.env` (`LLMHUB_WEIGHTS_SECRET`) | 0640 `root:llmhub-wauth`; контроллеру — через `EnvironmentFile` |
| `/etc/llmhub/instance.env` (`LLMHUB_INSTANCE_SECRET`) | 0600 `llmhub`; валидатору **не** выдаётся |
| `/etc/llmhub/weights-cert.pem` | публичный, 0644 (`weights.sources[].ca_pem_file`) |
| ключ сертификата весов | 0600, читает только nginx |

Валидатор открывает БД `mode=ro` (`busy_timeout` 300 мс); SQLite ≥ 3.22 читает WAL без записи в `-shm`. Ошибка БД → 503 → nginx 500 → под уходит на HF.

## Таймауты nginx весов
- `/_auth`: `proxy_connect_timeout 2s`, `proxy_send_timeout 5s`, `proxy_read_timeout 5s` — зависший валидатор не держит под.
- `send_timeout 60s` — застрявшее соединение пода рвётся, под докачивает по Range; `keepalive_timeout 15s`.
- Отдельно (API, не веса): nginx перед `10.8.0.1:8095` — `proxy_read_timeout` > 480 с и `proxy_buffering off` для SSE (BOARD «Входящие» от T-369).

## Порядок установки (T-371)
1. Пользователи `llmhub`, `llmhub-wauth`; каталоги и права по таблице.
2. Секреты: `LLMHUB_INSTANCE_SECRET`, `LLMHUB_WEIGHTS_SECRET` (≥ 32 байт, `openssl rand -base64 48`), разные.
3. `python deploy/gen_weights_cert.py 65.109.95.35 /etc/llmhub/weights` → `ca_pem_file`.
4. Конфиг: `weights.sets` — размеры и sha256 из `/opt/models/sha256-T365.txt`; `hf` — `revision` коммитом (Q4).
5. Отрендерить `nginx-weights.conf`, `nginx -t`, юнит `llmhub-weights-nginx.service` (свой pid, `www-data`); порт 9443 открыть только для публичного IPv4 (разрешение заказчика 2026-11-04). VPN-интерфейс и юниты llama-server 8090–8092 не трогать.
6. Юнит валидатора; опрос `GET /metrics` (только 127.0.0.1, `llmhub_weights_auth_total{verdict}`) и алерт.
7. Композиция контроллера: `materials=make_material_fns(cfg, load_instance_secrets(cfg))`, для реальных провайдеров `make_cloud_inference(conn, secrets, cfg, clock)` (инцидент `tls_pin_mismatch` high), для Mock — `MockInference`.
8. Опционально: `tc`/QoS для VPN (Q1) — вопрос заказчику.

## Толкование ADR-0138 п. 4 (записать в T-371)
Токен сервера весов **один на инстанс и многоразовый** для Range и повторов; отзывается проверкой состояния (только PROVISIONING/BOOTING/LOADING_MODEL) и сроком `exp` = `created_at + provisioning_s + booting_s + loading_model_s`; плюс лимит объёма 1,2 × Σsize и привязка к первому IP. Это уточнение формулировки «одноразовый», новый ADR не нужен.

## Журналы и ротация (ревью T-370, SEC-2/11/12/13)
- `weights-acct.log` пишет только location с `.gguf` и только для ответов 200/206 (`map $status $acct_ok` + `access_log … if=$acct_ok`). Страницы ошибок (401/403/404/503) объём op не тратят. Catch-all `location /` не логируется (`access_log off`), на уровне server стоит `limit_req` (зона `w_req`). Валидатор читает хвост acct-лога кусками по ≤ 1 МиБ и сдвигает смещение только до последнего `\n`.
- logrotate (T-371) для `/var/log/llmhub/weights-*.log`: `create 0640 www-data llmhub`, `maxsize 50M`, `rotate 7`, `postrotate kill -USR1 $(cat <pid весов>)`. **Без `copytruncate`**: усечение того же inode, выросшего дальше старого смещения, ведёт к пропуску строк, то есть к недоучёту объёма (SEC-11).
- Журнал валидатора (SEC-12): `main()` настраивает `logging.basicConfig(level=INFO)` в stderr, под systemd строки попадают в journald (`journalctl -u llmhub-weights-auth`). В строке только `op=<op_id> verdict=<…>`, без токена, URI-запроса и IP. Uvicorn запускается с `log_level=warning` и `access_log=False`.
- SEC-13 (принято): `exp` с ведущими нулями проходит проверку, потому что подпись считается по `int` (op и exp те же, вреда нет). Объём ответа, который ещё передаётся, учитывается только по его завершении, поэтому превышение лимита ограничено величиной `segments × size`.
- `If-Range` (SEC-1): nginx передаёт его валидатору как `X-If-Range`. Если значение непустое, списывается весь файл: при несовпадении валидатора nginx отдаёт 200 с полным телом. Действует `max_ranges 1`; multi-range обрабатывается как полный файл и так же списывается целиком.
- Ошибки SQLite в `auth-state.db` (SEC-8) → 503 `db_busy` (отказ закрытый: под уходит на HF).
- `LLMHUB_INSTANCE_SECRET` и `LLMHUB_WEIGHTS_SECRET` обязаны различаться: `load_instance_secrets` отвергает одинаковые значения (SEC-3).

## Ключ TLS пода (SEC-10)
Супервизор пишет `tls.crt`/`tls.key` режима `direct` в `/dev/shm/llmhub` (tmpfs, каталог 0700, файлы 0600), а не в слой контейнера, поэтому ключ не остаётся на диске арендованного хоста. В контейнерах RunPod `/dev/shm` — это tmpfs (`docker run --shm-size`). Если его нет, проверить при T-372. Ключ всё равно виден провайдеру через env контейнера (`LLMHUB_TLS_KEY_PEM`), так что модель угроз не меняется: защищается только диск хоста после завершения пода.
