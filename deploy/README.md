# deploy — установка llmhub на 65.109.95.35 (Docker, T-371)

> TL;DR: llmhub ставится четырьмя контейнерами `llmhub-api`, `llmhub-reaper`, `llmhub-weights-auth` и `llmhub-weights-nginx`. У всех `--network host`, работа не от root, rootfs только для чтения, секреты лежат файлами (в Env только `*_FILE`). Образ собирается локально (`docker/build.sh`) и переносится tar-архивом; сервер ничего не скачивает. Шаги сервера выполняет `server/llmhub-deploy.sh <step> check|apply|rollback`, у каждого есть проверка и откат. Эксплуатация описана в `docs/runbook.md`.

## Состав
| Файл | Что |
|---|---|
| `docker/Dockerfile`, `docker/.dockerignore` | образ `llmhub` (база закреплена по digest, wheelhouse с хешами, `USER 64100`, без секретов и конфигов) |
| `docker/images.env` | закреплённые образы: python, nginx (сервер), ubuntu и dind (только тест F5) |
| `docker/build.sh` | сборка: wheel → `uv export` → `pip download --require-hashes` → `docker build --pull=false` → `docker save` (llmhub и nginx) → `image.id`, `nginx.id` (digest индекса) и `image.config-id`, `nginx.config-id` (digest конфига из `manifest.json` tar, для классического хранилища образов, R3-5), `SHA256SUMS` в `dist/<ver>/` |
| `docker/run-args/*.args` | аргументы `docker run` каждого контейнера (по одному на строку, `@IMAGE@` = Id образа); источник истины для скрипта и тестов |
| `server/llmhub-deploy.sh` | шаги S0–S14 (план T-371 §6): `s0 s1 s2 s3 s4 s5 s6 s7 s8a s8b s8c s9 s10x s11 s14` |
| `config.example.yaml` | шаблон серверного конфига (Mock, `api.host 10.8.0.1`, `docs: false`, клиенты `voicebot`, `gamedev`, `agents`, `e2e`) |
| `render.py` | `config` (хеши ключей, `--set`, `--weights` из sha256-файла) и `nginx` (подстановка `__X__`, остаток — ошибка) |
| `nginx-weights.conf` | шаблон nginx весов: TLS 1.3, `auth_request`, `limit_rate __RATE__`, `limit_conn w_all __MAX_DL__`, `directio 4m` |
| `gen_weights_cert.py <ip> <outdir>` | self-signed EC P-256, IP SAN, 10 лет; ключ 0600; печатает sha256 |
| `systemd/llmhub-backup.{service,timer}` | ежедневный `docker exec llmhub-api python -m llmhub backup` |
| `logrotate-llmhub-acct` | ротация учёта весов (F12) |

## Контейнеры
| Имя | Пользователь | Слушает | Секреты (ro) | Прочие монтирования |
|---|---|---|---|---|
| `llmhub-api` | 64100 + группы 998 (ключ llama), 64102 (секрет весов) | `10.8.0.1:8095` | `label`, `instance`, `weights`, `providers` | `/etc/llmhub/conf` ro, `/etc/llama/api-key` ro, `weights-pub` ro, `/var/lib/llmhub` rw |
| `llmhub-reaper` | 64100 | — | `label` | `/etc/llmhub/conf` ro, `/var/lib/llmhub` rw (heartbeat `reaper.hb`) |
| `llmhub-weights-auth` | 64102 + группа 64100 | `127.0.0.1:8096` | `weights` | `/etc/llmhub/conf` ro, `/var/lib/llmhub` ro, `/var/lib/llmhub-weights-auth` rw, `/var/log/llmhub` ro, `weights-pub` ro |
| `llmhub-weights-nginx` | 64101 + группа 64100 | `65.109.95.35:9443` | — | `nginx-weights.conf` ro, `/etc/llmhub/weights` ro (cert + key 0600), `/opt/models` ro, `/var/log/llmhub` rw |

Общие флаги: `--network host` (`-p` обходит ufw — запрещено тестом), `--read-only`, `--tmpfs /tmp`, `--cap-drop ALL`, `no-new-privileges`, `--pids-limit 256`, `--memory`, `--restart unless-stopped`, `--log-driver journald`, healthcheck. Docker не перезапускает unhealthy-контейнер, поэтому у reaper есть самосторож: если проход завишет, процесс завершается с кодом 70.

## Владельцы и права на хосте
| Путь | Владелец / права |
|---|---|
| `/etc/llmhub` | `root:root 0755`; `config.yaml.bak-<ts>` (прежние версии, вне монтирования) |
| `/etc/llmhub/conf` | `root:llmhub 0750`; `config.yaml` 0640 `root:llmhub`. Монтируется **каталогом** (SEC-D2): `mv` внутри каталога работающий контейнер видит, а у монтирования одного файла остался бы старый inode |
| `/etc/llmhub/secrets` | `root:root 0700`; `label`, `instance` 0640 `root:llmhub`; `weights` 0640 `root:llmhub-wauth`; `providers` 0600 (пустой) |
| `/etc/llmhub/admin.key`, `/etc/llmhub/keys/*.key` | 0600 root (ключи создаёт `keys new --out`, на экран выводится только sha256) |
| `/etc/llmhub/weights` | `root:llmhub-wnginx 0750`; `weights-key.pem` 0600 `llmhub-wnginx` |
| `/etc/llmhub/weights-pub` | 0755; `weights-cert.pem` 0644 (`weights.sources[].ca_pem_file`) |
| `/var/lib/llmhub` | `llmhub:llmhub 0750`; `state.db*` 0640 (umask 0027 в `serve`/reaper) |
| `/var/lib/llmhub-weights-auth` | `llmhub-wauth 0700` |
| `/var/log/llmhub` | `root:llmhub 0750`; `weights-*.log` 0640 `llmhub-wnginx:llmhub` |
| `/var/lib/llmhub-deploy` | 0700 root: маркеры шагов, `hashes.txt`, снимок `preflight/baseline` (с `docker-store.txt`), `image-<ver>.local-id`, `nginx-<ver>.local-id` (Id образов после `s3`), `s2.created` (созданные пользователи), `ufw-*.added` (добавленные правила), временный `stage/` (0700 `llmhub`, только на время render) |

Пользователи (`useradd --system`, `nologin`): `llmhub` 64100, `llmhub-wnginx` 64101, `llmhub-wauth` 64102. id фиксированы, поэтому переустановка после S14 и восстановление из архива сохраняют владельцев (F13).

## Порядок (план T-371 §6)
- **Окно 1**: `s0` (снимок, только чтение) → `s1` (SHA256SUMS) → `s2` → `s3` → `s4` → `s5` → `s6` → `s7` → e2e (`scripts/e2e_mock.py pre`, `docker restart llmhub-api`, `post-restart`, `kill`) → `s11`.
- **Окно 2**: `s8a` → `s8b` (`LLMHUB_WEIGHTS_MODEL=<rel> [LLMHUB_WEIGHTS_DRAFT=<rel>]`; render идёт от uid 64100: нужны o+r на `/opt/models/sha256-T365.txt` и GGUF, o+x на `/opt/models` и каталоги моделей — `s0` предупреждает `WARN`, R3-4) → `s8c` → `s9` → проба `docker exec llmhub-weights-auth python -m llmhub.tools.weights_probe --config /etc/llmhub/conf/config.yaml --op auto --negatives [--expect-sha256 …]` (сразу после `scripts/e2e_mock.py prewarm` — Mock-инстанс в BOOTING около 60 с; admin `hold` GPU не запускает, он только держит уже живую; затем `close-lease --lease <id>`) → `s10x` (в конце T-371: клиент `e2e` удаляется, ключ — `shred`; до этого он нужен пробе) → `s11`.
- Новый конфиг (`s5`, `s8b`, `s10x`) сначала проходит `config-check` как `config.yaml.new` и только потом заменяет живой; новый `nginx-weights.conf` — `nginx -t` как `.new` (R3-1, R3-3). При отказе живой файл не меняется.
- Каждый шаг: `apply` → `check` = 0. Неожиданный вывод — остановиться и разобрать. Откат: `rollback` того же шага; полный откат — `s14`.
- Изменение конфига (`s5`, `s8b`, `s10x` и их откаты) перезапускает `llmhub-api`, `llmhub-reaper` и `llmhub-weights-auth`, если они есть. Изменение `nginx-weights.conf` в `s8c` перезапускает `llmhub-weights-nginx`. render и `keys new` работают от uid 64100 во временном каталоге `stage/`, а не от root поверх `/etc/llmhub` (L6).
- `s9 check` проходит через `lo`. Правило ufw на публичном интерфейсе проверяется только **с внешнего хоста** — команды в `docs/runbook.md` §6 (L7).
- ufw: скрипт удаляет только правила, которые сам добавил (`ufw-*.added`). Если на 8095/9443 уже есть чужое правило, `s0`/`s7`/`s9` останавливаются (L1).
- **Шаблон `config.example.yaml` вне сервера — только в контейнере с `--network none`** (SEC-D4). В шаблоне настоящие `10.8.0.1:809x`, и через VPN рабочей станции фоновые пробы дошли бы до llama-server. Тест F5 так и запускает «сервер» и проверяет, что в отрендеренных конфигах нет `10.8.0.1`/`65.109.95.35`. `tests/conftest.py` запрещает тестам `connect` к 10.8.0.0/24 и 65.109.95.35.
- Локальная проверка скрипта (F5): `uv run pytest -m docker tests/test_t371_deploy_script.py`. Тест запускает одноразовый ubuntu-«сервер» со своим dockerd и прогоняет `apply`/повторный `apply`/`rollback` для `s2`…`s9`, затем пробу весов, `s10x`, `s11` и `s14`.

## Таймауты nginx весов
- `/_auth`: `proxy_connect_timeout 2s`, `proxy_send_timeout 5s`, `proxy_read_timeout 5s` — зависший валидатор не держит под.
- `send_timeout 60s` — застрявшее соединение пода рвётся, под докачивает по Range; `keepalive_timeout 15s`.
- API: перед `10.8.0.1:8095` прокси нет; если он появится — абзац «Прокси перед API» в `docs/integration.md`.

## Толкование ADR-0138 п. 4 (записать в T-371)
Токен сервера весов **один на инстанс и многоразовый** для Range и повторов; отзывается проверкой состояния (только PROVISIONING/BOOTING/LOADING_MODEL) и сроком `exp` = `created_at + provisioning_s + booting_s + loading_model_s`; плюс лимит объёма 1,2 × Σsize и привязка к первому IP. Это уточнение формулировки «одноразовый», новый ADR не нужен.

## Журналы и ротация (ревью T-370, SEC-2/11/12/13)
- `weights-acct.log` пишет только location с `.gguf` и только для ответов 200/206 (`map $status $acct_ok` + `access_log … if=$acct_ok`). Страницы ошибок (401/403/404/503) объём op не тратят. Catch-all `location /` не логируется (`access_log off`), на уровне server стоит `limit_req` (зона `w_req`). Валидатор читает хвост acct-лога кусками по ≤ 1 МиБ и сдвигает смещение только до последнего `\n`.
- logrotate (T-371, `deploy/logrotate-llmhub-acct` → `/etc/logrotate.d/llmhub-acct`) для `/var/log/llmhub/weights-*.log`: `create 0640 llmhub-wnginx llmhub`, `maxsize 50M`, `rotate 7`. В `postrotate` стоит `docker kill -s USR1 llmhub-weights-nginx`, но только если контейнер запущен (`docker inspect -f '{{.State.Running}}'`). Скрипт никогда не завершается ошибкой (F12). Каталог `root:llmhub 0750` не открыт на запись пользователю nginx, поэтому `su` не нужен и `postrotate` выполняется от root. **Без `copytruncate`**: если усечь тот же inode, а файл к этому моменту вырос дальше старого смещения, строки пропадут, то есть объём будет недоучтён (SEC-11).
- Журнал валидатора (SEC-12): `main()` настраивает `logging.basicConfig(level=INFO)` в stderr, строки попадают в journald (`journalctl CONTAINER_NAME=llmhub-weights-auth`). В строке только `op=<op_id> verdict=<…>`, без токена, URI-запроса и IP. Uvicorn запускается с `log_level=warning` и `access_log=False`.
- SEC-13 (принято): `exp` с ведущими нулями проходит проверку, потому что подпись считается по `int` (op и exp те же, вреда нет). Объём ответа, который ещё передаётся, учитывается только по его завершении, поэтому превышение лимита ограничено величиной `segments × size`.
- `If-Range` (SEC-1): nginx передаёт его валидатору как `X-If-Range`. Если значение непустое, списывается весь файл: при несовпадении валидатора nginx отдаёт 200 с полным телом. Действует `max_ranges 1`; multi-range обрабатывается как полный файл и так же списывается целиком.
- Ошибки SQLite в `auth-state.db` (SEC-8) → 503 `db_busy` (отказ закрытый: под уходит на HF).
- `LLMHUB_INSTANCE_SECRET` и `LLMHUB_WEIGHTS_SECRET` обязаны различаться: `load_instance_secrets` отвергает одинаковые значения (SEC-3).

## Ключ TLS пода (SEC-10)
Супервизор пишет `tls.crt`/`tls.key` режима `direct` в `/dev/shm/llmhub` (tmpfs, каталог 0700, файлы 0600), а не в слой контейнера, поэтому ключ не остаётся на диске арендованного хоста. В контейнерах RunPod `/dev/shm` — это tmpfs (`docker run --shm-size`). Если его нет, проверить при T-372. Ключ всё равно виден провайдеру через env контейнера (`LLMHUB_TLS_KEY_PEM`), так что модель угроз не меняется: защищается только диск хоста после завершения пода.

## RunPod (T-372)
- Ключ провайдера — файл `/etc/llmhub/secrets/runpod` (`0400 64100:0`). Его кладёт оператор по ssh через stdin; скрипт ключ не создаёт и не печатает. Шаг `s6k`: `apply` монтирует ключ (`run-args/_runpod.args` через `@include-if-secret runpod`) только в api и reaper и пересоздаёт их, `check` сверяет монтирование, `rollback` делает `shred -u` и убирает монтирование.
- Шаблон конфига теста — `config.t372.example.yaml`; выбирается переменной `LLMHUB_CONFIG_TEMPLATE`. Порядок источников весов — `LLMHUB_WEIGHTS_CHAIN`, адрес HF — `LLMHUB_HF_BASE_URL`.
- Образ пода: `docker/publish-infer.sh scan` — сборка, `docker save` и проверка каждого слоя (`scripts/image_secret_scan.py`). `docker/publish-infer.sh push` — push в GHCR, только после чистого скана того же image id. Токен берётся из файла через `--password-stdin`, `DOCKER_CONFIG` временный. Digest пишется в `dist/<ver>/infer.digest`.
- Сторож на рабочей станции (`workstation/`): `llmhub-runpod-guard.{service,timer}` и `guard.env.example` (в нём только пути и срок `GUARD_UNTIL` = конец теста + 6 ч). Сторож — stdlib, Python ≥ 3.10 (юнит: `/usr/bin/python3`, на рабочей станции 3.10). Ставится на время теста; порядок — `docs/t372-live-test.md` L0/L8; в L0 ручной `watch --once` тем же интерпретатором, в L2 — `systemctl --user start` юнита до пробы.
