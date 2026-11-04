# План T-370: веса (WeightSource, сервер весов) и образ llmhub-infer

> TL;DR: Под загружает веса сам при старте, через entrypoint на Python: цепочка `self_hosted` → `hf`, sha256, докачка Range, переход к следующему источнику при ошибке и при скорости ниже `min_speed_mb_s` после 30 с. Прогресс отдаётся через `/health` пода; контроллер читает его в LOADING_MODEL. Сервер весов: отдельный nginx (TLS self-signed + pin, токен в заголовке, `auth_request` к валидатору llmhub), токен HMAC привязан к `op_id` и действует только до READY. Образ собирается `FROM ghcr.io/ggml-org/llama.cpp:server-cuda12-b12063@digest` (CUDA 12.8.1), без весов и секретов. Облачный `HttpInferenceBackend` (pin/ключ, готовность, ошибки, тайминги MTP) — отдельный поток G; замороженный протокол `InferenceBackend` не меняется. 8 потоков: 0 (подготовка) → A, B, C, D параллельно → E, G → F (интеграция). Ворота ADR-0139 пройдены 2026-11-04: раздел 2a главнее разделов ниже.

Задача: `tasks/active/T-370-weights.md` · ADR-0138 п. 1, 4, 12, 13 · исследование T-367 §6.12 и «Ворота 2026-11-04» п. 2, 6.

## 1. Объём и не-цели
**В объёме**
1. Загрузчик в поде (`infer-image/llmhub_infer/`): цепочка источников, sha256, Range, переход при ошибке и низкой скорости, файл прогресса.
2. Фронт пода: `/ping`, `/health` с прогрессом, проверка ключа API, обратный прокси к llama-server на `127.0.0.1`. TLS в режиме `direct`; в режиме `proxy` — HTTP за прокси RunPod.
3. Супервизор пода: разбор env → загрузка → запуск llama-server с флагами MTP → контроль процесса; секреты в дочерний процесс не передаются.
4. `infer-image/Dockerfile` + `requirements.txt` (с хешами), статические тесты Dockerfile, hadolint и `docker build` — только вручную (раздел 9).
5. Контроллер:
   - конфиг `weights` и `infer` пула;
   - выдача токенов (`weights/tokens.py`);
   - валидатор `auth_request` (`weights/auth_app.py`);
   - сборка env инстанса с секретами (`cloud_manager/instance_env.py`);
   - чтение прогресса и ранний E7 при `failed`.
6. **Облачный `InferenceBackend`** (решение координатора по Q6 из T-369): `HttpInferenceBackend` — httpx-клиент контроллер → `llmhub-infer`:
   - режимы TLS `direct` (pin) и `proxy` (только ключ), ключ на инстанс;
   - готовность = `/health` 200 + тест-запрос, таймауты;
   - отображение ошибок в `NetworkLost` / `InstanceLost`;
   - разбор таймингов llama-server (включая MTP).

   Отдельный поток G, тесты офлайн против фейкового `llmhub-infer`. Протокол `InferenceBackend` в `cloud_manager/inference.py` **заморожен и не меняется**.
7. Сервер весов как файлы: `deploy/nginx-weights.conf` (шаблон) и `deploy/gen-weights-cert.sh`; `nginx -t` в локальном docker.

**Не цели**
- Открытие порта, установка nginx и systemd-юнитов на 65.109.95.35, публикация образа (T-371/T-372).
- R2 (только `kind: r2` зарезервирован в схеме и отвергается валидатором как «not implemented»).
- Самоуничтожение пода по `max_lifetime` (нет ключа «только на этот под»).
- Запекание весов в образ.
- Реальный адаптер RunPod (T-372).
- Параллельная загрузка одного файла несколькими соединениями.

## 2. Ключевые решения (на ворота critic + advocate)
| # | Решение | Почему | Отвергнуто |
|---|---|---|---|
| R1 | Загрузка весов **внутри пода** при старте (entrypoint), контроллер только выдаёт план и токен | контроллер не грузит модели (ADR п. 1); трафик идёт напрямую сервер → под | прокачка через контроллер |
| R2 | Прогресс — **pull**: контроллер читает `GET /health` пода (JSON) в LOADING_MODEL | API контроллера доступен только через VPN (10.8.0.1), под до него не достучится | push на контроллер, отдельный публичный callback |
| R3 | Сервер весов — **отдельный экземпляр nginx** (свой `nginx.conf`, pid, юнит в T-371), `auth_request` к валидатору `127.0.0.1:8096` | в nginx есть `limit_conn`, `limit_rate`, `auth_request`, sendfile и Range; отдельный экземпляр не трогает существующие службы | `secure_link` (только MD5 и токен в query → попадает в логи, нет отзыва); Caddy (`forward_auth` встроен с v2.5, но нет штатных `limit_rate`/`limit_conn` — нужны плагины; лишний бинарник) |
| R4 | TLS сервера весов — **self-signed EC P-256 с IP SAN, CA:FALSE, 10 лет**; сертификат (публичный PEM) передаётся в под как единственный доверенный якорь (`cafile`), `check_hostname` по IP | проверено 2026-11-04 (OpenSSL 3.0.2, Python ssl): такой сертификат принимается как единственный якорь, TLS 1.3. Нет внешних CA, нет продления раз в 6 дней, нет порта 80 | IP-сертификат Let's Encrypt (короткоживущий профиль ~6 дней, нужен ACME-клиент и порт 80/ALPN, лишняя точка отказа; pin всё равно сильнее) |
| R5 | Токен — **в заголовке** `Authorization: Bearer`, путь `/w/<op_id>/<rel>` | заголовки nginx не пишет в лог; `op_id` в пути не секрет и даёт привязку и метку в логе | токен в query (`secure_link`) |
| R6 | Токен детерминирован: `HMAC(weights_secret, "llmhub-weights|v1|"+op_id+"|"+exp)` (без nonce; формат `v1.<op>.<exp>.<sig>`), один на инстанс, многоразовый для Range и повторов (уточнение формулировки ADR «одноразовый», раздел 2a п. 2), + лимит объёма и привязка к IP; срок `exp = instances.created_at + provisioning_s + booting_s + loading_model_s`. **Отзыв = проверка состояния**: валиден, только пока инстанс `op_id` в PROVISIONING/BOOTING/LOADING_MODEL | повторный create с тем же label выдаёт тот же токен (поздний под не ломается); READY, ERROR, STOPPING и таймаут E7 отзывают его без отдельной таблицы и хуков в автомат | таблица токенов + хуки на переходы (больше кода в ядре, гонки) |
| R7 | Ключ API и TLS пода (`direct`) **выводятся из секрета**: `api_key = HMAC(instance_secret, "api:"+op_id)`; Ed25519-ключ из `HMAC(instance_secret, "tls:"+op_id)`, сертификат детерминирован (Ed25519 подписывает детерминированно; срок и серийный номер — из `created_at` и `op_id`). Отпечаток — в `instances.tls_fingerprint` | после перезапуска контроллера ничего не нужно хранить; в БД нет приватных ключей; повторный create даёт тот же материал | хранить ключи в БД; генерировать в поде (нужен TOFU) |
| R8 | Два секрета: `LLMHUB_INSTANCE_SECRET` (ключи API и TLS, только контроллер) и `LLMHUB_WEIGHTS_SECRET` (токены, контроллер и валидатор) | взлом валидатора не раскрывает ключи API | один общий секрет |
| R9 | Код пода — отдельный пакет `llmhub_infer` (stdlib + starlette, uvicorn, httpx — те же версии, что в `uv.lock`), **без импортов `llmhub`**; контракт env проверяется тестом (контроллер собирает → под разбирает) | образ маленький и независимый; тесты идут в том же venv | общий пакет (тянет FastAPI и SQLite в образ) |
| R10 | Образ: `FROM ghcr.io/ggml-org/llama.cpp:server-cuda12-b12063@sha256:d5ffba7d096ab0236c65dc147d797b4b7658a7e5425634593373635d661bccda` + apt `python3 python3-venv` (Ubuntu 24.04 → 3.12) | проверено 2026-11-04: это последний официальный образ CUDA, `CUDA_VERSION=12.8.1` (драйвер 570 подходит), 2,48 ГБ сжатый, бинарник `/app/llama-server`. Образа `b12084` нет (замер шёл на бинарниках релиза); `draft-mtp` есть с b11619 (T-365) | сборка из исходников со своим CUDA 12.8 (долго, нужен devel-образ) — запасной вариант, если пиннутый образ не пройдёт проверку шага C5 |
| R11 | Публикация: **GHCR public** `ghcr.io/<owner>/llmhub-infer:<ver>`, в конфиге пула — по digest | в образе нет секретов и адресов (IP сервера весов и сертификат приходят через env); у RunPod не нужны учётные данные реестра — на один секрет и одну причину сбоя pull меньше. Push — только с разрешения заказчика (T-371/T-372) | GHCR private + registry auth в RunPod (лишний PAT, сбои pull); Docker Hub (лимиты pull) |

## 2a. Ворота 2026-11-04 (critic + advocate, ADR-0139) — главнее разделов ниже
Решения координатора. Шаги ниже обновлены там, где это не требовало переписывания. При расхождении действует этот раздел.

1. **Скорость и параллельность (заменяет Q1).** `limit_rate 80m` на соединение (640 Мбит/с из 1 Гбит/с — запас для VPN). `limit_conn w_all` = `segments` источника `self_hosted` (по умолчанию 1): с нашего сервера качает один под за раз, второй одновременный получает 503 и уходит на HF. `tc` в T-370 не делается; опционально — вопрос в T-371.
2. **Токен — уточнение формулировки ADR.** ADR говорит «одноразовый». Принято: **один на инстанс, многоразовый для Range и повторов, отозван при READY или таймауте** (проверка состояния, R6). Записать в журнал задачи и в T-371 как явное уточнение ADR-0138 п. 4; новый ADR не нужен, это толкование.
   - Подпись: `HMAC-SHA256(weights_secret, "llmhub-weights|v1|"+op_id+"|"+str(exp))`, формат `v1.<op>.<exp>.<b64url sig>`, без nonce.
   - **Лимит объёма на op.** Валидатор отказывает (403, `volume_cap`), когда `served(op) + requested > volume_cap_factor (1.2) × Σsize` набора.
     - `served(op)` — сумма `$body_bytes_sent` из отдельного лога nginx `weights-acct.log` (`op bytes status`). Валидатор читает его инкрементально: смещение и inode; при ротации — с начала нового файла; при старте — полный проход.
     - `requested` — из `X-Range`: `bytes=a-b` → `b−a+1`; `bytes=a-` → `size−a`; без Range → `size`. Размер берётся из набора по `rel`.
     - Обрыв на 50 % и докачка остатка дают 100 % — лимит не мешает честной докачке.
   - **Привязка к IP.** Первый `X-Real-IP` для op запоминается; запрос с другого IP → 403 `ip_mismatch`.
   - Состояние валидатора (IP и смещение лога) — маленький SQLite в `auth_state_dir`, своя БД, не БД контроллера.
3. **Параллельные сегменты Range — в T-370.** `WeightSourceConfig.segments`: по умолчанию 1 для `self_hosted` (до замера), 4 для `hf`. `limit_conn w_op` = `segments`.
   - `segments == 1`: потоковая загрузка с инкрементальным sha256 (как в 5.1).
   - `segments > 1`: файл предразмечается (`truncate(size)`), очередь кусков по 128 МиБ с ограниченным Range `bytes=a-b`. N рабочих потоков, у каждого своё соединение, запись через `os.pwrite`. Сделанные куски — в sidecar `<id>.gguf.part.json` (атомарная запись). Докачка и переход на другой источник берут оставшиеся куски. sha256 считается отдельным проходом после сборки (≈ 15–25 с на 16,5 ГБ — учтено в ETA).
   - HF: цель редиректа (CDN) кэшируется на файл; на 403/410 от CDN — новый запрос к `resolve`.
   - Скорость для `SpeedMonitor` — суммарная по всем сегментам.
   - Тесты (B4.16–B4.19):
     - сборка из 4 сегментов совпадает побайтно, sha верный;
     - обрыв одного сегмента → докачан только этот кусок;
     - переход на другой источник посреди сегментов → итог верный;
     - перезапуск процесса с sidecar → качаются только недостающие куски.
4. **HF_TOKEN по умолчанию не передаётся** (репозиторий публичный). Если задан `weights.hf_token_env` — только fine-grained токен с правом read на этот репозиторий; это записывается в `infer-image/README.md` и в пример конфига. Тест E2: по умолчанию `HF_TOKEN` нет в `secret_env`.
5. **Права валидатора и WAL (для T-371).**
   - Пользователи: юнит `llmhub-weights-auth` работает от `llmhub-wauth`, дополнительная группа `llmhub`. Контроллер — `llmhub:llmhub`, `UMask=0027`.
   - Права на файлы:

     | Путь | Права |
     |---|---|
     | каталог данных | 0750 `llmhub:llmhub` |
     | `llmhub.db`, `-wal`, `-shm` | 0640 `llmhub:llmhub` |
     | `/var/log/llmhub` | 0750 `www-data:llmhub` |
     | `weights-acct.log` | 0640 |
     | `auth_state_dir` | `StateDirectory=llmhub-weights-auth`, 0700 `llmhub-wauth` |
     | `/etc/llmhub/secrets.env` (`LLMHUB_WEIGHTS_SECRET`) | 0640 `root:llmhub-wauth` |

     `LLMHUB_INSTANCE_SECRET` валидатору не выдаётся: отдельный файл `/etc/llmhub/instance.env`, 0600 `llmhub`.
   - Валидатор открывает БД `file:…?mode=ro` (не `immutable`). `-wal` и `-shm` существуют, пока контроллер держит БД открытой. Без права записи на `-shm` SQLite ≥ 3.22 читает через heap-память — проверить версию SQLite в тесте.
   - Тест D2b: БД в WAL с открытым писателем; затем `chmod` каталога 0550 и `-shm`/`-wal` 0440 (под root — skip с причиной). Валидатор открывает БД ro и видит последнее закоммиченное состояние; запись через его соединение падает.
   - Счётчики отказов по verdict отдаёт валидатор на `GET /metrics` (Prometheus text, только 127.0.0.1); БД контроллера он не пишет, поэтому инцидентов не создаёт. Опрос `/metrics` и алерт — T-371.
6. **Находки критика (medium/low), все приняты:**
   - **Учётные данные `HttpInferenceBackend`:** сопоставление endpoint → ровно одна живая строка, дальше по `id`/`op_id`; после рукопожатия `sha256(DER)` сверяется с `instances.tls_fingerprint` (5.3). Тесты G4: две живые строки с одним endpoint → False; подменённый отпечаток в БД → False.
   - **ETA и медленная загрузка:**
     - `/health` пода отдаёт `eta_s` и `sources_left`;
     - контроллер (E5) делает ранний E7 `weights_too_slow`, если `sources_left == 0` и `now + eta_s + llama_load_s (60) > state_since + loading_model_s` на двух опросах подряд, не раньше 60 с в LOADING_MODEL;
     - тест E5b; тест «есть запасной источник → нет раннего E7».
   - **Общий дедлайн загрузки в поде** = `deadline_unix` из плана (= `exp` токена). По истечении — `failed`, reason `deadline`. Процесс жив, фронт отвечает 500 `failed`; без выхода и без цикла перезапусков контейнера. Тест C3b.
   - **Прочие правила загрузчика:**
     - `done > size` во время приёма → прервать и считать `size_mismatch`;
     - `size_mismatch` → следующий источник;
     - тесты B4.10 (меньше) и B4.10b (больше, прерывание до конца тела).
   - **Порядок проверок валидатора:** формат → HMAC → `exp` → op в пути → БД (состояние) → IP → объём. Тест D1b: шпион `state_of` не вызывается при плохом формате, подписи или сроке.
   - **Сертификаты** только через `cryptography` (`deploy/gen_weights_cert.py`, фейки) — без skip из-за `openssl`.
   - **Часы:** `SpeedMonitor`, `Fetcher` и `FakeWeightsServer` получают общий `FakeClock`. Троттлинг фейка двигает часы на `chunk / throttle_bps` при каждой отправке и не спит, поэтому тесты скорости (B4.7, B4.8) детерминированы. Реального времени нет нигде, кроме таймаутов сокетов.
   - **D5 с настоящим nginx обязателен при наличии docker** (иначе причина в журнал), с кейсами `%2e%2e`, `//`, `%2F`.
   - **`load_status` в режиме proxy:** не-JSON или JSON без нашего `status` (страница прокси RunPod) → `None`, не `failed`. Тест G4.
   - **Образ:** в Dockerfile пиннуется `python3.12=<версия>` и `python3.12-venv=<версия>` (версия из `apt-cache policy` при сборке C5); если пин ломает сборку — явное принятие с комментарием в Dockerfile и записью в журнале. hadolint запускается по digest: `hadolint/hadolint@sha256:<digest>`, digest записывается в журнал.
7. **Advocate:** R3 исправлен (Caddy `forward_auth` встроен с v2.5). Выбор nginx остаётся из-за `limit_rate` и `limit_conn`.

## 3. Контракт контроллер → под (env)
Обычные переменные (`InstanceSpec.env`):
| Переменная | Значение |
|---|---|
| `LLMHUB_OP_ID` | 12 hex |
| `LLMHUB_TRANSPORT` | `direct` или `proxy` |
| `LLMHUB_PORT` | `8443` (direct) / `8080` (proxy) |
| `LLMHUB_WEIGHTS_PLAN` | JSON, схема ниже |
| `LLMHUB_LLAMA_ARGS` | JSON-список доп. аргументов llama-server |
| `LLMHUB_MAX_LIFETIME_S` | информационно |

Секретные переменные (новое поле `InstanceSpec.secret_env`, `repr=False`, в логах и БД не появляется):
| Переменная | Когда |
|---|---|
| `LLMHUB_API_KEY` | всегда |
| `LLMHUB_WEIGHTS_TOKEN` | есть источник `self_hosted` |
| `LLMHUB_TLS_CERT_PEM`, `LLMHUB_TLS_KEY_PEM` | только `direct` |
| `HF_TOKEN` | **по умолчанию не передаётся** (репозиторий публичный); только если задан `weights.hf_token_env`, и только fine-grained read-only (раздел 2a п. 4) |

`LLMHUB_WEIGHTS_PLAN` (version 1):
```json
{"version": 1,
 "files": [{"id": "model", "size": 17680000000, "sha256": "<64hex>"},
           {"id": "draft", "size": 1470000000, "sha256": "<64hex>"}],
 "sources": [
   {"name": "self_hosted", "base_url": "https://65.109.95.35:9443/w/<op_id>/",
    "paths": {"model": "qwen3.8-27b/Qwen3.8-27B-UD-Q4_K_M.gguf", "draft": "qwen3.8-27b/mtp-Qwen3.8-27B-Q4_0.gguf"},
    "ca_pem": "-----BEGIN CERTIFICATE-----...", "auth": "weights_token", "min_speed_mb_s": 25, "segments": 1},
   {"name": "hf", "base_url": "https://huggingface.co/<repo>/resolve/<revision>/",
    "paths": {"model": "Qwen3.8-27B-UD-Q4_K_M.gguf", "draft": "MTP/mtp-Qwen3.8-27B-Q4_0.gguf"},
    "ca_pem": null, "auth": "none", "min_speed_mb_s": 0, "segments": 4}],
 "deadline_unix": 1762300000,
 "speed_grace_s": 30, "speed_window_s": 10, "read_timeout_s": 20, "connect_timeout_s": 10,
 "retries_per_source": 2}
```
Правила:
- Порядок файлов: `draft` (малый) → `model`.
- Имена на диске: `<weights_dir>/<id>.gguf`, по умолчанию `/models`.
- Супервизор сам добавляет `-m /models/model.gguf -md /models/draft.gguf --host 127.0.0.1 --port 8081`. В `LLMHUB_LLAMA_ARGS` запрещены `-m --model -md --model-draft --host --port --api-key --ssl-*` (проверка и в конфиге контроллера, и в поде).

Аргументы по умолчанию (замер 2026-11-04): `["-ngl","999","-fa","on","-c","65536","-np","1","--jinja","--spec-type","draft-mtp","--spec-draft-n-max","3"]`. ADR п. 13 упоминает q8 KV — см. вопрос Q3.

## 4. Конфигурация (поток A)
`configuration/models.py` (pydantic, `extra=forbid`):
```python
class WeightFileConfig(_Model):
    id: Literal["model", "draft"]; size: int = Field(gt=0); sha256: str  # ^[0-9a-f]{64}$

class WeightSourceConfig(_Model):
    name: str
    kind: Literal["self_hosted", "hf", "r2"]          # r2 -> validation error "not implemented"
    base_url: str                                       # self_hosted: must be https://, may contain "{op_id}"
    paths: dict[str, str]                               # file id -> relative path (no "..", no leading "/")
    ca_pem_file: Path | None = None                     # self_hosted: required (public cert, pin)
    min_speed_mb_s: float = Field(default=25, ge=0)
    segments: int | None = Field(default=None, ge=1, le=8)  # None -> 1 for self_hosted (until measured), 4 for hf

class WeightSetConfig(_Model):
    files: list[WeightFileConfig]                       # ids unique; "model" required

class WeightsConfig(_Model):
    sources: list[WeightSourceConfig] = []              # order = chain
    sets: dict[str, WeightSetConfig] = {}
    speed_grace_s: float = 30; speed_window_s: float = 10
    read_timeout_s: float = 20; connect_timeout_s: float = 10; retries_per_source: int = 2
    auth_listen: str = "127.0.0.1:8096"
    hf_token_env: str | None = None      # name of the var in providers.env; None -> HF_TOKEN never passed
    volume_cap_factor: float = 1.2       # validator: bytes served per op <= factor * sum(size)
    auth_state_dir: Path = Path("/var/lib/llmhub-weights-auth")
    acct_log: Path = Path("/var/log/llmhub/weights-acct.log")

class InferConfig(_Model):
    transport: Literal["direct", "proxy"] = "proxy"
    port: int | None = None                             # default 8443 direct / 8080 proxy
    llama_args: list[str] = [<defaults from section 3>]  # forbidden flags -> validation error
    weights_dir: str = "/models"
    client: InferClientConfig = InferClientConfig()   # controller -> pod timeouts (section 5.3, stream G)

# PoolConfig: + weights: str | None = None (key of weights.sets), infer: InferConfig = InferConfig()
# Config: + weights: WeightsConfig = WeightsConfig()
```
Проверки уровня `Config`:
- `pool.weights` есть в `sets`;
- у каждого источника есть путь для каждого файла набора;
- если пул ссылается на набор, цепочка не пуста;
- `self_hosted` требует `https` и `ca_pem_file`;
- `direct` требует `OfferQuery.need_public_ip=True` (подключить в поток E).

`configuration/loader.py`: `load_instance_secret(env)`, `load_weights_secret(env)` по образцу `load_label_secret` (≥ 32 байт, ошибка без значения). `ca_pem_file` читается при старте контроллера (`load_weights_ca(cfg) -> dict[str, str]`).

Пример: `examples/config.cloud.example.yaml` — пул `cloud-27b`, набор `qwen38-27b-q4`, суммы — заглушки `<from /opt/models/sha256-T365.txt>` (заполняются в T-371).

## 5. Интерфейсы
### 5.1 Под: `infer-image/llmhub_infer/` (потоки B, C)
```python
# plan.py (B) — parse/validate LLMHUB_WEIGHTS_PLAN
@dataclass(frozen=True) class FileSpec: id: str; size: int; sha256: str
@dataclass(frozen=True) class SourceSpec: name: str; base_url: str; paths: Mapping[str, str]; ca_pem: str | None; auth: str; min_speed_mb_s: float
@dataclass(frozen=True) class WeightsPlan: files: tuple[FileSpec, ...]; sources: tuple[SourceSpec, ...]; speed_grace_s: float; speed_window_s: float; read_timeout_s: float; connect_timeout_s: float; retries_per_source: int
def parse_plan(raw: str) -> WeightsPlan        # PlanError (message without secrets)

# speed.py (B)
class SpeedMonitor:
    def __init__(self, clock: Callable[[], float], grace_s: float, window_s: float, min_bps: float) -> None
    def add(self, nbytes: int) -> None
    def too_slow(self) -> bool        # False before grace; avg over last window < min_bps; min_bps==0 -> never
    def bps(self) -> float

# fetch.py (B)
class FetchError(Exception): ...      # .reason: "http_4xx"|"http_5xx"|"network"|"slow"|"sha_mismatch"|"size_mismatch"|"disk"|"exhausted"
@dataclass class Progress: phase: str; file: str | None; source: str | None; done: int; total: int; bps: float; failovers: list[dict[str, str]]; error: str | None
class Fetcher:
    def __init__(self, plan: WeightsPlan, dest: Path, secrets: Mapping[str, str],
                 on_progress: Callable[[Progress], None], clock: Callable[[], float] = time.monotonic,
                 transport_factory: Callable[[SourceSpec], httpx.BaseTransport] | None = None) -> None
    def run(self) -> dict[str, Path]  # id -> verified file; raises FetchError("exhausted")
# progress.py (B): write_progress(path, Progress) — atomic (tmp + os.replace), 0600; read_progress(path) -> dict | None

# front.py (C): Starlette app
def build_app(state: PodState, api_key: str, upstream: str = "http://127.0.0.1:8081") -> Starlette
#   GET /ping   -> 204, no auth (BOOTING -> LOADING_MODEL)
#   GET /health -> 401 without key; 200 {"status":"ok"} if llama /health 200;
#                  503 {"status":"loading", "phase", "file", "source", "done", "total", "mb_s", "failovers"};
#                  500 {"status":"failed", "reason"} (weights exhausted / llama exited twice)
#   /v1/*, /props -> Bearer check (hmac.compare_digest) -> stream proxy (SSE), Authorization stripped,
#                  hop-by-hop stripped, body <= 8 MiB; 503 until READY
#   everything else -> 404
# supervisor.py (C)
@dataclass class PodEnv: op_id; transport; port; plan: WeightsPlan; llama_args: list[str]; api_key; weights_token; tls_cert_pem; tls_key_pem; hf_token; weights_dir: Path; llama_bin: str = "/app/llama-server"
def read_env(env: Mapping[str, str]) -> PodEnv           # EnvError without values
def llama_argv(pe: PodEnv, files: dict[str, Path]) -> list[str]
def child_env(env: Mapping[str, str]) -> dict[str, str]  # drops LLMHUB_* secrets, HF_TOKEN
async def main(env=os.environ) -> int                    # front up first, then fetch -> llama -> READY; SIGTERM -> stop child
# __main__.py: asyncio.run(main())
```
Алгоритм `Fetcher.run`. Для каждого файла:
1. Если есть `<id>.gguf` и маркер `<id>.gguf.sha256` с той же суммой → пропустить.
2. Проверить свободное место: нужно ≥ `size − уже скачано` + 1 ГиБ, иначе `disk`.
3. Перебирать источники с текущего индекса; индекс «прилипает» к следующим файлам.
4. Попытка:
   - `GET base_url+paths[id]` с `Range: bytes=<len(.part)>-`;
   - ответ 206 с правильным `Content-Range` → дописывать;
   - ответ 200 на Range-запрос → обрезать файл и начать с 0;
   - 416 → `.part` больше файла: обрезать до 0 и повторить один раз;
   - sha256 считается потоково; при докачке сначала хешируется уже скачанный префикс;
   - каждую секунду `on_progress`.
5. Переход к следующему источнику, `.part` сохраняется:
   - 401/403/404/410 → сразу;
   - 5xx и сеть (connect/read timeout) → повтор с backoff 1–2 с до `retries_per_source`;
   - `too_slow()` → только если следующий источник есть; последний источник по скорости не прерывается.
6. По окончании:
   - размер ≠ `size` → `size_mismatch`;
   - sha ≠ → удалить `.part`, записать в `failovers` и перейти к следующему источнику;
   - совпало → `os.replace` в `<id>.gguf` + маркер.
7. Нет источников → `FetchError("exhausted")`.

Безопасность загрузчика:
- `follow_redirects` вручную, не больше 5 переходов; при смене хоста `Authorization` снимается;
- токен `self_hosted` уходит только на хост `base_url` этого источника, `HF_TOKEN` — только на `huggingface.co`;
- `ca_pem` задан → `ssl.create_default_context(cadata=ca_pem)`, системные CA не грузятся; без `ca_pem` → системные CA;
- `http://` не принимается (тестовый флаг `allow_http` только в конструкторе, не через env).

### 5.2 Контроллер (потоки A, D, E)
```python
# weights/tokens.py (D)
TOKEN_VERSION = "v1"
LOADING_STATES = frozenset({"PROVISIONING", "BOOTING", "LOADING_MODEL"})
def token_expiry(created_at: float, t: TimeoutsConfig) -> int
def issue_token(secret: bytes, op_id: str, exp: int) -> str     # "v1.<op>.<exp>.<sig43>", deterministic
class Verdict(enum.StrEnum): OK="ok"; MISSING="missing"; MALFORMED="malformed"; BAD_SIG="bad_signature"; EXPIRED="expired"; OP_MISMATCH="op_mismatch"; UNKNOWN_OP="unknown_op"; REVOKED="revoked"; PATH="path_not_allowed"
def check_request(secret: bytes, authorization: str | None, uri: str, now: float,
                  state_of: Callable[[str], str | None], allowed: frozenset[str]) -> tuple[Verdict, str | None]  # (verdict, op_id)
# weights/auth_app.py (D)
def build_auth_app(db_path: Path, secret: bytes, allowed: frozenset[str], clock: Clock) -> Starlette
#   GET /weights/auth: X-Original-URI + Authorization -> 204 | 401 (missing) | 403; DB opened read-only (mode=ro, busy_timeout 300 ms);
#   DB busy -> 503 (the pod then fails over to HF); log: op_id + verdict only
def main(argv: list[str] | None = None) -> int     # python -m llmhub.weights.auth_app --config ... (listen = weights.auth_listen)

# cloud_manager/instance_env.py (E)
@dataclass(frozen=True)
class InstanceMaterial: env: dict[str, str]; secret_env: dict[str, str] = field(repr=False); tls_fingerprint: str | None
def derive_api_key(instance_secret: bytes, op_id: str) -> str
def derive_tls(instance_secret: bytes, op_id: str, created_at: float) -> tuple[str, str, str]   # cert_pem, key_pem, sha256(DER) hex; Ed25519, deterministic
def build_material(inst: sqlite3.Row, pool: PoolConfig, cfg: Config, secrets: InstanceSecrets, ca_pems: Mapping[str, str]) -> InstanceMaterial
@dataclass(frozen=True) class InstanceSecrets: instance: bytes = field(repr=False); weights: bytes = field(repr=False); hf_token: str | None = field(default=None, repr=False)

# provider_adapters/base.py (E): InstanceSpec + `secret_env: Mapping[str, str] = field(default_factory=dict, repr=False)` (last field, default -> old calls work)
# cloud_manager/pools.py (E): PoolRuntime + `material: Callable[[sqlite3.Row], InstanceMaterial] | None = None`
# cloud_manager/provisioner.py (E): _start_create -> if rt.material: spec gets env/secret_env; tls_fingerprint written in the same intent tx

# cloud_manager/inference.py — FROZEN, not changed (ping/health/test_request/run, JobRunSpec/JobRunResult, InstanceLost/NetworkLost)
# cloud_manager/load_status.py (E) — optional SEPARATE protocol, checked with isinstance (runtime_checkable)
@dataclass(frozen=True) class LoadStatus: status: Literal["loading","ok","failed"]; phase: str | None; source: str | None; done: int; total: int; mb_s: float; reason: str | None
@runtime_checkable
class LoadStatusSource(Protocol):
    async def load_status(self, endpoint: str) -> LoadStatus | None: ...
```
Облачный backend (поток G) описан в 5.3.
Провайдер не узнаёт о секретах иначе как через `create(spec)`. `MockProvider` их не хранит; тест проверяет, что `repr(spec)` не содержит значений.

Прогресс в контроллере (E): в шаге LOADING_MODEL `provisioner` вызывает `inference.load_status(ep)`, только если `isinstance(inference, LoadStatusSource)`:
- результат пишется в `controller_state` ключом `load_progress:<iid>` (JSON без секретов, не чаще раза в `POLL_INTERVAL_S`);
- `status == "failed"` → E7 (LOADING_MODEL → ERROR, reason `weights_failed:<reason>`) сразу, без ожидания 20 мин;
- `None` или `loading` → как сейчас.

`MockInference` получает метод `load_status` (реализует `LoadStatusSource`, протокол `InferenceBackend` не трогается): `loading` или `ok` по своим knobs, `failed` — по новому knob `load_fail`.

### 5.3 Облачный `HttpInferenceBackend` (поток G)
```python
# cloud_manager/http_inference.py (G): implements the frozen InferenceBackend + LoadStatusSource
@dataclass(frozen=True)
class Timings:                # llama-server "timings" (non-stream JSON)
    prompt_n: int; prompt_ms: float; predicted_n: int; predicted_ms: float
    draft_n: int | None; draft_n_accepted: int | None      # MTP; absent -> None
    @property
    def pp_tps(self) -> float: ...
    @property
    def tg_tps(self) -> float: ...
    @property
    def accept_rate(self) -> float | None: ...
def parse_timings(body: Mapping[str, Any]) -> Timings | None    # tolerant: missing/garbage -> None, never raises
class HttpInferenceBackend:
    def __init__(self, conn: sqlite3.Connection, secrets: InstanceSecrets, cfg: Config, *,
                 transport_factory: Callable[[ssl.SSLContext | bool], httpx.AsyncBaseTransport] | None = None,
                 on_timings: Callable[[str, Timings], None] | None = None,
                 on_pin_mismatch: Callable[[str], None] | None = None) -> None
    async def ping(self, endpoint: str) -> bool          # GET /ping -> 204; any exception -> False
    async def health(self, endpoint: str) -> bool        # GET /health with key -> True only on 200 {"status":"ok"}
    async def test_request(self, endpoint: str) -> bool  # POST /v1/chat/completions, max_tokens 8, temperature 0 -> 200 + non-empty choices
    async def run(self, endpoint: str, job: JobRunSpec) -> JobRunResult
    async def load_status(self, endpoint: str) -> LoadStatus | None
    async def aclose(self) -> None
```
Таймауты — `InferClientConfig` в `InferConfig.client` (поток A добавляет):
| Поле | По умолчанию |
|---|---|
| `connect_s` | 10 |
| `ping_s` | 5 |
| `health_s` | 10 |
| `test_s` | `timeouts.ready_test_timeout_s` (30) |
| `read_s` | 120 — пауза между байтами работающей задачи; prefill 64k ≈ 55 с при 1200 ток/с, а при `stream=false` байтов нет до конца |
| `job_cap_factor` | 3.0 — общий предел `run` = (`tokens_in` / `pp_tps` + `max_tokens` / `tg_tps`) × factor + 30 с |

Правила:
- **Учётные данные — по строке инстанса** (раздел 2a, критик). Замороженный протокол передаёт только endpoint, поэтому endpoint сопоставляется со строкой: `SELECT id, op_id, created_at, pool, tls_fingerprint FROM instances WHERE endpoint=? AND state IN (<живые>)`.
  - Ровно одна строка → дальше всё по `id`/`op_id`: ключ `derive_api_key(op_id)` на всё, кроме `/ping`, сертификат `derive_tls(op_id, created_at)`.
  - 0 или > 1 строк → ping/health/test = False, `run` → `NetworkLost`; при > 1 — лог high.
  - Кэш по `(endpoint, id)`, сбрасывается при смене состояния.
- **TLS:**
  - `direct` → `ssl.create_default_context(cadata=derive_tls(...)[0])` без системных CA, `check_hostname=False`; сертификат — единственный якорь (pin). После рукопожатия `sha256(DER)` пира сверяется с `instances.tls_fingerprint` (через `httpx` extension `network_stream` → `ssl_object`). Несовпадение → False / `NetworkLost` + `on_pin_mismatch(endpoint)` (E пишет инцидент `tls_pin_mismatch`, high);
  - `proxy` → системные CA, только `https://`.
- **Отображение ошибок `run`:**
  - `NetworkLost` — connect/read timeout, `ConnectError`, `RemoteProtocolError`, обрыв посреди ответа, `ssl.SSLError`, общий предел `job_cap`; 502/503/504 без JSON нашего фронта (прокси RunPod без пода);
  - `InstanceLost` — фронт ответил 500 `{"status":"failed"}` (llama-server упал), 503 `{"status":"loading"}` во время `run` (контейнер перезапущен), 401 (по этому endpoint не наш под; лог high);
  - 4xx от llama-server (400: контекст и т. п.) → `JobRunResult(finish_reason="error", body=ответ)`.

  На шаге G1 сверить с тем, как ядро (`controller.py`, `scheduler`) обрабатывает `run`: если ядро ждёт исключения для 4xx — следовать ядру и записать отклонение в журнал задачи.
- **Результат:** `stream=false`, тело задачи пересылается как есть; `tokens_in`/`tokens_out` из `usage`, иначе из `timings` (`prompt_n`/`predicted_n`); `on_timings(job_id, t)` — для EWMA `pp_tps`/`tg_tps` и доли принятых MTP (потребитель — ядро/T-371, в G только колбэк).
- **Повторов внутри нет** (повторы — у ядра); `CancelledError` пробрасывается, соединение закрывается.

## 6. Сервер весов (поток D)
`deploy/nginx-weights.conf` — шаблон с плейсхолдерами `__PUBLIC_IP__`, `__PORT__` (по умолчанию 9443), `__RATE__` (по умолчанию `80m`), `__MODELS__` (`/opt/models`), `__AUTH__` (`127.0.0.1:8096`). Самостоятельный `nginx.conf`: свой `pid`, `worker_processes 1`, `worker_priority 10`.
- `listen __PUBLIC_IP__:__PORT__ ssl;` — только публичный IPv4, не `10.8.0.1`, не `0.0.0.0`; `ssl_protocols TLSv1.3`; `server_tokens off`.
- `log_format weights '$remote_addr [$time_local] "$request_method $uri" $status $body_bytes_sent $request_time op=$weights_op';` — без `$request`, `$args` и заголовков; `error_log ... warn`.
- `map $uri $weights_op { "~^/w/(?<o>[0-9a-f]{12})/" $o; default ""; }`
- `limit_conn_zone $server_name zone=w_all:1m; limit_conn_zone $weights_op zone=w_op:1m; limit_req_zone $binary_remote_addr zone=w_req:1m rate=10r/s;`
- `location ~ "^/w/[0-9a-f]{12}/(?<rel>[a-z0-9][a-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*\.gguf)$"`:
  - доступ: `limit_except GET { deny all; }`, `auth_request /_auth;`;
  - лимиты: `limit_conn w_all __SEGMENTS__; limit_conn w_op __SEGMENTS__; limit_conn_status 503;` (`__SEGMENTS__` = `segments` источника `self_hosted`, по умолчанию 1: одна загрузка с нашего сервера за раз, второй под уходит на HF — раздел 2a п. 1); limit_rate __RATE__; limit_req zone=w_req burst=20 nodelay;`;
  - файлы: `alias __MODELS__/$rel; disable_symlinks on; autoindex off; default_type application/octet-stream; add_header Cache-Control no-store;`.
- `location = /_auth { internal; proxy_pass http://__AUTH__/weights/auth; proxy_pass_request_body off; proxy_set_header Content-Length ""; proxy_set_header X-Original-URI $uri; proxy_set_header Authorization $http_authorization; }`
- `location / { return 404; }`
- Второй `access_log __LOGDIR__/weights-acct.log acct;` с `log_format acct '$weights_op $body_bytes_sent $status';` — для лимита объёма валидатора (раздел 2a п. 2).
- В `/_auth` передаются ещё `proxy_set_header X-Real-IP $remote_addr; proxy_set_header X-Range $http_range;`.
- Валидатор дополнительно сверяет `rel` со списком `allowed` — путями `self_hosted` из всех наборов (вторая линия защиты от обхода каталога).

`deploy/gen_weights_cert.py <ip> <outdir>` (через `cryptography`, без `openssl`; раздел 2a, критик): EC P-256, `subjectAltName=IP:<ip>`, `basicConstraints=critical,CA:FALSE`, `keyUsage=digitalSignature`, `extendedKeyUsage=serverAuth`, 3650 дней; ключ 0600 (`os.open` с 0o600); печатает sha256 сертификата. Для тестов — помощник `tests/tls_certs.py` (`make_self_signed(ip)`, `make_ed25519(...)`, на `cryptography`), его создаёт поток 0; им пользуются B, C, D, G.

## 7. Шаги (TDD)
Команда проверки любого шага: `uv run pytest tests/<файл> -q && uv run ruff check <файлы> && uv run pyright <файлы>`.

### Поток 0 — подготовка (координатор или первый implementer, haiku, ~10 мин)
- [x] 0.1 `pyproject.toml`:
  - зависимость `cryptography` (Ed25519, сертификаты; есть в колёсах для 3.12);
  - `[tool.pytest.ini_options] pythonpath = ["infer-image"]`, маркер `docker`;
  - `[tool.pyright] include += ["infer-image"]`, `extraPaths += ["infer-image"]`;
  - `uv sync`.
- [x] 0.2 Пустые пакеты: `infer-image/llmhub_infer/__init__.py` (`__version__ = "0.1.0"`), `src/llmhub/weights/__init__.py` (докстринг есть), `deploy/` (уже есть).
- [x] 0.2b `tests/tls_certs.py` — генерация тестовых сертификатов через `cryptography` (P-256 с IP SAN, Ed25519) + маленький тест на них.
- [x] 0.3 `tests/test_w_isolation.py` (сначала тест): `llmhub_infer` не импортирует `llmhub` (AST-обход), `src/llmhub` не импортирует `llmhub_infer`. Проверка: `uv run pytest tests/test_w_isolation.py`.

### Поток A — конфиг (haiku; файлы: `configuration/models.py`, `configuration/loader.py`, `examples/config.cloud.example.yaml`, `tests/test_w_config.py`)
- [x] A1 Тесты, затем модели из раздела 4:
  - валидный пример грузится;
  - `kind: r2` → ошибка;
  - `self_hosted` с `http://` или без `ca_pem_file` → ошибка;
  - путь с `..` или ведущим `/` → ошибка;
  - запрещённый флаг в `llama_args` (`--host`, `-m`, `--api-key`, `--ssl-key-file`) → ошибка;
  - пул ссылается на несуществующий набор → ошибка;
  - нет пути для файла в источнике → ошибка;
  - старые конфиги T-368 (без `weights`) валидны.
- [x] A2 `load_instance_secret` и `load_weights_secret`: нет / короче 32 байт → `ConfigError`, в сообщении нет значения. `load_weights_ca` читает PEM; файл не найден → `ConfigError`.
- [x] A2b `InferClientConfig` (поля и умолчания — таблица в 5.3; все > 0) + тест умолчаний и отказа на `≤ 0`.
- [x] A3 `examples/config.cloud.example.yaml` + тест «пример грузится».

### Поток B — загрузчик пода (sonnet; файлы: `infer-image/llmhub_infer/{plan,speed,fetch,progress}.py`, `tests/i_fakes.py`, `tests/test_i_plan.py`, `tests/test_i_speed.py`, `tests/test_i_fetch.py`)
- [x] B1 `tests/i_fakes.py` — `FakeWeightsServer` (ThreadingHTTPServer в потоке, порт 0, опционально TLS по сертификату из фикстуры `openssl`; без `openssl` — skip). Knobs:
  - `files: dict[path, bytes]`, `range_support`, `ignore_range` (отвечает 200);
  - `status_for: dict[path, int]`, `fail_first_n`;
  - `throttle_bps`, `drop_after_bytes` (обрыв), `corrupt` (подмена байта);
  - `redirect_to` (другой хост);
  - журнал запросов (путь, заголовки `Range` и `Authorization`).
- [x] B2 `test_i_plan.py` → `plan.py`: валидный план; неизвестная версия, нет `model`, повтор id, путь с `..`, `http://` → `PlanError`; сообщение не содержит токена.
- [x] B3 `test_i_speed.py` → `speed.py` на фейковых часах:
  - до grace — никогда не медленно;
  - после grace средняя за окно < порога → медленно;
  - всплеск после паузы считается по окну;
  - `min_bps = 0` → никогда.
- [x] B4 `test_i_fetch.py` → `fetch.py`, по одному тесту на сценарий (данные — 64–256 КиБ, без реальной сети):
  1. happy path, один источник: sha совпал, маркер записан, `.part` удалён;
  2. **Range-докачка**: обрыв после N байт → повтор с `Range: bytes=N-`, итог верный, префикс перехеширован;
  3. сервер игнорирует Range (200) → файл начат заново, итог верный;
  4. 416 при `.part` больше файла → обрезка и успех;
  5. **failover по ошибке**: 1-й источник 503 × (retries+1) → 2-й; `failovers` содержит причину; `.part` 1-го продолжен 2-м (Range со смещением);
  6. 403 → переход сразу, без повторов;
  7. **failover по скорости**: 1-й троттлинг 20 КиБ/с, `min_speed` 200 КиБ/с, grace 0,3 с, окно 0,2 с → переход ≤ 2 с, итог с 2-го;
  8. последний источник медленный → не прерывается, итог верный;
  9. **неверный sha**: 1-й отдаёт подменённые байты → `sha_mismatch`, `.part` удалён, 2-й даёт верный файл; все источники битые → `FetchError("exhausted")`;
  10. несовпадение размера → `size_mismatch`;
  11. повторный запуск с готовым файлом и маркером → ни одного запроса;
  12. мало места (подменённый `disk_usage`) → `disk`;
  13. **безопасность**:
      - токен отправлен только на `self_hosted`;
      - редирект на другой хост → без `Authorization`;
      - `HF_TOKEN` не уходит на `self_hosted`;
      - TLS: сервер с другим сертификатом, чем `ca_pem`, → ошибка соединения и failover;
      - в логах (caplog) нет токена;
  14. «прилипание» источника: после перехода на 2-м файле 1-й не запрашивается;
  15. `on_progress` монотонен по `done`, `total` = сумма размеров.
- [x] B5 `progress.py`: атомарная запись, права 0600, чтение битого файла → `None`.

### Поток C — фронт, супервизор, образ (sonnet; файлы: `infer-image/llmhub_infer/{front,supervisor,__main__}.py`, `infer-image/{Dockerfile,requirements.in,requirements.txt,.dockerignore}`, `tests/i_llama_fake.py`, `tests/test_i_front.py`, `tests/test_i_supervisor.py`, `tests/test_i_image.py`)
- [x] C1 `test_i_front.py` → `front.py` (httpx ASGITransport + фейковый upstream — ASGI-приложение):
  - `/ping` 204 без ключа;
  - `/health` без ключа 401, неверный ключ 401;
  - фазы loading → 503 с прогрессом; failed → 500; ok → 200 только при upstream `/health` 200; upstream 503 «Loading model» → 503 `phase=llama_load`;
  - `/v1/chat/completions`: проксируется, `Authorization` до upstream не доходит, SSE-поток идёт по частям (≥ 2 чанка до конца), тело > 8 МиБ → 413;
  - до READY → 503;
  - неизвестный путь → 404;
  - сравнение ключа через `hmac.compare_digest` (тест через monkeypatch-шпион).
- [x] C2 `test_i_supervisor.py` → `supervisor.py`:
  - `read_env`: нет ключа, неверный транспорт, `direct` без сертификата → `EnvError` без значений;
  - `llama_argv` = бинарник + доп. аргументы + `-m … -md … --host 127.0.0.1 --port 8081`; запрещённые флаги отвергаются;
  - `child_env` без `LLMHUB_API_KEY`, `LLMHUB_WEIGHTS_TOKEN`, `LLMHUB_TLS_*`, `HF_TOKEN`;
  - сертификат и ключ записаны в `/run/llmhub` (tmp в тесте) с правами 0600.
- [x] C3 Интеграционный тест супервизора (`tests/i_llama_fake.py` — скрипт, слушает порт, `/health` 503 → 200 через 0,5 с; `llama_bin` подменяется только аргументом `main(..., llama_bin=...)`, не через env образа). Сценарии:
  - фейковый сервер весов → загрузка → запуск фейковой llama → `/health` 200;
  - llama завершилась → один перезапуск → второй выход → `failed`;
  - SIGTERM → дочерний процесс остановлен, код 0;
  - `direct`: uvicorn с TLS, клиент с `cadata=cert` подключается, с чужим CA — нет.
- [x] C4 Образ:
  - `requirements.in` (starlette, uvicorn, httpx — версии из `uv.lock`);
  - `uv pip compile --generate-hashes --python-version 3.12 infer-image/requirements.in -o infer-image/requirements.txt` (сеть PyPI допустима, платного нет);
  - `Dockerfile`:
    - `ARG LLAMA_IMAGE=ghcr.io/ggml-org/llama.cpp:server-cuda12-b12063@sha256:d5ffba7d…bccda`, `FROM ${LLAMA_IMAGE}`;
    - apt `python3 python3-venv` `--no-install-recommends`, очистка списков;
    - venv `/opt/llmhub/venv`, `pip install --require-hashes --no-cache-dir`;
    - `COPY llmhub_infer /opt/llmhub/llmhub_infer`;
    - `ENV PYTHONPATH=/opt/llmhub PYTHONUNBUFFERED=1 LLMHUB_WEIGHTS_DIR=/models`;
    - `HEALTHCHECK NONE` (базовый curl `/health` без ключа неверен);
    - `EXPOSE 8443 8080`;
    - `ENTRYPOINT ["/opt/llmhub/venv/bin/python","-m","llmhub_infer"]`;
  - `.dockerignore` (`**/__pycache__`, `*.gguf`, `.env`, `secrets/`).

  `test_i_image.py` (статический, без docker):
  - `FROM` пиннут по `@sha256:`;
  - нет `ENV`/`ARG` с именами `*TOKEN*`, `*KEY*`, `*SECRET*`, `*PASSWORD*`;
  - нет `COPY`/`ADD` `*.gguf` и `ADD http`;
  - есть `HEALTHCHECK NONE`, `ENTRYPOINT` модуля;
  - `requirements.txt` содержит `--hash=` для каждой строки;
  - версии в `requirements.txt` = версиям в `uv.lock`.
- [x] C5 Ручная проверка (не pytest; только если есть docker, вывод — в журнал задачи):
  1. `docker run --rm -i hadolint/hadolint:v2.12.0 < infer-image/Dockerfile` — замечаний уровня warning нет или каждое обосновано в Dockerfile;
  2. `nice -n 10 docker build -t llmhub-infer:dev infer-image/` (~2,5 ГБ базы, push запрещён);
  3. `docker run --rm --entrypoint /app/llama-server llmhub-infer:dev --help | grep -E 'draft-mtp|spec-draft-n-max'` — подтверждает флаги MTP в b12063; если флагов нет → стоп, вопрос координатору (запасной вариант R10);
  4. `docker run --rm llmhub-infer:dev` без env → код ≠ 0 и понятная ошибка `EnvError` без трассы с секретами.

  GPU локально нет: llama-server в контейнере не запускается — это проверка этапа 4.

### Поток D — токены, валидатор, nginx (sonnet; файлы: `src/llmhub/weights/{tokens,auth_app}.py`, `deploy/{nginx-weights.conf,gen_weights_cert.py}`, `tests/test_w_tokens.py`, `tests/test_w_auth_app.py`, `tests/test_w_nginx_conf.py`)
- [x] D1 `test_w_tokens.py` → `tokens.py`:
  - выдача детерминирована;
  - верный токен в LOADING_MODEL → OK;
  - **повтор после READY** (и после ERROR, STOPPING, STOPPED) → REVOKED;
  - **истёкший** (`now ≥ exp`) → EXPIRED;
  - **чужой op_id** (токен A, путь `/w/<B>/…`) → OP_MISMATCH;
  - подпись другим секретом → BAD_SIG;
  - испорченный формат, пустой и не-Bearer заголовок → MALFORMED/MISSING;
  - op нет в БД → UNKNOWN_OP;
  - путь не из `allowed` или с `..` → PATH;
  - сравнение через `hmac.compare_digest`;
  - `token_expiry` = created_at + сумма трёх таймаутов.
- [x] D2 `test_w_auth_app.py` → `auth_app.py` на временной БД со схемой T-368 (`persistence.db`):
  - 204, 401, 403 для сценариев D1;
  - БД открыта только на чтение (запись падает);
  - заблокированная БД → 503;
  - caplog: есть `op_id` и verdict, нет токена;
  - `main` читает `weights.auth_listen` и не стартует без `LLMHUB_WEIGHTS_SECRET`.
- [x] D3 `deploy/gen_weights_cert.py` + тест (без skip): сертификат в tmp, разбор через `cryptography` показывает IP SAN, CA:FALSE, serverAuth; ключ 0600; Python-клиент с `cadata=cert` подключается к тестовому TLS-серверу по `127.0.0.1` (как в R4).
- [x] D4 `test_w_nginx_conf.py`:
  - статика шаблона: нет `0.0.0.0`, нет `10.8.0.1`, `listen` с плейсхолдером IP;
  - в `log_format` нет `$request`, `$args`, `$http_authorization`, `$request_uri`;
  - есть `limit_conn w_all __SEGMENTS__`, `limit_conn w_op __SEGMENTS__`, второй лог `acct`, `limit_rate`, `auth_request`, `disable_symlinks on`, `limit_except GET`;
  - regex `location` отвергает `../x.gguf`, `a/../../etc/passwd`, `x.txt`, `/w/<не hex>/…` (проверка самим regex через `re` с переводом PCRE-синтаксиса);
  - `@pytest.mark.docker` (skip без docker или без локального образа `nginx:1.25-alpine` — он уже есть локально, тест не тянет образы): рендер шаблона (IP 127.0.0.1, сертификат из D3), `docker run --rm -v … nginx:1.25-alpine nginx -t -c /etc/nginx/weights.conf` → код 0.
- [x] D5 (**обязателен при наличии docker**, `@pytest.mark.docker`; без docker — причина в журнал; раздел 2a) e2e: контейнер nginx (`--network host`, порт из свободных) + `auth_app` uvicorn на хосте + каталог с маленькими `.gguf`. Сценарии:
  - `Fetcher` (поток B) скачивает через nginx с токеном;
  - после перевода инстанса в READY та же докачка получает 403 → failover на фейковый «hf»;
  - второе одновременное соединение (при `__SEGMENTS__`=1) → 503;
  - обход пути: `/w/<op>/%2e%2e/%2e%2e/etc/passwd`, `/w/<op>//etc/passwd`, `/w/<op>/qwen%2F..%2Fx.gguf`, `/w/<op>/a/../../x.gguf` → 400/403/404, файл вне каталога не отдан;
  - превышение лимита объёма → 403; запрос с другого IP (`X-Real-IP` в тесте не подделать — второй клиент через другой адрес контейнерной сети или прямой вызов валидатора) → 403;
  - access-лог контейнера не содержит токена.

  Выполнять после B; иначе — вместе с F.

### Поток E — интеграция в контроллер (sonnet; зависит от A и D1; файлы: `provider_adapters/base.py`, `cloud_manager/{instance_env,load_status,pools,provisioner}.py`, `provider_adapters/mock_inference.py`, `tests/test_w_instance_env.py`, `tests/test_w_provisioner_loading.py`, `tests/test_w_contract.py`)
- E1 `InstanceSpec.secret_env` (последнее поле, default, `repr=False`):
  - `test_b_types` не ломается;
  - новый тест: `repr(spec)` не содержит секрета;
  - `dataclasses.asdict` используется где-либо? — grep; если да, исключить поле.
- E2 `test_w_instance_env.py` → `instance_env.py`:
  - `derive_api_key` детерминирован и различается по op;
  - `derive_tls` детерминирован (два вызова → тот же PEM и отпечаток), Ed25519, срок ≥ `max_lifetime`; TLS-сервер с этим сертификатом принимается клиентом с `cadata`, чужой — нет;
  - `build_material`:
    - env по разделу 3, `LLMHUB_WEIGHTS_PLAN` с `{op_id}` подставленным в `base_url`;
    - `secret_env` содержит токен, ключ и (direct) TLS;
    - `proxy` → без TLS и без отпечатка;
    - без `self_hosted` в цепочке → без токена;
    - `HF_TOKEN` только если задан;
  - ни одно секретное значение не попадает в `env`.
- E3 `test_w_contract.py`: `build_material` → `env|secret_env` → `llmhub_infer.supervisor.read_env` разбирает без ошибок, план совпадает поле в поле. Это единственное место, где тест импортирует оба пакета.
- E4 Провижининг:
  - `PoolRuntime.material`;
  - `_start_create` в той же tx намерения пишет `tls_fingerprint`; spec получает env/secret_env;
  - `direct` → `OfferQuery.need_public_ip=True`.

  Тесты (`test_w_provisioner_loading.py`, на Mock и harness T-368):
  - шпион-адаптер видит env и secret_env;
  - повторный create (unknown outcome) с тем же label → тот же токен и отпечаток;
  - в `transitions`, `incidents`, `last_error` и логах нет секретов (grep по БД-дампу и caplog).
- E5 `cloud_manager/load_status.py` (`LoadStatus`, `LoadStatusSource`) + `MockInference.load_status` (knob `load_fail`). В LOADING_MODEL при `isinstance(inference, LoadStatusSource)`:
  - `failed` → E7 сразу с reason `weights_failed:<reason>`;
  - прогресс — в `load_progress:<iid>`;
  - `on_pin_mismatch` → инцидент `tls_pin_mismatch` (high).

  Тесты:
  - E7 раньше `loading_model_s`;
  - прогресс обновляется;
  - backend без `load_status` работает по-старому;
  - `cloud_manager/inference.py` не изменён (тест сравнивает множество методов протокола с замороженным списком).

  Регрессия: `tests/test_s0_skeleton.py tests/test_f_transitions.py tests/test_f_provisioner.py tests/test_c_timeouts.py`.

### Поток G — облачный `HttpInferenceBackend` (sonnet; зависит от E2 (`derive_api_key`, `derive_tls`) и A (`InferClientConfig`); файлы: `cloud_manager/http_inference.py`, `tests/i_infer_fake.py`, `tests/test_g_timings.py`, `tests/test_g_http_inference.py`, `tests/test_g_http_inference_live.py`)
- G1 Чтение кода ядра: как `controller`/`scheduler` вызывают `run` и обрабатывают исключения и `finish_reason`; результат — в журнал (одна строка); таблица ошибок 5.3 подстраивается под ядро.
- G2 `test_g_timings.py` → `parse_timings`:
  - полный ответ llama-server с MTP (`draft_n`, `draft_n_accepted`) → `pp_tps`, `tg_tps`, `accept_rate`;
  - без MTP → `None` в draft-полях;
  - нет `timings`, нули, строки вместо чисел, отрицательные → `None` или корректные значения без исключений.
- G3 `tests/i_infer_fake.py` — фейковый `llmhub-infer` (Starlette-приложение, тот же контракт, что `llmhub_infer.front`). Knobs:
  - ключ, фаза (`loading`/`ok`/`failed`);
  - ответ чата с `usage` и `timings`;
  - задержка до первого байта, обрыв посреди тела, 4xx/5xx с телом и без;
  - «прокси без пода» (502 HTML).

  Подключается к backend через `transport_factory` (`httpx.ASGITransport`) — без сети и портов.
- G4 `test_g_http_inference.py` (ASGI-транспорт), отдельные тесты:
  - `ping` 204 → True; исключение → False;
  - `health`: ok → True; loading/failed → False; без ключа (неверный op) → False;
  - `test_request`: 200 + choices → True; пустые choices или 500 → False; задержка > `test_s` → False;
  - `run`:
    - happy path: тело переслано байт в байт, `Authorization` = ключ op, токены из `usage`, без `usage` — из `timings`, `on_timings` вызван;
    - 400 → `finish_reason="error"`;
    - обрыв → `NetworkLost`; read timeout → `NetworkLost`; `job_cap` → `NetworkLost`;
    - 502 HTML → `NetworkLost`;
    - 500 failed → `InstanceLost`; 503 loading → `InstanceLost`; 401 → `InstanceLost`;
    - отмена → `CancelledError`;
  - `load_status`: разбор JSON фронта во всех фазах; мусор → `None`;
  - endpoint не в БД → False / `NetworkLost`;
  - в логах нет ключа.
- G5 `test_g_http_inference_live.py` — настоящий `llmhub_infer.front` (поток C) в uvicorn на `127.0.0.1:0` за фейковым upstream llama:
  - `direct` с TLS из `derive_tls` → ping/health/test/run проходят;
  - сервер с другим сертификатом → False / `NetworkLost` и `on_pin_mismatch` вызван;
  - `proxy`-режим: локальный TLS-сервер с отдельным тестовым CA (в тесте подменяется контекст «системных CA»); `http://` отвергается.

  Зависит от C1; до готовности C — пропуск с пометкой (skip), не заглушка.
- G6 Подключение в composition root: где строится `PoolRuntime` для облачных провайдеров — `HttpInferenceBackend`; Mock — `MockInference`. Если composition root — в T-369/T-371, оставить фабрику `make_inference(cfg, conn, secrets, provider_name)` и строку во «Входящие» T-371.

### Поток F — интеграция и документы (haiku/sonnet, после всех)
- F1 Полный набор тестов задачи: `uv run pytest tests/test_w_*.py tests/test_i_*.py tests/test_g_*.py -q` + регрессия E5; `-m docker` отдельно; `uv run ruff check`, `uv run pyright` по всему дереву.
- F2 Короткий `infer-image/README.md` (TL;DR, env-контракт из раздела 3, health-семантика, ручные команды C5, «не публиковать без разрешения»).
- F3 Задача: журнал, отметки критериев; BOARD — только строка T-370 (статус `review`); во «Входящие» T-371 занести, что развернуть:
  - nginx-юнит;
  - `auth_app`-юнит;
  - сертификат;
  - секреты `LLMHUB_INSTANCE_SECRET` и `LLMHUB_WEIGHTS_SECRET`;
  - суммы из `sha256-T365.txt`;
  - tc/QoS для VPN (Q1).

## 8. Параллельные потоки
| Поток | Файлы (владение) | Зависит от | Модель |
|---|---|---|---|
| 0 | `pyproject.toml`, `uv.lock`, пустые `__init__`, `tests/test_w_isolation.py`, `tests/tls_certs.py` | — | haiku |
| A | `configuration/models.py`, `configuration/loader.py`, `examples/config.cloud.example.yaml`, `tests/test_w_config.py` | 0 | haiku |
| B | `infer-image/llmhub_infer/{plan,speed,fetch,progress}.py`, `tests/i_fakes.py`, `tests/test_i_{plan,speed,fetch}.py` | 0 | sonnet |
| C | `infer-image/llmhub_infer/{front,supervisor,__main__}.py`, `infer-image/{Dockerfile,requirements.*,.dockerignore}`, `tests/i_llama_fake.py`, `tests/test_i_{front,supervisor,image}.py` | 0; C3 использует `Fetcher` и `parse_plan` из B (до готовности B — заглушка с той же сигнатурой) | sonnet |
| D | `src/llmhub/weights/{tokens,auth_app,acct}.py` (`acct.py` — чтение `weights-acct.log` и состояние валидатора), `deploy/*` (`nginx-weights.conf`, `gen_weights_cert.py`), `tests/test_w_{tokens,auth_app,nginx_conf}.py` | 0; D2 читает `weights.auth_listen` из A (до готовности — аргумент CLI) | sonnet |
| E | `provider_adapters/{base,mock_inference}.py`, `cloud_manager/{instance_env,load_status,pools,provisioner}.py`, `tests/test_w_{instance_env,provisioner_loading,contract}.py` | A, D1; E3 — B и C | sonnet |
| G | `cloud_manager/http_inference.py`, `tests/i_infer_fake.py`, `tests/test_g_{timings,http_inference,http_inference_live}.py` | A (`InferClientConfig`), E2 (`derive_*`), E5 (`LoadStatus`); G5 — C1 | sonnet |
| F | `infer-image/README.md`, файл задачи, `BOARD.md` | все | haiku |

Схема: 0 → {A, B, C, D} параллельно → E → G → F. G2–G4 можно начинать параллельно с E, если E2/E5 сначала зафиксируют сигнатуры `derive_*` и `LoadStatus`. D5 — после B.

## 9. Безопасность (чек-лист ревьюера)
- [ ] В образе нет секретов, IP и сертификатов; `docker history` без `ENV`-секретов (C4/C5).
- [ ] Секреты только в `secret_env` (`repr=False`); не попадают в БД, логи, `transitions`, `incidents`, `last_error`, прогресс (E2, E4).
- [ ] Токен: HMAC-SHA256, сравнение за постоянное время, порядок проверок формат → HMAC → срок → (только потом) БД, привязка к op_id, пути и первому IP, лимит объёма 1,2 × Σsize, отзыв по состоянию, срок ≤ сумме таймаутов; в логах nginx и валидатора его нет (D1, D2, D4, D5).
- [ ] nginx: только публичный IP, только GET, только `*.gguf` из allowlist, без symlink, ≤ 2 соединения всего / 1 на op, `limit_rate`; отдельный экземпляр (свой pid и юнит) — VPN-интерфейс и 8090–8092 не затронуты (D4).
- [ ] Под: ключ на всё, кроме `/ping`; llama-server только на 127.0.0.1; секреты не в env дочернего процесса; ключ TLS в tmpfs 0600 (C1, C2).
- [ ] Загрузчик: токен только своему хосту, снятие `Authorization` на чужом редиректе, pin `ca_pem`, нет `http://` (B4.13).
- [ ] Контроллер ↔ под: `direct` — сертификат пода как единственный якорь (pin), `proxy` — системные CA + ключ, только https (G4, G5); протокол `InferenceBackend` не изменён (E5).

## 10. Риски и открытые вопросы
| # | Вопрос / риск | Блокирует? | Предложение |
|---|---|---|---|
| Q1 | **Решено на воротах**: `limit_rate 80m` (640 Мбит/с), одна загрузка за раз (`w_all` = `segments`, по умолчанию 1), второй под → HF. `tc` в T-370 нет; опционально — вопрос T-371 | — | — |
| Q2 | `min_speed_mb_s` по умолчанию 25 (ниже HF 37 МБ/с) — порог из ADR «по замеру этапа 4» | нет | 25 до замера Hetzner→RunPod в T-372 |
| Q3 | q8 KV (`-ctk q8_0 -ctv q8_0`, ADR п. 13) не входит в флаги замера 2026-11-04 | нет | по умолчанию флаги замера; q8 KV — строка в `llama_args` после проверки на 3090 (этап 4) |
| Q4 | Репозиторий HF и ревизия для 27B UD-Q4_K_M и `MTP/mtp-…` (T-365: `unsloth/…`, суммы = LFS-oid) | нет (пример с заглушкой) | T-371 заполняет из `sha256-T365.txt` и пиннит `revision` коммитом |
| Q5 | Владелец GHCR (`<owner>`) и разрешение на push публичного образа | нет для T-370 | T-371/T-372, отдельное разрешение |
| R-1 | Образ b12063 ≠ бинарники замера b12084 | нет | C5 п. 3 проверяет флаги; поведение MTP — на этапе 4 |
| R-2 | Холодный pull образа 2,5 ГБ + Python-слой попадает в BOOTING | нет | замер на этапе 4; вес Python-слоя ≈ 40–60 МБ |
| R-3 | Env провайдера видит владелец аккаунта RunPod (наш аккаунт) — секреты пода хранятся у провайдера | нет | ключи на один инстанс и отзыв по состоянию; принято ADR п. 12 |
| R-4 | Ed25519 в TLS-клиентах: OpenSSL 3 и httpx поддерживают (проверено 2026-11-04: TLS 1.3, `cadata` = сертификат) | нет | запасной вариант — P-256 с ключом из HMAC через `ec.derive_private_key` + сохранение сертификата в kv |
| R-5 | Валидатор недоступен → nginx 500 → под уходит на HF (медленнее, но работает) | нет | так задумано; метрика в T-371 |
| R-6 | Источник отдал корректный префикс и битый хвост → докачка с другого источника даёт sha mismatch и полную перекачку | нет | редкий случай; полная перекачка со следующего источника (B4.9) |

Блокирующих вопросов нет.

## 11. Критерии приёмки → шаги
| Критерий задачи | Шаги |
|---|---|
| `weights.sources: [self_hosted, hf]`, R2 — опция; манифест sha256; проверка суммы; Range; переход при ошибке и при скорости после 30 с — отдельные тесты на фейковых HTTP | A1, B2–B4 (2, 3, 5, 7, 8, 9) |
| Сервер весов: read-only HTTPS только для GGUF; токен на инстанс, привязан к op_id, до READY или таймаута, затем отозван; `limit_rate`; ≤ 2 загрузки; VPN и llama-server не затронуты; тесты: повтор, истёкший, чужой op_id | D1–D5, раздел 6, Q1 |
| Образ `llmhub-infer`: llama.cpp server-cuda (CUDA 12.8) внутри, TLS-прокси + ключ API, загрузчик; веса не запекаются; lint Dockerfile + юнит-тесты загрузчика | C1–C5, B4 |
| TLS `direct` (pin) и `proxy` (ключ) в конфиге и спецификации инстанса | A1 (`InferConfig.transport`), E2, E4, G4, G5 |
| (Q6 координатора) облачный `InferenceBackend`: pin/ключ, готовность, таймауты, NetworkLost/InstanceLost, тайминги MTP; протокол заморожен | G1–G6, E5 |
| Ничего не открыто на сервере и не опубликовано; pytest, ruff, pyright чисто; ревью ≤ 3 раундов | F1, раздел 1 «Не цели», C5 (без push) |

## 12. Критерии остановки
- Тесты задачи (`tests/test_w_*.py`, `tests/test_i_*.py`, `tests/test_g_*.py`) и регрессия E5 зелёные; `-m docker` зелёные или пропущены с причиной в журнале.
- `uv run ruff check` и `uv run pyright` по всему дереву чисто.
- Каждый сценарий B4 (1–15), D1, C1, G4 — отдельный тест; чек-лист раздела 9 закрыт.
- [x] C5 выполнен или в журнале указана причина пропуска (нет docker или сети).
- Нет открытых blocker/major у ревьюера; ≤ 3 раунда. Сложные части — загрузчик (B4), токены + провижининг (D1, E4, E5) и отображение ошибок `run` (G4) — по 2 независимых цикла ревью (логика и гонки; сбои и перезапуски).
