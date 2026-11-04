# План T-371 (v2 после ворот): деплой llmhub в Docker на 65.109.95.35 (Mock), e2e, integration.md

> TL;DR: v2 учитывает решения заказчика от 2026-11-04. Всё ставится как Docker-контейнеры `llmhub-*`, у каждого `--network host`. Образ собирается из нашего wheel и wheelhouse, база закреплена по digest. Работа внутри не от root, rootfs только для чтения, секреты — файлами ro (в `docker inspect` видны только пути). `nginx-core` через apt и venv на хосте не используются. Установка в два окна: **окно 1** — API + reaper + e2e на Mock; **окно 2** — nginx весов + валидатор + `ufw 9443` + проба (после пробы порт остаётся открыт), лимит 80m.
> Код делается локально по TDD, потоки A–H. В T-371: integrity busy, CLI с `clear-weights-hold` и `backup`, SEC-L2, лимит 429, ленивый ключ llama, только GET-пробы, сторож reaper, порог MemAvailable, `mock_only`. В T-372 перенесены пачки `IN` и `host_blacklist_s≥3600`. Шаги сервера S0–S14 выполняет координатор, у каждого есть check/apply/rollback. Скрипт проверяется локально в одноразовом контейнере (F5). Регистрация MCP `llmhub_ask` в projectVoiceBot и gameDev разрешена и делается после деплоя (S12). GHCR — опционально, не выполняется.

Задача: `tasks/active/T-371-deploy.md` · `deploy/README.md` · `docs/integration.md` · BOARD «Входящие» · ADR-0138 п. 1, 12–13. Ворота 2026-11-04: critic — «accept with changes», advocate — «needs-decision»; решения заказчика (Docker, 80m, MCP) — ниже. Примечание: тексты находок F9, F12, F13 до analyst не дошли. Ниже они не сопоставлены — координатору нужно сверить их с §13.

## 0. Осмотр сервера 2026-11-04 (только чтение)
| Что | Факт | Следствие |
|---|---|---|
| ОС | Ubuntu 22.04.5, systemd 249, x86_64, 12 CPU | хост дает только systemd-таймер бэкапа и ufw |
| Docker | Server 29.9.2, cgroup v2, apparmor+seccomp, `daemon.json` нет, есть `docker.service.d/` (не трогаем); log driver по умолчанию `json-file` без ротации | у наших контейнеров явно `--log-driver journald`; dockerd не перезапускать, `daemon.json` не создавать |
| Контейнеры | 25 штук, из них 13 в `host` (xray, prometheus, grafana, loki…), `searxng` в bridge (127.0.0.1:8888) | `--network host` — привычная для этого сервера схема; чужие контейнеры не трогаем (S11 сверяет `Id/StartedAt` всех) |
| Python/nginx на хосте | Python 3.10, uv нет, nginx нет; :80 занят apache2, `65.109.95.35:443` — xray | на хост ничего не ставим, nginx в контейнере на 9443 |
| Порты | 8095, 8096, 9443 свободны | API `10.8.0.1:8095`, валидатор `127.0.0.1:8096`, веса `65.109.95.35:9443` |
| llama-server | 8090/8091/8092 слушают **10.8.0.1**, `User=llama`, ключ `/etc/llama/api-key` `root:llama 0640`, gid `llama` = **998** | ключ монтируется ro, в контейнер `--group-add 998`; URL бэкендов `http://10.8.0.1:809x/v1` |
| UID/GID | 64100–64102 свободны | фиксированные системные id для файлов на хосте (§2.3) |
| Память | 62 ГиБ, MemAvailable ≈ 31 ГиБ, swap занят 8,5 ГиБ | порог MemAvailable (F6) по умолчанию 8 ГиБ |
| Сеть | uplink 1 Гбит/с; `tun0` 10.8.0.1/24, рабочая станция 10.8.0.4 | 80m (≈640 Мбит/с) — по решению заказчика; `__MAX_DL__=1` (§2.8) |
| ufw | active, default deny | +2 правила: 8095 на tun0 (окно 1), 9443 на enp9s0 (окно 2) |
| Веса | `/opt/models` root 0755, `sha256-T365.txt` из 8 строк | ro-монтирование; nginx отдает только `.gguf` |

## 1. Объём и не-цели
**В объёме**
1. Код до деплоя: integrity busy; админ-CLI (в т. ч. `clear-weights-hold`, `backup`) + runbook; SEC-L2 docs; SEC-L4 лимит 429; секреты из файлов (`*_FILE`); ленивое чтение ключа llama только в контроллере (F1); фоновые пробы llama — только GET `/health` или `/v1/models` с интервалом (F8); самосторож reaper + healthcheck (F4/F7 advocate); порог MemAvailable у валидатора весов (F6); `mock_only` у клиента `e2e` (F7).
2. Docker: `deploy/docker/Dockerfile` (образ `llmhub`), `deploy/docker/nginx-weights.Dockerfile` (или чистый `nginx` по digest + ro-конфиг, §2.5), сборка wheelhouse, скрипт шагов сервера `deploy/server/llmhub-deploy.sh` с проверкой в одноразовом контейнере (F5).
3. e2e на Mock и проба весов; в конце T-371 клиент `e2e` удаляется (F7).
4. `docs/runbook.md`, `docs/integration.md` (рабочая версия; абзац о прокси вместо `nginx-api.example.conf`), `deploy/README.md`.
5. Сервер: окно 1 (S0–S7, S10, S11), окно 2 (S8, S9, S10w, S11), S12 MCP-регистрация.

**Не входит / перенесено**
- В T-372 (строки в BOARD «Входящие»): `waiter._tick` `IN (...)` пачками; нижняя граница `retry.host_blacklist_s ≥ 3600` (advocate F6/OPEN-3).
- `deploy/nginx-api.example.conf` — убран; перед API прокси нет, в integration.md только абзац.
- Реальные провайдеры, траты, ключ RunPod (T-372). `providers.enabled: [mock]`.
- GHCR (S13 описан, не выполняется). apt-пакеты на хосте. Изменения в dockerd, `daemon.json`, `docker.service.d`, llama-server, apache2, xray, OpenVPN, Prometheus, чужих контейнерах.
- tc/QoS — не делаем (заказчик принял 640 Мбит/с).

## 2. Ключевые решения (Docker)
1. **Сеть — `--network host` у всех четырёх контейнеров.** Обоснование:
   - (а) При `-p` Docker пишет DNAT в цепочки `DOCKER`/`DOCKER-USER` до `ufw-*`, поэтому правила ufw (источник 10.8.0.0/24 на tun0) перестают действовать. Порт `-p 10.8.0.1:8095:8095` был бы доступен любому, кто маршрутизирует до 10.8.0.1 (AWG 10.42.42.0/24, контейнерные сети), а ufw этого бы не показал.
   - (б) Контроллер ходит к llama на `10.8.0.1:809x`, nginx — к валидатору на `127.0.0.1:8096`. В host-сети это локальные адреса, не нужны `extra_hosts` и мосты.
   - (в) Процесс сам биндится на 10.8.0.1 / 127.0.0.1 / 65.109.95.35, как в конфиге. Валидатор слушает только loopback (это уже проверяет `auth_listen`).
   - (г) Так уже работают 13 контейнеров этого сервера.
   - Цена — нет сетевой изоляции. Её компенсируют `--cap-drop ALL`, `no-new-privileges`, не-root и read-only rootfs.
   - Тест E3 проверяет, что в `docker run` нет `-p`/`--publish`.
2. **Контейнеры** (имена строго `llmhub-*`; скрипт работает только с ними):
   | Имя | Образ | Пользователь | Команда |
   |---|---|---|---|
   | `llmhub-api` | `llmhub@<image id>` | `64100:64100` + `--group-add 998` | `python -m llmhub serve --config /etc/llmhub/config.yaml --no-docs` |
   | `llmhub-reaper` | тот же | `64100:64100` | `python -m llmhub.reaper --config … --loop 15 --heartbeat /var/lib/llmhub/reaper.hb` |
   | `llmhub-weights-auth` | тот же | `64102:64102` + `--group-add 64100` | `python -m llmhub.weights.auth_app --config …` |
   | `llmhub-weights-nginx` | `nginx:<1.27-alpine>@sha256:…` (pull по digest) | `64101:64101` | `nginx -c /etc/nginx-weights/nginx.conf -g 'daemon off;'` |

   Общие флаги:
   - `--read-only --tmpfs /tmp:rw,noexec,nosuid,size=64m`;
   - `--cap-drop ALL --security-opt no-new-privileges:true`;
   - `--pids-limit 256`, `--memory 1g` (nginx — 256m);
   - `--restart unless-stopped`;
   - `--log-driver journald --log-opt tag={{.Name}}`;
   - `--init` (python);
   - healthcheck (§2.6), `--label org.llmhub.release=<ver>`.

   Порт 9443 > 1024, поэтому nginx работает без root и без `CAP_NET_BIND_SERVICE`.
3. **Владельцы файлов на хосте (без входа в систему):**
   - группа/пользователь `llmhub` 64100, `llmhub-wnginx` 64101, `llmhub-wauth` 64102 (`useradd --system --uid … -s /usr/sbin/nologin -M`);
   - `/var/lib/llmhub` 0750 64100:64100 → `/var/lib/llmhub` (rw) у api и reaper, ro у weights-auth через группу 64100 (`state.db*` 0640, `UMask` = 0027 задаётся в `serve` через `os.umask`, тест);
   - `/var/lib/llmhub-weights-auth` 0700 64102 (rw у валидатора);
   - `/var/log/llmhub` 0750 64101:64100: `weights-acct.log` 0640, nginx пишет, валидатор читает ro через группу 64100.
4. **Секреты — только файлами.**
   - Код: `_load_secret(name, env)` читает `env[name]`, иначе `env[name + "_FILE"]` — путь к файлу (обычный, не симлинк, первая строка, длина ≥ минимума). В `docker inspect` Env видна только строка `LLMHUB_*_FILE=/run/llmhub-secrets/<f>`.
   - Файлы на хосте `/etc/llmhub/secrets/<f>` создаются на сервере через `openssl rand -base64 48` прямо в файл и нигде не выводятся. Каталог 0750 root:<группа>, файлы 0640 root:<группа потребителя>. В контейнер файлы попадают ro-монтированием по одному:
     - `label` — группа 64100 → api, reaper;
     - `instance` — 64100 → только api (reaper монтирует только `label`);
     - `weights` → api и weights-auth. Файл один, а групп у файла не может быть две, поэтому — 0640 `root:64102` плюс `--group-add 64102` у api. Тест сверяет матрицу монтирования.
   - `providers.env` на хосте не нужен: облако Mock, файл провайдера пустой, монтируется ro по критерию задачи (`/etc/llmhub/secrets/providers` 0600 root, пустой).
   - Admin-ключ: `/etc/llmhub/admin.key` 0600 root, на хосте, в контейнеры не монтируется. CLI запускается `docker exec -i llmhub-api python -m llmhub status --admin-key-file /dev/stdin < /etc/llmhub/admin.key` — ключ идёт через stdin и не попадает в argv/env (B1).
   - Ключи клиентов: `/etc/llmhub/keys/<n>.key` 0600 root, генерируются `docker run --rm --network none … keys new --out /out/<n>.key`. После передачи — `shred -u`.
5. **Образ `llmhub`.**
   - `deploy/docker/Dockerfile`: `FROM python:3.12-slim-bookworm@sha256:<digest>` (digest фиксирует implementer, тест требует `@sha256:` в каждом `FROM`).
   - `COPY wheelhouse/ /wheels/` → `pip install --no-index --find-links /wheels --require-hashes -r /wheels/requirements.txt`, затем `pip install --no-index --no-deps /wheels/llmhub-<ver>-py3-none-any.whl`.
   - В образе `USER 64100`, нет секретов, конфигов и весов; `HEALTHCHECK` не зашит (задаётся в `docker run`).
   - Wheelhouse собирается локально (`uv build --wheel`, `uv export --frozen --no-dev --no-emit-project`, `pip download --only-binary :all: --require-hashes --platform manylinux2014_x86_64 --python-version 3.12`), значит F10 не применим.
   - Перенос: `docker save llmhub:<ver> | gzip` → `scp` → `docker load`. Сверяется `docker image inspect -f '{{.Id}}'` с локальным, контейнеры запускаются **по Id** (`sha256:…`) — это закрепление по digest.
   - nginx: `docker pull nginx@sha256:<digest>`, конфиг монтируется ro (своего образа нет).
6. **Перезапуск и здоровье** (вместо `llmhub-reaper.timer`).
   - `--restart unless-stopped`: процесс упал — Docker перезапускает; после перезагрузки контейнеры поднимает dockerd.
   - Сам по себе Docker нездоровый контейнер не перезапускает. Поэтому:
     - reaper — самосторож: если проход не завершился за `3 × loop` (≥ 45 с), процесс делает `os._exit(70)`, и Docker его перезапускает. Каждый проход обновляет heartbeat-файл;
     - healthcheck reaper: `python -m llmhub.reaper healthcheck --heartbeat … --max-age 60`;
     - api: `python -m llmhub healthcheck --url http://10.8.0.1:8095/healthz`;
     - weights-auth: `… /metrics` на 127.0.0.1:8096;
     - nginx: `nginx -t -q` + проверка pid.
   - Состояние health видно в `docker ps` и S-проверках; алертинг — в runbook.
   - API до подъёма tun0: bind не удаётся → выход → restart с backoff Docker.
7. **Журналы**: `--log-driver journald` у всех (`journalctl CONTAINER_NAME=llmhub-api`). Ротацию делает journald — logrotate для журналов не нужен. Исключение — `weights-acct.log`: это данные учёта, которые читает валидатор, а не журнал, поэтому на хосте остаётся маленький `/etc/logrotate.d/llmhub-acct`:
   - `create 0640 64101 64100`, `maxsize 50M`, `rotate 7`, без `copytruncate`;
   - `postrotate docker kill -s USR1 llmhub-weights-nginx`.
   Это единственный `docker kill`, и только на `llmhub-*` (E4).
8. **nginx весов (F6, решение 2).**
   - `__RATE__=80m`, `__MAX_DL__=1` (один под одновременно, ≤ 640 Мбит/с суммарно; критерий «≤ 2 загрузок» выполняется). `__SEGMENTS__=1`.
   - `directio 4m; output_buffers 2 1m; sendfile on;` для `.gguf`: большие файлы идут мимо page cache и не вытесняют mmap моделей llama.
   - Порог памяти: валидатор отвечает `503 low_memory`, если `MemAvailable` (`/proc/meminfo`, в контейнере — значение хоста) < `weights.min_mem_available_mib` (8192). nginx получает 500, под уходит на HF.
   - S10w дополнительно прерывает пробу при MemAvailable < 8 ГиБ.
9. **Ключ llama (F1).**
   - `LocalBackendConfig.api_key_file`; файл монтируется ro `/etc/llama/api-key:/run/llama/api-key:ro` только в `llmhub-api` (`--group-add 998`).
   - Ключ читается **лениво**: при первом запросе к бэкенду и повторно после 401 (не чаще раза в 30 с). `load_config` файл не открывает, поэтому reaper с тем же конфигом стартует без монтирования (тест).
10. **Пробы llama (F8).** В фоне контроллер делает только `GET /health` или `GET /v1/models` (`probe: Literal["health", "models"]`, по умолчанию `health`) не чаще `probe_interval_s` (≥ 10 с). `slots` из серверного конфига убираем, в коде он остаётся (по умолчанию — `health`).
    - Цена: внешняя занятость llama (VoiceBot) определяется только по 503/таймауту, Q3.
    - Тест: за N интервалов FakeClock к фейковому llama пришли только `GET /health`, их ≤ N+1, `POST` нет.
11. **e2e (F7).** Клиент `e2e` получает `cloud_allowed: true` и `mock_only: true`. `Config` отвергает (`ConfigError`) клиента с `mock_only`, если `providers.enabled != ["mock"]`, и `config-check` тоже. В конце T-371 (S10, последний пункт) клиент `e2e` удаляется из конфига, затем `reload-clients` и `shred` ключа.
12. **integrity / SEC-L2 / SEC-L4** — как в v1: `StateDbBusy` → код 75; `ApiConfig.docs` + `--no-docs`; `ClientConfig.max_active_jobs=32` → `429 rate_limited`.
13. **Бэкап (F11).**
    - `python -m llmhub backup --config … --to /var/lib/llmhub/backup --keep 14`: `sqlite3.Connection.backup` в `*.tmp`, затем `integrity_check` копии, `os.replace`, файлы 0640.
    - Хост: `llmhub-backup.service` (oneshot, `ExecStart=/usr/bin/docker exec llmhub-api python -m llmhub backup …`) и `llmhub-backup.timer` (daily, `Persistent=true`).
    - Восстановление описано в runbook.

## 3. Файлы и интерфейсы
### Изменить
| Файл | Изменение |
|---|---|
| `src/llmhub/persistence/integrity.py`, `persistence/__init__.py` | `class StateDbBusy(RuntimeError)`; `open_state_db(path, clock, *, busy_wait_s: float = 30.0, sleep: Callable[[float], None] = time.sleep) -> OpenResult` |
| `src/llmhub/configuration/loader.py` | `_load_secret`: `NAME` или `NAME_FILE` (оба заданы → `ConfigError`); `read_secret_file(path: Path) -> bytes` |
| `src/llmhub/configuration/api_models.py` | `ClientConfig.max_active_jobs: int = Field(32, ge=1, le=10000)`, `ClientConfig.mock_only: bool = False`; `ApiConfig.docs: bool = True`; валидатор `ApiConfig.host` (запрещены wildcard/пустой); `LocalBackendConfig.api_key_file: Path \| None`, `probe: Literal["slots","health","models"] = "health"`; `LocalConfig.probe_interval_s: float = Field(30, ge=10)` |
| `src/llmhub/configuration/models.py` | валидатор `Config`: `mock_only` при не-mock провайдере → ошибка |
| `src/llmhub/configuration/weights_models.py` | `WeightsConfig.min_mem_available_mib: int = Field(8192, ge=0)` (0 — выключено) |
| `src/llmhub/api_gateway/errors.py` | `ErrorCode.RATE_LIMITED` → `(429, True, "rate_limit_error")` |
| `src/llmhub/api_gateway/submission.py` | `_check_client_limit(principal, opts) -> None` перед `controller.submit` |
| `src/llmhub/api_gateway/app.py` | `docs_url/redoc_url/openapi_url = None` при `api.docs is False` |
| `src/llmhub/local_inference/client.py` | `KeySource = Callable[[], str \| None]`; `LocalBackendClient(..., key_source: KeySource \| None)`; ленивое чтение + повтор после 401 (≤ 1 раз / 30 с); `probe()` — только `GET /health` или `GET /v1/models` (режим `models`) |
| `src/llmhub/server.py` | `build_service`: `key_source` из `api_key_file`/`api_key_env` без чтения при старте; `os.umask(0o027)` в `serve` |
| `src/llmhub/reaper/__main__.py`, `reaper/core.py` | `--heartbeat PATH`; самосторож `STALL_FACTOR = 3`; подкоманда `healthcheck --heartbeat P --max-age S` (код 0/1) |
| `src/llmhub/weights/auth_app.py` | `mem_available_mib(path="/proc/meminfo") -> int \| None`; при < порога → 503 `low_memory`, метрика `verdict="low_memory"` |
| `src/llmhub/__main__.py` | CLI (B), `serve --no-docs`, `StateDbBusy` → 75, `keys new --out`, `backup`, `healthcheck` |
| `deploy/nginx-weights.conf` | `limit_conn w_all __MAX_DL__;`, в location `.gguf`: `directio 4m; output_buffers 2 1m;`; `pid __PID__` в tmpfs; temp-пути в `__TMP__` |
| `tests/test_w_nginx_conf.py` | обновить ожидаемые строки и рендер |
| `docs/openapi.yaml` | регенерация |
| `deploy/README.md`, `docs/integration.md` | Docker-раскладка; абзац о прокси (`proxy_read_timeout` > 480 с, `proxy_buffering off`, `proxy_request_buffering off`) |

### Создать
```python
# src/llmhub/admin_cli.py — HTTP-клиент admin-API (httpx), без импортов controller
class AdminCliError(Exception): ...
def read_key_file(path: Path) -> str          # "/dev/stdin" разрешён; иначе обычный файл, mode & 0o077 == 0
def base_url(cfg: Config, override: str | None) -> str
def call(url: str, key: str, method: str, path: str,
         body: dict[str, Any] | None = None, timeout: float = 70.0) -> dict[str, Any]
def write_kill_file(data_dir: Path) -> Path
def run(argv: Sequence[str]) -> int           # 0 ok, 1 API, 2 аргументы/конфиг

# src/llmhub/persistence/backup.py
def backup_state_db(src: Path, dst_dir: Path, keep: int, now: datetime) -> Path
```
CLI: `status · budget · client list · reload-clients · hold --pool|--model --ttl · release --pool · kill [--reason] [--file] · resume [--provider] · clear-weights-hold --pool · config-check · backup --to DIR --keep N · healthcheck --url U · keys new --name N --out FILE`.

| Новый файл | Что |
|---|---|
| `deploy/docker/Dockerfile`, `deploy/docker/.dockerignore` | образ `llmhub` (§2.5) |
| `deploy/docker/build.sh` | локальная сборка: wheel, export, wheelhouse, `docker build --pull=false`, `docker save`, `SHA256SUMS`, `image.id` |
| `deploy/docker/run-args/*.args` | аргументы `docker run` для каждого контейнера (источник истины для скрипта и тестов) |
| `deploy/config.example.yaml` | серверный конфиг на Mock: `api.host 10.8.0.1`, `docs: false`; `local_backends` 8090–8092 (`api_key_file: /run/llama/api-key`, `probe: health`); клиенты `voicebot`, `gamedev`, `agents` (`cloud_allowed: false`), `e2e` (`mock_only: true`); пул `cloud-27b` (mock); `weights.*` (окно 2: `sources` и `sets` заполняет `render.py`) |
| `deploy/render.py` | `parse_sha256_file`, `render_nginx` (остаток `__X__` → ошибка), `fill_weight_sets`, CLI |
| `deploy/systemd/llmhub-backup.{service,timer}` | §2.13 |
| `deploy/logrotate-llmhub-acct` | §2.7 |
| `deploy/server/llmhub-deploy.sh` | `llmhub-deploy.sh <step> check\|apply\|rollback`; переменные `LLMHUB_ROOT` (по умолчанию `/`), `LLMHUB_UFW` (по умолчанию `ufw`), `LLMHUB_SYSTEMCTL` — для теста F5; маркеры `/var/lib/llmhub-deploy/` |
| `scripts/e2e_mock.py`, `scripts/weights_probe.py` | как в v1; проба запускается `docker exec -i llmhub-weights-auth python /…/weights_probe.py` (секрет весов уже смонтирован там; токен не выводится) — скрипт кладётся в образ как модуль `llmhub.tools.weights_probe` |
| `docs/runbook.md` | §8 |
| тесты | `test_t371_integrity_busy.py`, `test_t371_secret_files.py`, `test_t371_config.py`, `test_t371_limits.py`, `test_t371_local_key.py`, `test_t371_probes.py`, `test_t371_reaper_watchdog.py`, `test_t371_mem_threshold.py`, `test_t371_cli.py`, `test_t371_backup.py`, `test_t371_render.py`, `test_t371_docker_files.py`, `test_t371_deploy_script.py` (`docker`, F5), `test_t371_e2e_script.py` (`slow`), `test_t371_weights_probe.py`, `test_t371_docs.py` |

## 4. Шаги кода (TDD; каждый шаг: тест → код → `uv run pytest <файлы>`, `uv run ruff check src tests scripts deploy`, `uv run pyright`)
**A — integrity и бэкап (sonnet)**
- A1. Busy/corrupt/прочее (как в v1): `uv run pytest tests/test_t371_integrity_busy.py tests/test_a_integrity.py tests/test_review_r2_migrate.py`.
- A2. `backup_state_db`: копия при открытом писателе (WAL) совпадает по `integrity_check` и данным; ротация `keep`; повторный запуск в ту же секунду не затирает; права 0640. `tests/test_t371_backup.py`.

**C — конфиг, секреты, лимиты, docs (sonnet)**
- C1. `test_t371_config.py`:
  - `api.host` wildcard → ошибка;
  - `mock_only` + `providers.enabled=[mock, x]` → ошибка;
  - `probe_interval_s < 10` → ошибка;
  - `api_key_env` и `api_key_file` вместе → ошибка;
  - `load_config` не открывает `api_key_file` (несуществующий путь допустим);
  - `min_mem_available_mib` по умолчанию 8192.
- C2. `test_t371_secret_files.py`:
  - `NAME_FILE` читается (0640, первая строка);
  - симлинк, пустой или короткий файл → `ConfigError` без содержимого;
  - заданы и `NAME`, и `NAME_FILE` → ошибка;
  - `load_instance_secrets` отвергает одинаковые секреты и в файловом варианте.
- C3. `429 rate_limited` (как в v1, C2).
- C4. docs-флаг; регенерация `docs/openapi.yaml`; `tests/test_o_openapi.py tests/test_api_errors.py tests/test_review_t369_sec_api.py`.

**D — локальные бэкенды (sonnet; после C1)**
- D1. `test_t371_local_key.py`:
  - `build_service` не читает файл при старте;
  - первый chat читает файл и отправляет `Bearer`;
  - фейк отвечает 401, файл обновлён → повторное чтение, второй запрос проходит;
  - нет файла → бэкенд `local_unavailable`, в журнале нет содержимого;
  - reaper (`python -m llmhub.reaper --once`) с конфигом, где `api_key_file` не существует, → код 0.
- D2. `test_t371_probes.py` (F8): FakeClock, 10 интервалов → у фейкового llama только `GET /health` (или `/v1/models` при `probe: models`), их число ≤ 11, других методов и путей нет; при работающей задаче лишних проб нет.

**R — reaper и валидатор (sonnet; после C1)**
- R1. `test_t371_reaper_watchdog.py`:
  - heartbeat обновляется каждый проход;
  - зависший `run_once` (фейковый адаптер ждёт вечно) → `_exit(70)` через `3 × loop` (через подменяемый `exit_fn`);
  - `healthcheck`: свежий файл → 0, старый/нет → 1.
- R2. `test_t371_mem_threshold.py`: фейковый `/proc/meminfo` 4 ГиБ → 503 `low_memory`, метрика; 31 ГиБ → обычная проверка токена; файл нечитаем → проверка не мешает (лог-предупреждение).

**B — CLI (sonnet; после A1)**
- B1. `read_key_file` (симлинк, 0644 → отказ; `/dev/stdin` → ок); `keys new --out` (0600, ключа нет в stdout/stderr, существующий файл → 2).
- B2. Команды против `LiveStack` через `--url`: `status`, `budget`, `client list`, `hold/release`, `clear-weights-hold` (`removed: true`, потом `false`), `kill` → `resume`, `kill --file`, `reload-clients`; неверный ключ → 1 без утечки.
- B3. `config-check` (секреты по `*_FILE`, `mock_only`), `serve` при `StateDbBusy` → 75, `--no-docs`, `backup`, `healthcheck`.

**E — Docker и деплой-артефакты (sonnet; после C1, R1)**
- E1. `test_t371_render.py` (как в v1) + `__MAX_DL__`, `directio 4m`, `output_buffers` в рендере; `tests/test_w_nginx_conf.py` (+ `-m docker` с образом nginx по digest — `nginx -t` на отрендеренном конфиге).
- E2. `test_t371_docker_files.py` (статика):
  - каждый `FROM` содержит `@sha256:`; есть `USER 64100`; нет `ENV` с `SECRET|KEY|TOKEN`; нет `COPY` из `secrets/`, `.env`, `*.key`;
  - `pip install` — `--no-index --require-hashes`.
  - в каждом `run-args/*.args`:
    - есть `--network host`, `--read-only`, `--cap-drop ALL`, `no-new-privileges`, `--user` не 0, `--restart unless-stopped`, `--log-driver journald`, `--health-cmd`, `--name llmhub-*`;
    - нет `-p`/`--publish`, `--privileged`, `-e *SECRET=`/`-e *KEY=`;
    - `/opt/models` смонтирован только `:ro`, `/etc/llama/api-key` — только у api, `:ro`, с `--group-add 998`;
    - матрица монтирования секретов: reaper без `instance`/`weights`, валидатор без `instance`/`label`.
  - `config.example.yaml` проходит `load_config`, `providers.enabled == ["mock"]`, `api.host == "10.8.0.1"`, `docs is False`, у `agents` `cloud_allowed is False`.
- E3. Скрипт `llmhub-deploy.sh`:
  - `bash -n`, `shellcheck` (если установлен);
  - запрещено: `pkill -f`, `set -x`, `echo/printf` с переменной секрета в stdout, `cat .*(secrets|\.key)` без перенаправления, `systemctl (restart|stop|disable|mask|daemon-reexec) (docker|llama)`, `docker (stop|rm|kill|restart|run|exec)` с именем не `llmhub-*`, `docker system|prune|network rm|volume rm`, `ufw (reset|disable|default)`, `/etc/docker/daemon.json`;
  - у каждого шага есть `check_/apply_/rollback_`.
- E4. **F5** — `test_t371_deploy_script.py` (`@pytest.mark.docker`, вручную перед окном 1). Одноразовый «сервер»: контейнер `docker:29-dind@sha256:…` (privileged) + `ubuntu:22.04@sha256:…` с docker CLI (`DOCKER_HOST` на dind), `ufw` установлен, `LLMHUB_SYSTEMCTL=true`-заглушка, фейковые `/etc/llama/api-key`, `/opt/models` с маленьким `.gguf` и sha256-файлом, фейковый «llama» (заглушка `/health`) на 127.0.0.1, `api.host: 127.0.0.1`. Сценарий для каждого шага S2–S9:
  - `apply` → `check` = 0 → повторный `apply` без изменений (хеши файлов, `docker inspect Id/StartedAt` те же) → `rollback` → `check` = не-0, следов нет (пользователи, файлы, контейнеры, правила ufw);
  - в конце — полный прогон и S14;
  - отдельно: у `docker inspect` контейнеров в Env нет значений секретов (сверка со сгенерированными файлами внутри dind);
  - «чужой» контейнер `searxng-fake` после всех шагов имеет те же `Id/StartedAt`.
  Команда: `uv run pytest -m docker tests/test_t371_deploy_script.py` (~5–10 мин).

**F — e2e и проба (sonnet; независим)**
- F1. `scripts/e2e_mock.py` (§7) против `LiveStack`, `slow`.
- F2. `weights_probe` (как в v1) + проверка MemAvailable перед запросом.

**G — документация (sonnet; после B, C, E, F)**
- G1. `docs/runbook.md`; G2. `docs/integration.md`; G3. `deploy/README.md`; `test_t371_docs.py` (+ в integration.md есть абзац о прокси: `proxy_read_timeout`, `proxy_buffering off`; в runbook — `docker logs` не используется, а `journalctl CONTAINER_NAME=…`; есть пункт об удалении `e2e`).

**I — интеграция (последним)**
- I1. ruff, pyright, все `tests/test_t371_*.py` + затронутые старые.
- I2. Полный прогон (релиз): `uv run pytest -q -p no:cacheprovider`, затем `-m docker` (E1, E4).
- I3. `deploy/docker/build.sh` → `dist/llmhub-<ver>.tar.gz`, `image.id`, `SHA256SUMS`; версия в `src/llmhub/__init__.py`.
- I4. Строки для T-372 в BOARD «Входящие» (`IN`-пачки, `host_blacklist_s ≥ 3600`) уже добавлены analyst 2026-11-04; реализовывать их в T-371 не нужно.

## 5. Параллельные потоки
| Поток | Файлы (владелец) | Зависит | Модель |
|---|---|---|---|
| A | `persistence/integrity.py`, `persistence/__init__.py`, `persistence/backup.py`, тесты A | — | sonnet |
| C | `configuration/loader.py`, `api_models.py`, `models.py`, `weights_models.py`, `api_gateway/errors.py`, `submission.py`, `app.py`, `docs/openapi.yaml`, тесты C | — | sonnet |
| D | `local_inference/client.py`, `server.py`, тесты D | C1 | sonnet |
| R | `reaper/__main__.py`, `reaper/core.py`, `weights/auth_app.py`, тесты R | C1 | sonnet |
| B | `__main__.py`, `admin_cli.py`, `test_t371_cli.py` | A1, A2 | sonnet |
| F | `scripts/e2e_mock.py`, `src/llmhub/tools/weights_probe.py` (+`tools/__init__.py`), тесты F | — | sonnet |
| E | `deploy/**` (кроме README), `tests/test_w_nginx_conf.py`, `test_t371_render.py`, `test_t371_docker_files.py`, `test_t371_deploy_script.py` | C1, R1, B3 (для команд в args) | sonnet |
| G | `docs/runbook.md`, `docs/integration.md`, `deploy/README.md`, `test_t371_docs.py` | B, C, E, F | sonnet |
| I | `src/llmhub/__init__.py`, `BOARD.md`, `STATUS.md`, сборка | все | — |
Параллельно стартуют A, C, F; затем D, R (после C1) и B (после A); E — после C1, R1, B3; G — последним.

## 6. Шаги сервера (координатор; `ssh -i ~/.ssh/id_ed25519 root@65.109.95.35`)
Правила:
- скрипт `/root/llmhub-dist/<ver>/llmhub-deploy.sh <step> check|apply|rollback`; любой неожиданный вывод — стоп и вопрос;
- `docker run/rm/stop/kill/exec` — только `llmhub-*`;
- dockerd, `daemon.json`, `docker.service.d`, llama-server, чужие контейнеры и правила ufw не трогаются.

**Окно 1 — API + reaper + e2e на Mock**
| Шаг | Apply | Check | Rollback |
|---|---|---|---|
| **S0 preflight** (только чтение) | снимок `/root/llmhub-preflight/<ts>/`: `systemctl show llama-server{,-27b,-small} -p ActiveState,SubState,ExecMainPID,NRestarts,ExecMainStartTimestamp`; `sha256sum` юнитов llama + drop-in + `/etc/llama/*`; `docker ps -a --no-trunc --format '{{.ID}} {{.Names}}'` + `docker inspect -f '{{.Name}} {{.Id}} {{.State.StartedAt}} {{.RestartCount}}'` **всех** контейнеров; `systemctl show docker -p ExecMainPID,ActiveEnterTimestamp`; `ss -ltnp`; `ufw status numbered`; `MemAvailable`; порты 8095/8096/9443 свободны; uid/gid 64100–64102 свободны | файлы снимка есть | — |
| **S1 артефакты** | `scp dist/llmhub-<ver>.tar.gz image.id SHA256SUMS deploy/…` → `/root/llmhub-dist/<ver>/` | `sha256sum -c SHA256SUMS` | `rm -rf /root/llmhub-dist/<ver>` |
| **S2 владельцы и каталоги** | `groupadd -g 64100 llmhub`, `useradd --system -u 64100 -g 64100 -M -s /usr/sbin/nologin llmhub`; то же 64101 `llmhub-wnginx`, 64102 `llmhub-wauth`; `install -d` по §2.3 + `/etc/llmhub` 0755, `/etc/llmhub/secrets` 0750, `/etc/llmhub/keys` 0700 | `getent`, `stat -c '%u:%g %a'` | `userdel`/`groupdel` (только при маркере S2 и без контейнеров `llmhub-*`), `rmdir` пустых |
| **S3 образ** | `docker load < llmhub-<ver>.tar.gz` | `docker image inspect -f '{{.Id}}' llmhub:<ver>` = `image.id`; `docker run --rm --network none --read-only --user 64100 sha256:<id> python -c 'import llmhub;print(llmhub.__version__)'` | `docker image rm sha256:<id>` (если нет контейнеров на нём) |
| **S4 секреты и ключи** | `label`, `instance`, `weights`: если файла нет — `umask 027; openssl rand -base64 48 \| tr -d '\n' > F; chgrp <g> F` (§2.4); `providers` — пустой 0600; admin и клиенты `voicebot`, `gamedev`, `agents`, `e2e`: `docker run --rm --network none --read-only --user 0 -v /etc/llmhub/keys:/out sha256:<id> python -m llmhub keys new --name N --out /out/N.key` (stdout — только sha256 → `/root/llmhub-dist/<ver>/hashes.txt`), admin → `/etc/llmhub/admin.key` | `stat` режимов и групп; секреты попарно различаются (`config-check`); `wc -c ≥ 32`; в выводе сессии только sha256 | `shred -u` созданных в этом запуске (список `S4.created`) |
| **S5 конфиг** | `render.py config` (окно 1 — без `weights.sources`) → `/etc/llmhub/config.yaml` 0640 `root:64100` (копия `.bak-<ts>` при наличии) | `docker run --rm --network host --read-only --user 64100 -v …ro sha256:<id> python -m llmhub config-check --config /etc/llmhub/config.yaml` = `ok` | восстановить `.bak` или удалить |
| **S6 контейнеры api + reaper + бэкап** | `docker run -d` по `run-args/llmhub-api.args`, `llmhub-reaper.args` (по Id образа); `install` `llmhub-backup.{service,timer}` → `systemctl daemon-reload && systemctl enable --now llmhub-backup.timer` | `docker inspect -f '{{.State.Health.Status}}'` = healthy ×2 за ≤ 60 с; `ss -ltnp` — `10.8.0.1:8095`, нет `0.0.0.0:8095`; `curl -s http://10.8.0.1:8095/healthz`; `docker exec -i llmhub-api python -m llmhub status --admin-key-file /dev/stdin < /etc/llmhub/admin.key`; `/metrics` с admin-ключом (через `-H @файл`, без вывода); `docker inspect llmhub-api -f '{{json .Config.Env}}'` — только `*_FILE`; `stat` `state.db*` 0640 64100; `journalctl CONTAINER_NAME=llmhub-api -n 50` без `CRITICAL`; `systemctl start llmhub-backup.service` → файл в `backup/` | `docker rm -f llmhub-api llmhub-reaper`; `systemctl disable --now llmhub-backup.timer`, `rm` юнитов, `daemon-reload`; данные остаются |
| **S7 ufw API** | `ufw allow in on tun0 from 10.8.0.0/24 to 10.8.0.1 port 8095 proto tcp comment 'llmhub api'` | с 10.8.0.4: `curl --max-time 5 http://10.8.0.1:8095/healthz` = ok; `ufw status numbered` = до + 1 | `ufw delete allow in on tun0 from 10.8.0.0/24 to 10.8.0.1 port 8095 proto tcp` |
| **S10 e2e на Mock** | §7 с рабочей станции; ключ `e2e`: `ssh … 'cat /etc/llmhub/keys/e2e.key' > ~/.config/llmhub/e2e.key` при `umask 077` (не на экран); перезапуск — `docker restart llmhub-api`; **в конце**: удалить клиента `e2e` из конфига → `reload-clients` → `shred -u` обоих экземпляров ключа (F7) | отчёт e2e — все пункты ok; после удаления ключ `e2e` → 401 | при провале: `kill` через CLI, разбор; возврат клиента `e2e` — только для повторного e2e |
| **S11 пост-проверка** | снова снимок S0 и `diff` | llama — те же `ExecMainPID`, `NRestarts`, `ExecMainStartTimestamp`, хеши; dockerd — тот же `ExecMainPID`; **все** чужие контейнеры — те же `Id/StartedAt/RestartCount`; `ufw` = до + 1 (окно 1) / + 2 (окно 2); `ss` = до + 8095 (+8096, 9443); с рабочей станции `curl http://10.8.0.1:8090/health` как до | расхождение по чужим — стоп, заказчику; llmhub — S14 |

**Окно 2 — сервер весов** (после окна 1; MemAvailable ≥ 8 ГиБ)
| Шаг | Apply | Check | Rollback |
|---|---|---|---|
| **S8a сертификат** | `docker run --rm --network none --user 0 -v /etc/llmhub/weights:/out sha256:<id> python /opt/llmhub/deploy/gen_weights_cert.py 65.109.95.35 /out`; ключ → `0600 64101`, cert 0644 | `openssl x509 -noout -fingerprint -sha256` = из вывода скрипта | `rm` обоих файлов |
| **S8b конфиг весов** | `render.py config --sums /opt/models/sha256-T365.txt` (добавить `weights.sources`/`sets`, `min_mem_available_mib`) → `docker restart llmhub-api` | `config-check` ok; api healthy | `.bak` + restart |
| **S8c валидатор и nginx** | `docker pull nginx@sha256:<digest>`; `render.py nginx` (`__PUBLIC_IP__=65.109.95.35 __PORT__=9443 __RATE__=80m __SEGMENTS__=1 __MAX_DL__=1 __MODELS__=/opt/models …`) → `/etc/llmhub/nginx-weights.conf` 0644; `docker run --rm … nginx -t`; `docker run -d` по `llmhub-weights-auth.args`, `llmhub-weights-nginx.args`; `install deploy/logrotate-llmhub-acct /etc/logrotate.d/llmhub-acct` | healthy ×2; `ss` — `65.109.95.35:9443`, `127.0.0.1:8096`; `curl -s 127.0.0.1:8096/metrics`; `logrotate -d /etc/logrotate.d/llmhub-acct`; `weights-acct.log` 0640 64101:64100; Env без секретов | `docker rm -f llmhub-weights-nginx llmhub-weights-auth`; `rm /etc/logrotate.d/llmhub-acct`; `docker image rm nginx@sha256:…` |
| **S9 ufw весов** | `ufw allow in on enp9s0 to 65.109.95.35 port 9443 proto tcp comment 'llmhub weights'` | снаружи: без токена → 401; `-tls1_2` → отказ; `/w/x/bench/run.py` → 403/404; правила v6 нет | `ufw delete allow in on enp9s0 to 65.109.95.35 port 9443 proto tcp` |
| **S10w проба** | `hold` Mock-пула → инстанс в BOOTING (Mock `boot_s` 60 с) → `docker exec llmhub-weights-auth python -m llmhub.tools.weights_probe --config … --op <op>` → `release` | 206, получен 1 МиБ, sha256 первых байтов совпадает с локальным чтением; `MemAvailable` до/после; негативы (чужой op 403, старый токен 403); **порт остаётся открытым** (решение заказчика) | при провале: S9 rollback до разбора |
| **S11** | повтор (окно 2) | как выше | — |

**После деплоя**
| Шаг | Apply | Check | Rollback |
|---|---|---|---|
| **S12 MCP `llmhub_ask`** (разрешено заказчиком 2026-11-04; выполняет координатор) | ключ `agents` (`cloud_allowed: false`) → `/home/user/.config/llmhub/agents.key` (0600, способ из S10); копия `.mcp.json` → `.bak-<ts>`; в `/var/www/projectVoiceBot/.mcp.json` добавить сервер `llmhub` по integration.md §8 (+`"timeout": 360000`), `voicebot-dev` не трогать; создать `/var/www/gameDev/.mcp.json` (файла нет) с `LLMHUB_MCP_ROOTS=/var/www/gameDev:/tmp/claude-1000`; без `LLMHUB_MCP_ALLOW_CLOUD`; ключи `voicebot`/`gamedev` — способом из Q6, затем `shred -u` на сервере | JSON валиден (`python -m json.tool`); `llmhub_models` из новой сессии отвечает; ключ нигде не выведен | вернуть `.bak` / удалить созданный gameDev `.mcp.json`; отозвать `agents` (убрать из `clients`, `reload-clients`) |
| **S13 GHCR** (опционально, **не выполнять** без отдельного разрешения) | `docker tag/push` `llmhub-infer` | — | удалить пакет |
| **S14 полный откат** | `docker rm -f` всех `llmhub-*`; `ufw delete` обоих правил; `systemctl disable --now llmhub-backup.timer`, `rm` юнитов и `/etc/logrotate.d/llmhub-acct`; `tar czf /root/llmhub-rollback-<ts>.tgz /etc/llmhub /var/lib/llmhub /var/lib/llmhub-weights-auth` (0600); `rm -rf` каталогов; `docker image rm` наших образов; `userdel`/`groupdel`; S11 | S11 = снимок S0 | tar + S2–S9 |

## 7. e2e на Mock (`scripts/e2e_mock.py`)
- `pre`:
  - `/healthz`; `/v1/models`;
  - облачный chat на Mock (`cloud_allowed`, `QUALITY`) и он же в stream (SSE до `[DONE]`);
  - локальный chat в 9b (`max_tokens=8`);
  - task (`Idempotency-Key`, опрос, `/result`);
  - lease: create → renew → close;
  - 429 при `max_active_jobs=4`;
  - 2 незавершённые задачи сохраняются в `S.json`.
- `post-restart` (после `docker restart llmhub-api`): задачи из `S.json` не потеряны и дошли до `completed`, ресурсов Mock-сирот нет.
- `kill`: hold → `kill` → 0 живых ресурсов за **≤ 60 с** → `resume`.
- Негатив: 401 без ключа, 403 у client-ключа на `/admin/*`, `/docs` → 404.
- Отчёт — JSON без ключей и текстов.

## 8. Runbook (`docs/runbook.md`)
- Состояние: `docker ps --filter name=llmhub-`, health, `journalctl CONTAINER_NAME=…`, CLI через `docker exec -i … --admin-key-file /dev/stdin`.
- Инциденты:
  - `weights_config` → `clear-weights-hold --pool`;
  - `db_corrupt`/safe mode; код 75 (`StateDbBusy`);
  - `kill`/`kill --file`/`kill_incomplete`; `tls_pin_mismatch`; `low_memory` у валидатора.
- Бэкап и восстановление (`docker stop llmhub-api llmhub-reaper` → копия → `docker start`).
- Ротация ключей (admin — `docker restart llmhub-api`; клиенты — `reload-clients`).
- Перевыпуск сертификата весов; закрытие порта 9443.
- Обновление релиза (`docker load` → новые контейнеры по новому Id; откат — старый Id из `/root/llmhub-dist/<prev>/image.id`); полный откат S14.
- Перед T-372 — сброс Mock-ledger (Q7) и проверка, что клиента `e2e` нет.

## 9. `docs/integration.md`
Рабочая версия, как в v1, с правками:
- адрес `10.8.0.1:8095`, только OpenVPN 10.8.0.0/24;
- `429 rate_limited` + `Retry-After`;
- OpenAPI — `docs/openapi.yaml` (на сервере `/docs` выключен);
- раздел C#/.NET (gameDev) и VoiceBot T-366;
- **абзац «прокси перед API»**: по умолчанию его нет; если появится — `proxy_read_timeout` > 480 с (600), `proxy_buffering off`, `proxy_request_buffering off`;
- §8 MCP — фактическая регистрация (S12), `agents` без облака.

## 10. Риски
| Риск | Мера |
|---|---|
| `-p` обходит ufw | `--network host` у всех, `-p` запрещён тестом E2 |
| host-сеть без изоляции | не-root, `--cap-drop ALL`, `no-new-privileges`, `--read-only`, bind только на нужные адреса (валидатор loopback по конфигу) |
| Docker не перезапускает unhealthy | самосторож reaper (`_exit(70)`), падение api/валидатора → выход процесса; health — для наблюдения |
| Секрет в `docker inspect` | только `*_FILE`; тест E4 сверяет Env с содержимым секретов |
| Права bind-монтирований (uid внутри = uid хоста) | фиксированные 64100–64102, gid llama 998; тест F5 на dind |
| Канал VPN при скачивании весов | 80m по решению заказчика, `__MAX_DL__=1`; Mock-поды веса не качают — до T-372 реального трафика нет |
| Вытеснение page cache моделей llama | `directio 4m`, порог MemAvailable → 503 → HF |
| Внешняя занятость llama не видна (только `/health`) | Q3; ошибки 503/таймаут llama → штраф бэкенда (есть в T-369) |
| Перезапуск dockerd рестартует **все** контейнеры (VPN!) | dockerd не трогаем; запрещено тестом E3; S11 сверяет PID dockerd |
| `docker load` 200+ МБ, CPU | окно согласует заказчик (Q1) |
| Mock-ledger при T-372 | Q7, runbook |

## 11. Вопросы
Для implementer блокирующих нет.
- **Q1** (блокирует только сервер): время окон 1 и 2.
- **Q3**: устраивает ли проба только по `/health` (внешняя занятость llama не видна) или разрешить `GET /slots` (тоже GET, не нагружает).
- **Q4**: 8095 для AWG 10.42.42.0/24 — по умолчанию нет.
- **Q5**: скрейп `/metrics` существующим Prometheus — по умолчанию нет.
- **Q6**: передача ключей `voicebot`/`gamedev`.
- **Q7**: сброс Mock-ledger перед T-372.
- **Q8**: тариф трафика Hetzner (проверка только в Robot).
- **Q10**: GHCR — отдельное разрешение.
- Закрыты решениями заказчика: Docker; лимит 80m (Q2); регистрация MCP (Q9).

## 12. Приёмка
| Критерий | Где |
|---|---|
| Контейнеры api/reaper (вместо `llmhub.service`/`reaper.timer` — решение заказчика), `config.example.yaml`, секреты 0600/0640, `providers` пустой; не root; llama не изменены | E2, S2–S6, S11 |
| `/healthz`, `/metrics` по VPN; CLI | B, S6–S7 |
| Порт весов: HTTPS, только GGUF, `limit_rate 80m`, ≤ 2 загрузок; VPN и llama как прежде; тариф Hetzner | E1, S8–S10w, S11, Q8 |
| e2e на Mock: chat, stream, task, lease, kill ≤ 60 с, перезапуск без потерь | F1, S10 |
| `docs/integration.md` рабочий | G2 |
| Бэкапы и откат описаны; реальных ключей нет | A2, `llmhub-backup.timer`, G1, S14 |
| Входящие: integrity busy · R3-2 `clear-weights-hold` + runbook · SEC-L2 · SEC-L4 (лимит 429) · сервер весов по README · composition root (уже в `build_service`) · nginx API (абзац) | A1 · B2, G1 · C4 · C3 · S8 · S6/S10 · G2 |
| Перенесено в T-372: `IN`-пачки, `host_blacklist_s ≥ 3600` | I4 (BOARD «Входящие») |
| Ворота: critic F1, F4–F8, F10 (n/a), F11; advocate F6/OPEN-3, F7 | §2.9, 2.6, E4, 2.8/R2, 2.11, 2.10/D2, 2.5, 2.13/A2; §1 |

## 13. Сопоставление находок ворот и решений координатора (дополнено implementer 2026-11-04)
| Находка / решение | Где закрыто |
|---|---|
| critic F1 (ленивый ключ llama) | `LocalBackendConfig.api_key_file`, `local_inference/client.py` (`file_key_source`, повтор после 401 не чаще 30 с), `test_t371_local_key.py` |
| critic F4/F7, advocate F7 (сторож reaper, healthcheck) | `reaper/core.py` (`Watchdog`, `stall_seconds`, heartbeat), `reaper healthcheck`, `test_t371_reaper_watchdog.py` |
| critic F5 (скрипт в одноразовом окружении) | `tests/test_t371_deploy_script.py` (ubuntu 22.04 + свой dockerd из образа dind) |
| critic F6, advocate F6/OPEN-3 (page cache, порог памяти) | `directio 4m`/`output_buffers` в `nginx-weights.conf`, `weights.min_mem_available_mib`, 503 `low_memory`, `test_t371_mem_threshold.py` |
| critic F7 (`mock_only`, удаление `e2e`) | `ClientConfig.mock_only` + валидатор `Config`, шаг `s10x` (в конце T-371, после S10w) |
| critic F8 + **Q3 координатора**: GET `/slots` разрешён | `probe: slots\|health\|models`, только GET, интервал ≥ 10 с; `test_t371_probes.py` (в фоне нет POST и completion) |
| **F9**: проверка S9 — путь под regex location | `s9 check`: `/w/000000000000/qwen/x.gguf` без токена → 401 (не 404); путь вне regex → 404/403; TLS 1.2 → отказ |
| critic F10 (pip без хешей) | n/a: wheelhouse с `--require-hashes` из `uv.lock` |
| critic F11 (бэкап) | `backup_state_db`, `llmhub backup`, `llmhub-backup.{service,timer}`, `test_t371_backup.py` |
| **F12**: logrotate при остановленном nginx, владелец | `deploy/logrotate-llmhub-acct`: `postrotate` шлёт USR1 только при `docker inspect -f '{{.State.Running}}' llmhub-weights-nginx` = `true`, `\|\| true`, `exit 0`. Новые файлы `create 0640 llmhub-wnginx llmhub` (владелец — пользователь nginx). Каталог `/var/log/llmhub` = `root:llmhub 0750` (nginx `--group-add 64100`), поэтому `su` не нужен. Со `su` скрипт `postrotate` выполнялся бы от этого пользователя без доступа к docker, nginx не переоткрыл бы файл и учёт объёма был бы занижен |
| **F13**: пользователи и каталоги при S14/переустановке | `s14 apply`: tar (0600), затем **удаление** `/etc/llmhub`, `/var/lib/llmhub*`, `/var/log/llmhub`, образов и пользователей. Переустановка `s2` создаёт **те же фиксированные uid/gid 64100–64102**, поэтому архив восстанавливается с верными владельцами (runbook §7) |

## 14. Выполнение кода (implementer, 2026-11-04)
- [x] A1 integrity busy (`StateDbBusy`, ожидание 30 с, прочие OperationalError не карантинят)
- [x] A2 `backup_state_db`
- [x] C1 конфиг (host без wildcard, `mock_only`, `probe_interval_s ≥ 10`, `api_key_file`, `min_mem_available_mib`, `docs`)
- [x] C2 секреты `*_FILE`
- [x] C3 `429 rate_limited` (`max_active_jobs`)
- [x] C4 docs-флаг, регенерация `docs/openapi.yaml`
- [x] D1 ленивый ключ llama; D2 пробы GET
- [x] R1 сторож reaper; R2 порог памяти валидатора
- [x] B1–B3 CLI (`admin_cli.py`, `keys new --out`, `config-check`, `backup`, `healthcheck`, `serve --no-docs`, код 75)
- [x] F1 `scripts/e2e_mock.py`; F2 `llmhub.tools.weights_probe`
- [x] E1 рендер и nginx; E2 статика Docker; E3 скрипт; E4 F5 на одноразовом сервере
- [x] G1 runbook; G2 integration.md; G3 deploy/README.md
- [x] I1–I3 проверки, сборка `dist/0.2.0`
- [ ] Шаги сервера S0–S14 — координатор

## 15. Исправления после ревью, раунд 1 (implementer, 2026-11-04)
- [x] SEC-D2 конфиг каталогом `/etc/llmhub/conf` (rename виден контейнерам), перезапуск api/reaper/валидатора при изменении конфига, `s10x` требует 401 от работающего API и проверяет `client list`
- [x] SEC-D3 `limit_conn_zone $server_port` (тест на закреплённом nginx: вторая загрузка → 503)
- [x] SEC-D1 `grep -qF -f` (секрет не в argv)
- [x] SEC-D4 F5 с `--network none`, проверка отрендеренных конфигов на 10.8.0.1/65.109.95.35, socket-guard в `tests/conftest.py`
- [x] SEC-D5 образы по своим тегам `llmhub:<ver>`/`llmhub-nginx:<ver>`; откат снимает только теги
- [x] L1–L7, I1, I3
- [x] REV2-1…REV2-7
- [x] xfail в `tests/test_review_t371_*.py` сняты; сборка `dist/0.2.1`
