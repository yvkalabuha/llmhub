# T-369 — план: API, router, локальный инференс

> TL;DR: Внешний контракт llmhub поверх ядра T-368: FastAPI в одном процессе с `Controller` и новым `LocalWorker` (один event loop, одно SQLite-соединение, все обработчики `async`). OpenAI-совместимый `/v1/chat/completions` (+ `stream` с keep-alive, ответ отдаётся буфером), `/v1/models`, `/v1/tasks`, `/v1/leases`, `/admin/*`, `/healthz`/`/readyz`/`/metrics`; ошибки — объект OpenAI `error` с `code/reason/retryable/retry_after_s`. Router — чистая функция правил ECONOMY/BALANCED/QUALITY. Локальные llama-server — HTTP-клиенты с одним CPU-семафором, EWMA и пробой. Нормативны `docs/openapi.yaml` (тест на расхождение) и curl-примеры (исполняются в тесте). MCP-сервер `llmhub_ask` вынесен в T-373 (ворота). 34 шага, 6 потоков; G первым, затем H, R, W параллельно, потом I и O. Ворота critic + advocate пройдены 2026-11-04 (§11). Блокирующих вопросов нет.

Задача: `tasks/active/T-369-api-router.md`. Источники: ADR-0138 (п. 2, 8, 10, 11, 12, 14); `/var/www/projectVoiceBot/platform/docs/research/T-367-cloud-gpu-manager.md` §6.7 (локальная очередь), §6.9 (ошибки), §6.10 (API), §6.11 (router), §6.12 (безопасность), «Ворота 2026-11-04» п. 4, 5, 7–11 (главнее); ТЗ `T-367-tz-v2.md` §9, §10, §14. Интерфейсы ядра — журнал `tasks/done/T-368-core-on-mock.md` и код (`controller.py`, `task_queue/queue.py`, `lifecycle_manager/leases.py`).

## 1. Объём и что не входит

**Входит:** `api_gateway` (OpenAI-шлюз, tasks, leases, admin, auth, ошибки, заголовки, метрики, маскирование логов), `local_inference` (клиент llama-server, CPU-семафор, EWMA, проба, воркер локальной очереди), `model_router` (правила, детектор секретов, представления состояния облака и ETA), конфиг моделей/клиентов/локальных бэкендов, точка входа `python -m llmhub serve`, `docs/openapi.yaml`, `examples/` (curl, C#/.NET, openai-python), контрактный тест, `docs/integration.md` (раздел API). MCP-сервер `llmhub_ask` — задача T-373 (зависит от T-369).

**Не входит:**
- Реальный облачный `InferenceBackend` (`HttpInferenceBackend`, TLS direct/proxy) — T-370 (поток G по решению координатора); контракт: `run` возвращает тело chat.completion JSON. Облако в T-369 — только Mock (`MockInference`).
- Настоящий поток токенов (token-by-token) — v1 отдаёт SSE «буфером»: keep-alive, пока задача ждёт/идёт, затем ответ чанками (Q5).
- Отмена работающей облачной задачи с разрывом upstream — v1: результат отбрасывается, статус `cancelled`, уже потраченное время GPU оплачено (решение координатора, документируется в `integration.md`).
- MCP-сервер `llmhub_ask` — T-373.
- Бюджеты/квоты/`max_concurrent` клиентов, LLM-советник, `llmhub-client` SDK — этап 5 (ADR-0138 п. 14).
- systemd, деплой, nginx — T-371. Регистрация MCP в `.mcp.json` projectVoiceBot и перевод `tools/llm` на llmhub — после T-373 (llmhub **не импортирует** `tools/llm`).
- Изменение локальных юнитов llama-server — запрещено (CLAUDE.md). В тестах — только фейковый llama-server.

## 2. Ключевые решения плана

1. **Один процесс, один loop, одно соединение.** `Controller.run_forever`, `LocalWorker.run_forever`, проба бэкендов и uvicorn живут в одном asyncio loop; все обработчики FastAPI — `async def` (sync-обработчик ушёл бы в threadpool, а `sqlite3` привязан к потоку). Транзакции короткие, без `await` внутри (правило T-368). `Busy` (БД держит сторож) → `503 busy`, `retryable`, `retry_after_s=1`. Тест проверяет, что все маршруты **и их зависимости** (`dependant.dependencies`, рекурсивно) — корутины. Тяжёлые чистые функции на CPU (`json.loads` тела, `canonical_body`, sha256 тела, `detect_secrets`) идут через `asyncio.to_thread`; метрика `llmhub_loop_lag_s` (задержка тика loop) показывает блокировки.
2. **Всё идёт через очередь**, и локальное тоже: обработчик только пишет задачу (`Controller.submit`) и ждёт её конца (ADR-0138 п. 11, §6.9 «HTTP-обработчики только пишут в очередь и ждут события»). Задачи с `pool IS NULL` (`route='local'` или ядровое `local:fallback`) выполняет новый `LocalWorker`; облачные — ядро как есть. Плюсы: одна идемпотентность, перезапуск, отмена, статусы, `/v1/tasks` для всех путей.
3. **Ожидание**: один общий опросчик `JobWaiter` на процесс — за тик (`api.wait_poll_s`, 0,5 с, через `clock.sleep_until`) один запрос `SELECT … WHERE id IN (…)` по всем ожидаемым задачам, плюс мгновенное пробуждение по `notify(job_id)` от `LocalWorker` и отмены. Ожидающий запрос выходит при `request.is_disconnected()`; **разрыв синхронного HTTP не отменяет задачу** — результат остаётся доступен по `task_id` (решение координатора). Ядро не трогаем ради событий; опрос работает и на `FakeClock` (тест «8 минут» на ускоренных часах).
4. **Канонизация тела**: в очередь пишется JSON с сортированными ключами без `stream` и `stream_options`, плюс служебный объект `"_llmhub": {data_class, policy, priority}` (эффективные значения); поэтому неявная дедупликация `sha256(client+тело)` одинакова для потокового и обычного повтора, но различает запросы с разными `data_class`/политикой/приоритетом. Повтор после `503` присоединяется к той же задаче. `_llmhub` вырезается перед отправкой в бэкенд.
5. **Минимальные правки ядра** (поток G, после них — тесты T-368 по затронутым файлам):
   - `Controller.wake()` — публичный `self._wake.set()` (prewarm-lease, admin-операции);
   - `queue.complete`: при `cancel_requested=1` статус `cancelled` вместо `completed` (стоимость пишется);
   - новый модуль `task_queue/local.py` (локальная очередь, `fail`, просрочка), миграция `0003_api.sql`;
   - `queue.requeue`: при `restarts > MAX_RESTARTS` у задачи с `pool IS NULL` — `fail(local_unavailable)` вместо `fallback_local` (иначе локальная задача крутится вечно).
6. **Локальный воркер — тот же `worker_id = boot_id`**, что у контроллера, и пульс своих задач не реже `timeouts.worker_heartbeat_s`. Иначе `requeue_orphaned_running` в каждом `step()` вернёт локальные задачи в очередь (проверено по `controller.py:229`). Отдельный тест.
7. **Клиенты — декларативно в конфиге** (`clients: {name: {key_sha256, allowed_models, max_priority, default_priority, default_policy, cloud_allowed}}`), при старте синхронизируются в таблицу `clients` (upsert; отсутствующие в конфиге → `disabled_at`). В БД и в конфиге только `sha256(key)`. Ключ выдаёт CLI `python -m llmhub keys new` (печатает ключ один раз и хеш). Админ-ключ — отдельный хеш `api.admin_key_sha256`; если он не задан (`None`), `/admin/*` и `/metrics` отвечают 403. **Перечитывание клиентов без перезапуска**: `SIGHUP` и `POST /admin/reload_clients` перечитывают раздел `clients` файла конфига, валидируют и делают `ClientRegistry.sync()`; ошибка валидации — старый набор остаётся, ответ 400 / запись в лог.
8. **Ответ облака/Mock — тело chat.completion JSON**; `MockInference` возвращает `b"ok"`, поэтому `results.to_chat_completion` оборачивает не-JSON тело в `choices[0].message.content` (только для Mock).
9. **Ошибка недоступности облака** известна либо до постановки (router видит блок провайдера, открытый breaker, HALTED, ступень бюджета), либо после (ядро сделало `fallback_local` с `fallback_reason`). Если у псевдонима нет локальной модели, `LocalWorker` завершает такую задачу `failed` с `error_code='cloud_unavailable'`, а API отдаёт `503 cloud_unavailable` + `reason=fallback_reason`.
10. **Конфиденциальность выше доступности**: тело с эффективным `data_class=private` или с `secret_detected` **никогда** не уходит в облако — ни правилом политики, ни запасным путём F2, ни ядровым перезапуском (у таких задач `pool IS NULL`). Нарушение = blocker ревью; отрицательные тесты R2/W4/I2.
11. **Эффективный `data_class` = клиент ∧ заголовок `X-LLMHub-Data-Class` ∧ поле `llmhub.data_class`**: каждый источник может только сузить до `private`, расширить нельзя.
12. **Просрочка**: задачи в очереди после `deadline_at` → `failed/deadline_exceeded`; после `created_at + max_queue_wait_s` (если задан) → `failed/queue_wait_exceeded`. Проверка — в `LocalWorker.step()` для локальных и для облачных (отдельный проход `expire_overdue` по всем `queued`; ядро облачные просрочки не завершает).

## 3. Файлы и интерфейсы

Новые пакеты наполняют пустые `api_gateway/`, `local_inference/`, `model_router/`. Экспорты в `__init__.py` — ленивые, как в T-368 (PEP 562), или пустые; правит их только поток I.

### 3.1 Конфиг (`configuration/models.py`, поток G)
```python
Policy = Literal["ECONOMY", "BALANCED", "QUALITY"]

class LocalBackendConfig(_Model):
    url: str                        # base URL с /v1, например "http://127.0.0.1:8091/v1"
    model: str                      # имя модели для llama-server (поле "model")
    ctx_tokens: int = Field(ge=1024)
    pp_tps: float = Field(gt=0)     # стартовые значения EWMA (T-365)
    tg_tps: float = Field(gt=0)
    timeout_s: float = 3600
    api_key_env: str | None = None  # имя переменной окружения с ключом llama-server; значение не в конфиге
    probe: Literal["slots", "health"] = "slots"

class ModelAliasConfig(_Model):
    local: str | None = None        # имя из local_backends; None = только облако
    cloud_ctx_tokens: int = 65536   # контекст облачного профиля (пул берётся из pools[*].models)
    quality_tier: int = 0
    description: str = ""

class TaskTypeConfig(_Model):
    max_tokens: int = Field(ge=1)
    think: bool | None = None       # None = не трогать chat_template_kwargs

class RouterConfig(_Model):
    default_policy: Policy = "BALANCED"
    auto_alias: str | None = None              # во что раскрывается "auto" (ECONOMY/BALANCED)
    economy_local_max_eta_s: float = 3600
    balanced_local_max_eta_s: float = 600
    bytes_per_token: float = 3.5
    extra_secret_patterns: list[str] = []      # доп. regex детектора

class LocalConfig(_Model):
    concurrency: int = Field(default=1, ge=1, le=1)   # один CPU-сервер (ADR п. 2)
    probe_interval_s: float = 30
    probe_timeout_s: float = 3
    ewma_alpha: float = Field(default=0.3, gt=0, le=1)
    external_busy_penalty_s: float = 120       # слот занят не нами (8090 вызывают напрямую)
    unavailable_after_failures: int = 3
    poll_s: float = 2.0
    max_attempts: int = 2

class ClientConfig(_Model):
    key_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    allowed_models: list[str]                  # псевдонимы или ["*"]; [] = ничего
    max_priority: Priority = Priority.NORMAL
    default_priority: Priority = Priority.NORMAL
    default_policy: Policy = "BALANCED"
    cloud_allowed: bool = False                # data_class по умолчанию private

class ApiConfig(_Model):
    host: str = "127.0.0.1"                    # прод: 10.8.0.1 (T-371)
    port: int = 8095
    admin_key_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    sync_wait_s: float = Field(default=480, gt=0, le=480)
    wait_poll_s: float = 0.5
    keepalive_s: float = 15
    max_body_bytes: int = 4_000_000
    retry_after_min_s: int = 5
    retry_after_max_s: int = 900

class Config(_Model):  # + поля
    local_backends: dict[str, LocalBackendConfig] = {}
    models: dict[str, ModelAliasConfig] = {}
    task_types: dict[str, TaskTypeConfig] = {}  # task_type_max_tokens остаётся (ядро)
    router: RouterConfig = RouterConfig()
    local: LocalConfig = LocalConfig()
    clients: dict[str, ClientConfig] = {}
    api: ApiConfig = ApiConfig()
```
Валидаторы (`model_validator(mode="after")`): `models[*].local` ∈ `local_backends`; каждый псевдоним из `pools[*].models` есть в `models`; `router.auto_alias` ∈ `models`; `"auto"` не может быть именем модели; `clients[*].allowed_models` ⊆ `models ∪ {"*"}`; `default_priority ≤ max_priority`; regex из `extra_secret_patterns` компилируются; `key_sha256` уникальны и ≠ `admin_key_sha256`. В коде нет имён моделей.

### 3.2 Миграция `persistence/migrations/0003_api.sql` (поток G)
```sql
ALTER TABLE jobs ADD COLUMN backend TEXT;        -- 'local:<name>' или 'cloud:<pool>' — для X-LLMHub-Backend
ALTER TABLE jobs ADD COLUMN warnings TEXT;       -- 'secret_detected', 'economy_stage' (через запятую)
CREATE INDEX jobs_local_queue ON jobs(status, prio_rank, created_at) WHERE pool IS NULL;
CREATE INDEX clients_key ON clients(key_hash);
```
`est_cost_usd` уже есть; пишет его `Submitter` отдельным `UPDATE` в той же транзакции после `submit` (или через расширение `JobSpec.est_cost_usd: float | None = None` — выбрать второе, поле в конце dataclass, `queue.submit` пишет его; поток G).

### 3.3 `task_queue/local.py` (новый, поток G)
```python
def pick_next_local(conn, now: float) -> sqlite3.Row | None
    # pool IS NULL AND status='queued' AND route LIKE 'local%'; порядок как pick_next: prio_rank, round-robin по клиенту, created_at
def mark_running_local(conn, job_id: str, worker_id: str, backend: str, now: float) -> bool
    # status queued->running, instance_id NULL, backend, attempts+1, started_at, heartbeat
def complete_local(conn, job_id: str, worker_id: str, result: JobRunResult, now: float, res_path: str) -> bool
    # CAS: UPDATE ... WHERE id=? AND status='running' AND worker_id=?; False -> результат отброшен (задачу перехватили/отменили);
    # статус cancelled при cancel_requested=1; cost_usd=0.0; закрывает implicit lease
def requeue_local(conn, job_id: str, now: float, reason: str) -> Literal["queued", "failed"]
    # попытки исчерпаны (attempts >= local.max_attempts) -> fail(<последний код: local_unavailable | upstream_timeout>); fallback_local не вызывается (иначе цикл)
def fail(conn, job_id: str, code: str, now: float, *, reason: str | None = None) -> None
    # status='failed', error_code=code, fallback_reason=COALESCE(reason, fallback_reason), finished_at, закрыть implicit lease
def local_queue_ahead(conn, prio_rank: int, created_at: float) -> list[sqlite3.Row]  # для ETA
def expire_overdue(conn, now: float) -> list[tuple[str, str]]
    # все status='queued' (локальные и облачные): deadline_at <= now -> fail(deadline_exceeded);
    # max_queue_wait_s IS NOT NULL AND created_at + max_queue_wait_s <= now -> fail(queue_wait_exceeded); закрывает implicit lease
```
Правка `queue.complete`: `status = CASE WHEN cancel_requested=1 THEN 'cancelled' ELSE 'completed' END`.

### 3.4 `local_inference/` (поток H)
```python
# client.py
class LocalError(Exception): code: str            # 'local_unavailable' | 'context_too_large' | 'upstream_timeout' | 'bad_response'
@dataclass(frozen=True)
class LocalChatResult:
    body: bytes               # JSON chat.completion от llama-server, "model" не меняется здесь
    tokens_in: int; tokens_out: int; finish_reason: str
    prompt_tps: float | None; gen_tps: float | None; latency_s: float
@dataclass(frozen=True)
class ProbeResult:
    ok: bool; busy_slots: int; total_slots: int | None; status: int | None
class LocalBackendClient:
    def __init__(self, name: str, cfg: LocalBackendConfig, api_key: str | None, *,
                 http: httpx.AsyncClient | None = None, clock: Clock) -> None
    async def chat(self, request: dict[str, Any], *, timeout_s: float) -> LocalChatResult
        # POST {url}/chat/completions, stream=false, model=cfg.model; 400 с "context"/"exceed" -> context_too_large;
        # 5xx/транспорт -> local_unavailable; таймаут -> upstream_timeout. Ключ только в заголовке; repr без ключа.
    async def probe(self) -> ProbeResult
        # probe="slots": GET {root}/slots -> is_processing; 404/501 -> переход на /health навсегда; /health 200/503
# stats.py
class Ewma: def __init__(self, alpha: float, initial: float); def update(self, x: float) -> float; value: float
@dataclass
class BackendStats:
    pp: Ewma; tg: Ewma; available: bool = True; failures: int = 0
    busy_external: bool = False; last_probe_at: float | None = None
    def on_result(self, r: LocalChatResult) -> None   # обновляет EWMA по timings, иначе по latency
    def on_probe(self, p: ProbeResult, ours_running: bool, unavailable_after: int) -> None
# eta.py (чистые функции)
def work_s(tokens_in: int, max_tokens: int, pp: float, tg: float) -> float
def local_eta_s(*, ahead: Sequence[tuple[int, int]], running_left_s: float, own: tuple[int, int],
                stats: BackendStats, cfg: LocalConfig) -> float
    # running_left + Σ work(ahead) + work(own) + (external_busy_penalty_s если busy_external)
# worker.py
class LocalWorker:
    def __init__(self, conn, cfg: Config, clock: Clock, bodies: BodyStore,
                 backends: Mapping[str, LocalBackendClient], boot_id: str,
                 notify: Callable[[str], None] = lambda _id: None) -> None
    stats: dict[str, BackendStats]
    def kick(self) -> None
    async def step(self) -> float            # детерминированный проход: пульс, отмены, раздача (семафор 1); возвращает next_wakeup
    async def probe_all(self) -> None        # раз в probe_interval_s
    async def run_forever(self, stop: asyncio.Event) -> None
    def cancel(self, job_id: str) -> bool    # отменяет asyncio-задачу вызова (разрыв upstream)
    def eta_s(self, alias: str, tokens_in: int, max_tokens: int, prio_rank: int, now: float) -> float | None
        # None: у псевдонима нет локального бэкенда или бэкенд недоступен
    def available(self, backend: str) -> bool
```
Выполнение задачи: тело из `BodyStore` → убрать `_llmhub`, добавить `model`, `chat_template_kwargs.enable_thinking` по `task_types[t].think` (если клиент сам не задал) и `max_tokens` (если не задан) → `chat` → `write_result` → `complete_local` (CAS по `worker_id`) → `notify`. Каждый `step()` вызывает `expire_overdue`. Псевдоним без локального бэкенда → `fail('cloud_unavailable', reason=fallback_reason)` (если `route='local:fallback'`) или `fail('local_unavailable')`. Отмена: `cancel_requested=1` у running → `task.cancel()`; `CancelledError` → `_finish_cancel`. Ошибки: `context_too_large` → `fail` сразу; `local_unavailable`/`upstream_timeout` → `requeue_local`.

### 3.5 `model_router/` (поток R)
```python
# secrets.py
def extract_strings(req: dict[str, Any]) -> Iterator[str]
    # все строки запроса: messages[*].content (строка и content-parts text), tool_calls[*].function.arguments,
    # сообщения role=tool, name; tools[*] (description, parameters); response_format (схема); system
def detect_secrets(req: dict[str, Any], extra: Sequence[str] = ()) -> list[str]   # имена сработавших шаблонов, без совпавшего текста
    # PEM/OpenSSH/PGP блоки: "-----BEGIN [A-Z ]*PRIVATE KEY-----", "-----BEGIN OPENSSH PRIVATE KEY-----", "-----BEGIN PGP PRIVATE KEY BLOCK-----";
    # AKIA[0-9A-Z]{16}; sk-[A-Za-z0-9_-]{20,}; ghp_/gho_/github_pat_; xox[abpr]-; AIza[0-9A-Za-z_-]{35}; hf_[A-Za-z0-9]{30,}; rpa_;
    # Bearer [A-Za-z0-9._~+/=-]{20,}; JWT eyJ[\w-]{10,}\.eyJ[\w-]{10,}\.[\w-]{10,};
    # учётные данные в URI [a-z][a-z0-9+.-]*://[^/\s:@]+:[^/\s@]+@;
    # имя ключа (JSON, env, YAML): [\w-]*(pass|secret|token|api[_-]?key)[\w-]*["']?\s*[:=] с непустым значением
    # Все шаблоны без вложенных квантификаторов (без катастрофического backtracking); вызов — через asyncio.to_thread
# rules.py
@dataclass(frozen=True)
class RouteRequest:
    alias: str                      # уже раскрыт из "auto"
    task_type: str | None
    priority: Priority
    policy: Policy                  # с учётом ступени бюджета (ECONOMY при ≥ 80 % для normal/low)
    deadline_in_s: float | None
    data_class: Literal["private", "cloud_allowed"]   # эффективный: клиент ∧ заголовок
    secret_hits: tuple[str, ...]
    tokens_in: int
    max_tokens: int
@dataclass(frozen=True)
class LocalView:
    backend: str | None; ctx_tokens: int | None; eta_s: float | None; available: bool
@dataclass(frozen=True)
class CloudView:
    pool: str | None; ctx_tokens: int; warm: bool; eta_s: float     # 0/работа при warm, иначе t_cold_p90 + работа
    unavailable: UnavailableReason | Literal["controller_halted"] | None
    est_cost_usd: float
@dataclass(frozen=True)
class RouteDecision:
    route: Literal["local", "cloud"]; pool: str | None; backend: str | None
    reason: str                       # 'private' | 'secret_detected' | 'ctx_local_too_small' | 'economy_local' | 'balanced_warm' ...
    fallback: str | None              # причина недоступности облака, если ушли локально вместо облака
    warnings: tuple[str, ...]; est_cost_usd: float | None
class RouteError(Exception):
    def __init__(self, code: ErrorCode, *, reason: str | None = None, retry_after_s: float | None = None) -> None
def resolve_alias(requested: str, policy: Policy, cfg: Config, allowed: Callable[[str], bool]) -> str
def decide(req: RouteRequest, local: LocalView, cloud: CloudView, cfg: RouterConfig) -> RouteDecision
# views.py
class CloudState:   # кеш 5 с: ступень бюджета, halted, kill-файл, блоки провайдеров
    def __init__(self, conn, cfg: Config, clock: Clock) -> None
    def view(self, alias: str, tokens_in: int, max_tokens: int, priority: Priority) -> CloudView
def retry_after_s(conn, cfg: Config, job: sqlite3.Row, worker: LocalWorker, now: float) -> int   # ETA, зажатое в [min,max]
```
**Таблица правил `decide`** (каждая строка — отдельный параметризованный тест `test_r_rules.py`):

| # | Условие (по порядку) | Итог |
|---|---|---|
| H1 | `data_class=private` | локально (`reason=private`); нет локальной → `RouteError(forbidden, reason='private_requires_local')`; контекст больше локального → `context_too_large` (400), не облако |
| H2 | `secret_hits` непусто | локально + предупреждение `secret_detected`; нет локальной → `forbidden`, `reason='secret_detected'`; контекст больше локального → `context_too_large` |
| H3 | `tokens_in + max_tokens > local.ctx` и облако есть | только облако (`ctx_local_too_small`); облако недоступно → `503 cloud_unavailable` с `reason` |
| H4 | контекст больше и локального, и облачного | `context_too_large` (400) |
| H5 | нет ни локальной, ни облачной модели | `model_not_available` (404) |
| P1 | ECONOMY: локально, если `local.eta ≤ min(deadline_in_s, economy_local_max_eta_s)` | иначе облако |
| P2 | BALANCED: облако тёплое (READY/IDLE/BUSY) | облако (`balanced_warm`) |
| P3 | BALANCED: `local.eta ≤ min(deadline_in_s, balanced_local_max_eta_s)` | локально, иначе облако |
| P4 | QUALITY: облако доступно | облако; иначе локально с `fallback` |
| P5 | `urgent` при ступени URGENT_ONLY и выше — облако разрешено; normal/low при URGENT_ONLY — облако считается недоступным (`budget_limit_reached`) | |
| F1 | выбрано облако, но `cloud.unavailable` | локально с `fallback=<reason>`; локально невозможно (нет модели, недоступна, H3) → `503 cloud_unavailable` + `reason` |
| F2 | выбрано локально, локальный бэкенд недоступен | облако **только** если `data_class=cloud_allowed` **и** нет `secret_hits` (H1/H2 сюда не доходят) и облако доступно (`fallback=local_unavailable`); иначе `503 local_unavailable`. Отрицательный тест: private и секрет при недоступном локальном → 503, задача в облако не ставится |
| F3 | NO_LAUNCH (100 %): облако доступно только тёплое | иначе `budget_limit_reached` |

Облако «недоступно» до постановки: HALTED/KILL → `controller_halted`; `provider_accounts.blocked_reason` → его причина (`insufficient_balance`, `quota_exceeded`, …); `circuit_state='open'` → `provider_api_error` (`retry_after_s` = остаток `breaker_open_s`); ступень бюджета → `budget_limit_reached`; пул без адаптера → `provider_api_error`. `gpu_unavailable` и `deployment_error` появляются только после (ядро, `fallback_reason`).

### 3.6 `api_gateway/` (поток W)
```python
# errors.py
class ErrorCode(StrEnum): UNAUTHORIZED, FORBIDDEN, INVALID_REQUEST, REQUEST_TOO_LARGE, CONTEXT_TOO_LARGE,
    MODEL_NOT_AVAILABLE, NOT_FOUND, IDEMPOTENCY_CONFLICT, TASK_NOT_READY, LEASE_EXPIRED, LEASE_CLOSED,
    QUEUE_WAIT_EXCEEDED, DEADLINE_EXCEEDED, CONTROLLER_HALTED, UPSTREAM_TIMEOUT, CANCELLED,
    LOCAL_UNAVAILABLE, CLOUD_UNAVAILABLE, BUSY, CONFLICT, INTERNAL_ERROR
SPEC: dict[ErrorCode, tuple[int, bool, str]]   # http, retryable, OpenAI "type"
class ApiError(Exception):
    def __init__(self, code: ErrorCode, message: str, *, reason: str | None = None,
                 retry_after_s: float | None = None, task_id: str | None = None,
                 fallback: str | None = None, headers: Mapping[str, str] | None = None) -> None
    def body(self) -> dict[str, Any]   # {"error": {type, code, message, param: None, reason, retryable, retry_after_s, task_id, fallback}}
def install_handlers(app: FastAPI) -> None  # ApiError, RequestValidationError -> invalid_request, 404/405, Busy -> busy, прочее -> internal_error без текста
# auth.py
def hash_key(key: str) -> str; def generate_key() -> str            # "lh_" + token_urlsafe(32)
@dataclass(frozen=True)
class Principal:
    client_id: str; allowed_models: frozenset[str]; max_priority: Priority; default_priority: Priority
    default_policy: Policy; cloud_allowed: bool
    def may_use(self, alias: str) -> bool
class ClientRegistry:
    def __init__(self, conn, cfg: Config, clock: Clock) -> None
    def sync(self) -> None                     # конфиг -> таблица clients (upsert, disable отсутствующих)
    def reload(self, path: Path) -> int        # перечитать clients из файла, валидировать, sync; SIGHUP и /admin/reload_clients
    def by_key(self, key: str) -> Principal | None   # sha256 -> dict; hmac.compare_digest
async def require_client(request: Request) -> Principal       # FastAPI dependency (async!); 401 без/с неверным ключом
async def require_admin(request: Request) -> None             # 401/403; ключ клиента на /admin -> 403; admin_key_sha256=None -> 403
# options.py
@dataclass(frozen=True)
class RequestOptions:
    priority: Priority; policy: Policy; deadline_in_s: float | None; wait_s: float; task_type: str | None
    data_class: Literal["private", "cloud_allowed"]; lease_id: str | None; idem_key: str | None
def parse_options(headers: Mapping[str, str], principal: Principal, cfg: Config,
                  overrides: Mapping[str, Any] | None = None) -> RequestOptions
    # X-LLMHub-Priority (> max_priority -> 403 forbidden, reason=priority_not_allowed), X-LLMHub-Policy,
    # X-LLMHub-Deadline (секунды от now), X-LLMHub-Wait (1..sync_wait_s, иначе 400), X-LLMHub-Task-Type (∈ task_types),
    # X-LLMHub-Data-Class и поле llmhub.data_class (только сужают до private; эффективный = клиент ∧ заголовок ∧ поле), X-LLMHub-Lease, Idempotency-Key (1..255 печатных ASCII)
# submission.py
def canonical_body(req: dict[str, Any]) -> bytes        # без stream/stream_options, sort_keys, separators=(",", ":")
def estimate_tokens_in(req: dict[str, Any], bytes_per_token: float) -> int
@dataclass(frozen=True)
class Submitted: job_id: str; created: bool; decision: RouteDecision | None   # None у дубликата
class Submitter:
    def __init__(self, rt: "Runtime") -> None
    def submit(self, principal: Principal, req: dict[str, Any], opts: RequestOptions) -> Submitted
        # alias -> resolve_alias; may_use -> 403/404; секреты; CloudState.view; LocalWorker.eta_s; decide; JobSpec;
        # Controller.submit (IdempotencyConflict -> 409 c task_id); worker.kick(); lease_id проверяется (свой, живой)
# waiter.py
class JobWaiter:   # один на процесс; фоновая задача-опросчик, один SELECT ... WHERE id IN (...) за тик
    def __init__(self, conn, clock: Clock, poll_s: float) -> None
    def notify(self, job_id: str) -> None
    async def wait(self, job_id: str, until: float, disconnected: Callable[[], Awaitable[bool]] | None = None) -> sqlite3.Row | None
        # терминальная строка; текущая по истечении until; None при разрыве клиента (задача НЕ отменяется)
    async def run(self, stop: asyncio.Event) -> None
# results.py
TERMINAL = {"completed", "failed", "cancelled"}
def to_chat_completion(job: sqlite3.Row, body: bytes) -> dict[str, Any]   # id "chatcmpl-<job_id>", model = псевдоним
def to_sse_chunks(completion: dict[str, Any], include_usage: bool) -> list[str]  # "data: {...}\n\n" ... "data: [DONE]\n\n"
    # delta: role, content, reasoning_content, tool_calls (с index, id, type, function.name/arguments); finish_reason; usage
def response_headers(job: sqlite3.Row) -> dict[str, str]
    # X-LLMHub-Task, X-LLMHub-Backend, X-LLMHub-Fallback (если fallback_reason), X-LLMHub-Truncated: 1 (finish_reason=length),
    # X-LLMHub-Cost-Estimate (est_cost_usd), X-LLMHub-Warning (warnings)
def job_error(job: sqlite3.Row) -> ApiError     # failed/cancelled -> код/причина
# routes_openai.py, routes_tasks.py, routes_leases.py, routes_admin.py, routes_health.py, metrics.py — см. §4
# logging.py
class SecretMaskFilter(logging.Filter)   # маскирует "Bearer …", "api_key=…", "lh_…", Authorization; тела запросов не логируются
def configure_logging(level: str = "INFO") -> None
# app.py
@dataclass
class Runtime:
    conn: sqlite3.Connection; cfg: Config; clock: Clock; controller: Controller; worker: LocalWorker
    bodies: BodyStore; registry: ClientRegistry; waiter: JobWaiter; cloud: CloudState
    started_at: float; version: str
def create_app(rt: Runtime, *, run_loops: bool = True) -> FastAPI
    # lifespan: run_loops=True -> Controller.run_forever + LocalWorker.run_forever + проба; тесты на FakeClock -> False
```

### 3.7 Точка входа (поток I)
- `src/llmhub/server.py`: `def build_runtime(cfg: Config, env: Mapping[str, str], clock: Clock) -> Runtime` (open_state_db → migrate → `ClientRegistry.sync` → адаптеры по `providers.enabled` — пока только `mock` + `MockInference` → `LocalBackendClient` на каждый `local_backends` с ключом из `env[api_key_env]` → `Controller`, `LocalWorker(notify=waiter.notify)`); `async def serve(rt) -> None` (uvicorn.Server на `api.host:api.port`, логирование с маской).
- `src/llmhub/__main__.py`: `python -m llmhub serve --config PATH` и `python -m llmhub keys new [--name N]` (ключ печатается один раз в stdout, хеш — строкой для конфига).

### 3.8 MCP-сервер — вынесен в T-373
По решению ворот MCP `llmhub_ask` — отдельная задача `tasks/active/T-373-mcp-server.md` (зависит от T-369); находки critic по MCP — в её критериях. T-369 обеспечивает только HTTP-контракт, на который MCP опирается (`/v1/tasks`, коды ошибок, `X-LLMHub-Data-Class`).

### 3.9 Документы и примеры (поток O)
- `docs/openapi.yaml` — генерируется `scripts/export_openapi.py` из `create_app(...).openapi()` (с `responses=` для ошибок, примерами, описаниями заголовков `X-LLMHub-*`), дальше коммитится; тест сравнивает файл со свежей генерацией (расхождение = падение).
- `examples/curl/`: `01_models.sh`, `02_chat.sh`, `03_chat_stream.sh`, `04_task_submit_poll_result.sh`, `05_task_cancel.sh`, `06_lease_lifecycle.sh`, `07_idempotency.sh`, `08_errors.sh`, `09_admin_status.sh` — каждый использует только `$LLMHUB_URL`, `$LLMHUB_KEY` (`$LLMHUB_ADMIN_KEY` для admin), `curl -sS --fail-with-body` и `jq`-free проверки (`grep`), завершается кодом 0 при ожидаемом ответе.
- `examples/csharp/LlmHubExample/` (`.csproj` + `Program.cs`, HttpClient: chat, task submit/poll/result, lease с пульсом, разбор `error.code/reason/retry_after_s`, `Timeout > 8 мин`), `examples/python/openai_chat.py` (openai-python, `base_url`, `timeout=600`, `max_retries` и Retry-After), `examples/python/raw_tasks.py` (httpx).
- `docs/integration.md` — разделы 2–6 переписываются под фактический контракт (коды ошибок, заголовки, примеры). Явно: в v1 при `stream` первый чанк с текстом приходит только в конце (до этого — keep-alive); разрыв синхронного запроса задачу не отменяет; отмена облачной задачи в работе — потраченное время GPU оплачено.

## 4. Эндпоинты и схемы

Все `/v1/*` — `Authorization: Bearer <ключ клиента>`; `/admin/*` и `/metrics` — админ-ключ (не задан → 403); `/healthz`, `/readyz` — без ключа (ничего чувствительного). Тело > `api.max_body_bytes` → 413 `request_too_large`. Все ответы JSON, кроме SSE и `/metrics`.

| Метод, путь | Запрос | Ответ | Ошибки |
|---|---|---|---|
| `POST /v1/chat/completions` | OpenAI chat body (`model`, `messages`, `stream`, `stream_options.include_usage`, `max_tokens`/`max_completion_tokens`, `response_format`, `tools`, прочие поля проходят как есть); заголовки `Idempotency-Key`, `X-LLMHub-*` | 200 `chat.completion` + заголовки ответа; при `stream` — SSE: сразу заголовки и `: keep-alive` каждые 15 с, по готовности — чанки `chat.completion.chunk` и `[DONE]` | 400, 401, 403, 404, 409 (`idempotency_conflict`, `task_id`), 413, 503 `queue_wait_exceeded` + `Retry-After` + `task_id` (задача остаётся), 503 `cloud_unavailable`/`local_unavailable`/`controller_halted`/`busy`, 504 `deadline_exceeded`/`upstream_timeout`. В SSE после отправки заголовков ошибка приходит событием `data: {"error": {...}}` без `[DONE]` (openai-python поднимает `APIError`) |
| `GET /v1/models` | — | `{"object":"list","data":[{"id","object":"model","created","owned_by":"llmhub","llmhub":{"local":bool,"cloud":bool,"ctx_tokens"}}]}` — только разрешённые клиенту + `auto` | 401 |
| `POST /v1/tasks` | chat body + необязательный объект `"llmhub": {priority, policy, deadline_s, task_type, data_class}` (заголовки тоже работают; поле главнее) | 202 `{"id","object":"llmhub.task","status","route","backend","created_at"}`, заголовок `Location`; дубликат → 200 с тем же `id` | 400, 401, 403, 404, 409, 413, 503 `cloud_unavailable` (решение до постановки) |
| `GET /v1/tasks/{id}` | — | `{"id","status","route","backend","pool","priority","policy","created_at","started_at","finished_at","eta_s","est_cost_usd","cost_usd","finish_reason","usage","fallback","warnings","error"}` | 401, 404 (чужая = несуществующая) |
| `GET /v1/tasks/{id}/result` | — | 200 `chat.completion` + заголовки; `failed`/`cancelled` → объект ошибки с кодом задачи | 409 `task_not_ready` (`retry_after_s`), 404 |
| `POST /v1/tasks/{id}/cancel` | — | 200 `{"id","status"}`: queued → `cancelled`; running local → разрыв upstream → `cancelled`; running cloud → `cancel_requested`, итог `cancelled` (потраченное время GPU оплачено); терминальная — без изменений | 404 |
| `POST /v1/leases` | `{"model": alias, "ttl_s"?: 30..3600, "prewarm"?: bool}` | 201 `{"id","pool","kind","ttl_s","expires_at"}` | 403 (клиент без `cloud_allowed`, модель вне `allowed_models`), 404 (у модели нет облачного пула), 400 (ttl) |
| `POST /v1/leases/{id}/renew` | — | 200 `{"id","expires_at"}` | 410 `lease_expired`, 409 `lease_closed`, 404 |
| `POST /v1/leases/{id}/close` | `{"final"?: bool}` | 200 `{"id","closed":true}` (идемпотентно) | 404 |
| `GET /admin/status` | — | halted/halt_reason/safe_mode/kill_file; пулы (состояние, инстанс, очередь по приоритетам, leases); локальные бэкенды (available, busy_external, EWMA, очередь); бюджет (ступень, месяц/день, предупреждения); провайдеры (blocked_reason, circuit, баланс); 20 открытых инцидентов | 401/403 |
| `GET /admin/budget` | — | ступень, `committed`/лимиты месяц/день, предупреждения, ledger за месяц | |
| `GET /admin/clients` | — | список клиентов без хешей: name, allowed_models, max_priority, cloud_allowed, disabled | |
| `POST /admin/reload_clients` | — | 200 `{"clients": N}` (то же делает `SIGHUP`) | 400 `invalid_request` (конфиг не прошёл валидацию, старый набор остаётся) |
| `POST /admin/hold` | `{"pool"|"model", "ttl_s" ≤ 28800}` | 201 `{"lease_id"}` | 409 `conflict` (`HoldRejected`: reason из `Gate`, `HoldExceedsSessionBudget`), 400 (ttl) |
| `POST /admin/release` | `{"pool"|"model"}` | 200 | |
| `POST /admin/kill` | `{"reason"?}` | 200 `KillReport` (≤ 60 с) | |
| `POST /admin/resume` | `{"provider"?}` | 200 | 409 `conflict` (`reason=kill_file_present` / `reconcile_required`) |
| `POST /admin/reload_billing` | — | 200 | 503 `busy` |
| `GET /healthz` | — | `{"status":"ok","version","halted","uptime_s"}` (не зависит от LLM) | |
| `GET /readyz` | — | 200, если loop контроллера жив (heartbeat ≤ 60 с) и БД открыта; иначе 503 | |
| `GET /metrics` | — | Prometheus text: `llmhub_jobs{status,route}`, `llmhub_queue_depth{pool,priority}`, `llmhub_local_queue_depth`, `llmhub_instances{pool,state}`, `llmhub_budget_committed_usd{window}`, `llmhub_budget_stage`, `llmhub_halted`, `llmhub_local_ewma_tps{backend,kind}`, `llmhub_local_available{backend}`, `llmhub_http_requests_total{path,code}`, `llmhub_loop_lag_s`, `llmhub_waiters` | 401/403 (без `admin_key_sha256` — всегда 403) |

**Коды ошибок** (`errors.SPEC`; каждая строка — тест `test_w_errors.py`):

| `code` | HTTP | `retryable` | Когда |
|---|---|---|---|
| `unauthorized` | 401 | нет | нет/неверный ключ |
| `forbidden` | 403 | нет | модель вне `allowed_models`, priority > `max_priority`, ключ клиента на `/admin`, private/секрет без локальной модели, lease без `cloud_allowed` |
| `invalid_request` | 400 | нет | валидация тела/заголовков |
| `request_too_large` | 413 | нет | тело > лимита |
| `context_too_large` | 400 | нет | H4 или 400 llama-server по контексту |
| `model_not_available` | 404 | нет | неизвестный псевдоним, H5 |
| `not_found` | 404 | нет | задача/lease не найдены или чужие |
| `idempotency_conflict` | 409 | нет | тот же ключ, другое тело (`task_id` существующей) |
| `task_not_ready` | 409 | да | результат ещё не готов |
| `lease_expired` | 410 | нет | `LeaseExpired` |
| `lease_closed` | 409 | нет | `LeaseClosed` |
| `conflict` | 409 | нет | admin: hold отклонён, resume запрещён |
| `queue_wait_exceeded` | 503 | да | синхронное окно истекло (`Retry-After`, `task_id`, задача остаётся) или задача просрочила `max_queue_wait_s` (`failed`) |
| `cloud_unavailable` | 503 | по причине: `provider_api_error`, `gpu_unavailable`, `deployment_error` — да; `insufficient_balance`, `quota_exceeded`, `budget_limit_reached` — нет | облако недоступно, локально нельзя |
| `local_unavailable` | 503 | да | локальный бэкенд недоступен/попытки исчерпаны |
| `controller_halted` | 503 | нет | HALTED/KILL и локально нельзя |
| `busy` | 503 | да (1 с) | `persistence.db.Busy` |
| `deadline_exceeded` | 504 | нет | дедлайн прошёл до старта (`expire_overdue`) |
| `upstream_timeout` | 504 | да | таймаут llama-server после попыток |
| `cancelled` | 409 | нет | результат отменённой задачи |
| `internal_error` | 500 | да | прочее; сообщение без деталей |

Сообщения ошибок не содержат текста запроса, ключей и путей файлов.

## 5. Шаги (TDD) и потоки

Команда каждого шага: `uv run pytest tests/<файлы шага> -q && uv run ruff check src tests && uv run pyright`. Тесты плоско в `tests/`, префикс потока. Фикстуры потоков — `tests/<поток>_fixtures.py` через `pytest_plugins`. Общие фикстуры T-368 (`harness.py`) не меняются.

### Поток G — фундамент и правки ядра (sonnet; первым, ~0,5 дня). Владеет: `configuration/models.py`, `configuration/loader.py`, `persistence/migrations/0003_api.sql`, `task_queue/local.py`, `task_queue/models.py` (поле `est_cost_usd`), правка `task_queue/queue.py` (`complete`, `requeue` при `restarts` для `pool IS NULL`, запись `est_cost_usd`), `controller.py` (только `wake()`), `pyproject.toml`, `tests/test_g_*.py`, `tests/fixtures/config_api.yaml`
| Шаг | Сначала тест | Реализация |
|---|---|---|
| G1 [x] | `test_g_config.py`: умолчания §3.1; каждый валидатор отвергает (local вне backends, alias пула не в `models`, `auto` как имя, клиент с неизвестной моделью, `default_priority > max_priority`, плохой regex, дубль хеша, хеш = админскому, `sync_wait_s` 481); пример `tests/fixtures/config_api.yaml` грузится | `configuration/models.py`, `loader.py` |
| G2 [x] | `test_g_migration.py`: `migrate` → `user_version=3`, колонки `backend`, `warnings`, индексы; повтор — noop; БД T-368 (v2) мигрирует без потерь | `0003_api.sql` |
| G3 [x] | `test_g_local_queue.py`: `pick_next_local` берёт только `pool IS NULL`, порядок приоритет → round-robin → время; `mark_running_local` CAS (второй вызов False); `complete_local` CAS: чужой `worker_id` или не running → False, строка не меняется; `requeue_local` до `max_attempts` → `queued`, затем `failed` с последним кодом (`local_unavailable`/`upstream_timeout`); `fail` закрывает implicit lease (если был) и пишет `fallback_reason`; `local_queue_ahead` | `task_queue/local.py` |
| G3a [x] | `test_g_expire.py`: `expire_overdue` — локальная и облачная queued после `deadline_at` → `failed/deadline_exceeded`, после `max_queue_wait_s` → `failed/queue_wait_exceeded`, implicit lease закрыт (облачный пул без задач не держится); running и терминальные не трогаются; до срока — без изменений | `task_queue/local.py` |
| G4 [x] | `test_g_core_hooks.py`: `complete` при `cancel_requested=1` → `cancelled`, `cost_usd` записан, lease закрыт; `Controller.wake()` будит `run_forever` (FakeClock); `JobSpec.est_cost_usd` пишется в `jobs`; `requeue` локальной задачи с `restarts > 5` → `failed/local_unavailable` (облачная — по-прежнему `fallback_local`). Затем регрессия ядра: `uv run pytest tests/test_d_*.py tests/test_f_tz15.py tests/test_s0_skeleton.py -q` | `queue.py`, `models.py`, `controller.py` |
| G5 [x] | — (без теста) `pyproject.toml`: dev-группа `+ openai`; `uv sync` проходит (зависимости MCP — в T-373) | `pyproject.toml` |

### Поток H — локальный инференс (sonnet). Владеет: `local_inference/*` (кроме `__init__.py`), `tests/fake_llama.py`, `tests/h_fixtures.py`, `tests/test_h_*.py`. Зависит от G1, G3, G3a
| Шаг | Сначала тест | Реализация |
|---|---|---|
| H1 | `tests/fake_llama.py` — ASGI-приложение: `/v1/chat/completions` (ответ с `usage`, `timings`, `finish_reason` stop/length), `/health` (200/503), `/slots` (`is_processing`, 501 при выключенных), проверка `Authorization`, ручки: задержка (через переданный `Clock`), 5xx, обрыв, 400 «context exceeds», занятость внешним клиентом, журнал запросов. `test_h_fake_llama.py` — сам фейк | `tests/fake_llama.py` |
| H2 | `test_h_client.py` (httpx `ASGITransport`): успешный chat — поля `LocalChatResult`; `model` подставлен; 400 context → `context_too_large`; 503/обрыв → `local_unavailable`; таймаут → `upstream_timeout`; ключ только в заголовке, `repr`/исключения без ключа; `probe` slots → busy; slots 501 → переход на `/health` | `client.py` |
| H3 | `test_h_stats_eta.py`: EWMA (α 0,3) по `timings`, без `timings` — по latency; `on_probe`: 3 сбоя → `available=False`, успех → снова True; занятый слот при отсутствии наших задач → `busy_external`; `local_eta_s` — сумма впереди + текущая + своя + штраф; таблица значений | `stats.py`, `eta.py` |
| H4 | `test_h_worker.py` (FakeClock, фейк по ASGI): одна задача → `completed`, результат в `BodyStore`, `backend='local:<name>'`, `notify` вызван; семафор 1 — две задачи на разных бэкендах идут последовательно; приоритет urgent обгоняет low; `enable_thinking` из `task_types` добавлен, если клиент не задал; `max_tokens` по умолчанию | `worker.py` |
| H5 | `test_h_worker_faults.py`: 5xx → `requeue_local` → вторая попытка успешна; 2 сбоя → `failed/local_unavailable`; context → `failed/context_too_large` без повтора; `local:fallback` у псевдонима без локальной модели → `failed/cloud_unavailable` с `fallback_reason`; отмена running → вызов прерван (фейк видит обрыв), статус `cancelled`; **пульс**: задача идёт 10 мин, `Controller.step()` (harness) её не перехватывает (`worker_id=boot_id`, пульс ≤ 30 с); перезапуск (новый boot_id) → running-задача возвращена в очередь и выполнена; поздний ответ старого вызова после перехвата не перезаписывает строку (CAS); `step()` вызывает `expire_overdue` — просроченная локальная задача → `deadline_exceeded`, вызова бэкенда нет | `worker.py` |
| H6 | `test_h_no_real_units.py` (страж): ни одна фикстура/конфиг в `tests/` не указывает на `127.0.0.1:8090–8092`, `10.8.0.1:8090–8092` или `localhost:809[0-2]`; `fake_llama` слушает только свободный порт или ASGI | — |

### Поток R — router (sonnet). Владеет: `model_router/*` (кроме `__init__.py`), `tests/test_r_*.py`. Зависит от G1 (конфиг) и интерфейса `LocalWorker.eta_s` (по сигнатуре §3.4; в тестах — заглушки `LocalView`)
| Шаг | Сначала тест | Реализация |
|---|---|---|
| R1 | `test_r_secrets.py`: каждый шаблон ловится на синтетическом примере (фиктивные ключи, ≥ 1 положительный и 1 отрицательный на шаблон), в т. ч. формы JSON (`"api_key": "…"`), env (`DB_PASSWORD=…`), YAML, JWT, `https://user:pass@host`, OpenSSH/PGP блоки; секрет найден в content-parts, `tool_calls.arguments`, сообщении role=tool, `tools[*].description`, `response_format`; `extra` работает; результат — имена без найденного текста; производительность: 4 МБ текста < 200 мс, патологический ввод (`"a"*10**6`, `"="*10**6`) < 200 мс | `secrets.py` |
| R2 | `test_r_rules.py`: параметризованная таблица §3.5 (H1–H5, P1–P5, F1–F3); **отрицательные**: private/секрет при недоступном локальном и доступном тёплом облаке → никогда `route=cloud` (перебор всех политик и ступеней бюджета); private + контекст > локального → `context_too_large`; пример §6.11 (8000 ток локально 14–16 мин → облако; 1000 ток ~2 мин → локально); детерминизм (одинаковый вход → одинаковый выход) | `rules.py` |
| R3 | `test_r_alias.py`: `auto` → `auto_alias`; QUALITY + `auto` → наибольший `quality_tier` среди разрешённых клиенту с облаком (если `cloud_allowed`), иначе `auto_alias`; неизвестный → `model_not_available`; запрещённый → `forbidden` | `rules.py` |
| R4 | `test_r_views.py` (БД + Mock-сценарии вставками): warm при READY/IDLE/BUSY; `t_cold_p90` при STOPPED; `blocked_reason` → причина; circuit open → `provider_api_error` + `retry_after_s`; HALTED, KILL-файл → `controller_halted`; ступень ECONOMY/URGENT_ONLY/NO_LAUNCH; кеш 5 с; `retry_after_s` зажат в [5, 900] | `views.py` |

### Поток W — шлюз (sonnet; ревью ×2). Владеет: `api_gateway/*` (кроме `__init__.py`), `tests/w_fixtures.py`, `tests/test_w_*.py`. Зависит от G (все), интерфейсов H и R по §3.4–3.5 (до их готовности — заглушки в `w_fixtures.py`)
| Шаг | Сначала тест | Реализация |
|---|---|---|
| W1 | `test_w_errors.py`: каждая строка таблицы §4 → HTTP, `type`, `code`, `retryable`; `RequestValidationError` → `invalid_request` без эха тела; неизвестный путь → `not_found` в формате OpenAI; исключение → 500 без текста; `Busy` → 503 `busy` | `errors.py` |
| W2 | `test_w_auth.py`: `hash_key`/`generate_key`; `sync` upsert/disable; `reload` — новый ключ работает без перезапуска, удалённый клиент → 401, битый конфиг → старый набор остаётся; `SIGHUP` вызывает `reload`; нет заголовка/чужой ключ/отключённый клиент → 401; ключ клиента на `/admin` → 403; админ-ключ на `/v1` → 401; `admin_key_sha256=None` → `/admin/*` и `/metrics` 403; в БД нет открытых ключей (grep по файлу БД) | `auth.py` |
| W3 | `test_w_options.py`: каждый заголовок; priority > max → 403 `priority_not_allowed`; data_class: таблица клиент × заголовок × поле `llmhub.data_class` (8 сочетаний) — `cloud_allowed` только когда все три разрешают/молчат, расширить нельзя; Wait 0/481/«abc» → 400; Task-Type вне конфига → 400; Idempotency-Key длина/символы | `options.py` |
| W4 | `test_w_submission.py`: канонизация (stream/порядок ключей не влияют на sha; разные data_class/policy/priority → разные задачи); `_llmhub` не уходит в бэкенд; оценка токенов; private-клиент → `route=local` всегда; секрет в сообщении → local + warning, у cloud-only псевдонима → 403; private/секрет при недоступном локальном → 503, `jobs` без строки с `pool IS NOT NULL`; конфликт ключа → 409 c `task_id`; дубликат без ключа (≤ 15 мин) → тот же `job_id`; чужой/истёкший `X-LLMHub-Lease` → 404/410 | `submission.py` |
| W5 | `test_w_waiter_results.py`: `wait` возвращается по `notify` без продвижения часов; по опросу; по `until`; при `disconnected` → None, задача не отменена; один SELECT за тик на 100 ожидающих (счётчик запросов через `set_trace_callback`), 100 ожидающих завершаются, `llmhub_waiters` возвращается к 0; `to_chat_completion` — JSON llama-server (model → псевдоним, id) и не-JSON Mock; `to_sse_chunks` (role, content, `reasoning_content`, `tool_calls` с `index`, finish_reason, usage при include_usage, `[DONE]`); заголовки `Backend`, `Fallback`, `Truncated` (finish_reason=length), `Cost-Estimate`, `Warning` | `waiter.py`, `results.py` |
| W6 | `test_w_chat.py` (FakeClock, `create_app(run_loops=False)`, циклы гоняет фикстура): локальный ответ 200; облачный (Mock) 200 c `X-LLMHub-Backend: cloud:p1`; **ожидание 480 с → 503 `queue_wait_exceeded` + `Retry-After` + `task_id`, задача осталась queued; повтор того же запроса присоединяется к задаче и получает результат**; `X-LLMHub-Wait: 30` → 503 через 30 с; stream: keep-alive комментарии по 15 с (часы двигает отдельная asyncio-задача, пока клиент читает поток), затем чанки, `[DONE]`; stream с таймаутом → событие ошибки; разрыв клиента во время ожидания → задача продолжается и позже доступна по `task_id`; `X-LLMHub-Truncated: 1`; повтор с `Idempotency-Key` после завершения → тот же ответ без новой задачи | `routes_openai.py` |
| W7 | `test_w_tasks.py`: submit 202 + `Location`; статус с ETA и маршрутом; result 409 `task_not_ready` до готовности, затем 200; result отменённой → 409 `cancelled`; просроченная (`deadline_s`) → status `failed`, result → 504 `deadline_exceeded`; cancel queued → `cancelled`; cancel running local → `cancelled` и обрыв в фейке; cancel running cloud → итог `cancelled`; чужая задача → 404; `llmhub`-поле главнее заголовка | `routes_tasks.py` |
| W8 | `test_w_leases.py`: create (explicit, prewarm → пул STOPPED запускается через `Controller.wake`), renew, close/final (drain); истёкший renew → 410; закрытый → 409; private-клиент → 403; модель без пула → 404; ttl вне [30, 3600] → 400; чужой lease → 404; **агент с lease молчит 20 мин — GPU жива** (через API) | `routes_leases.py` |
| W9 | `test_w_admin.py`: status (все разделы), budget, clients (без хешей), reload_clients; hold/release (gate отказ → 409), kill → 0 ресурсов Mock ≤ 60 с, HALTED → чат cloud-only → 503 `controller_halted`, локальный → 200 с `X-LLMHub-Fallback: controller_halted`; resume при KILL-файле → 409 `kill_file_present`; resume после порчи без reload_billing → 409 `reconcile_required`; reload_billing → resume проходит | `routes_admin.py` |
| W10 | `test_w_health_metrics.py`: healthz без ключа, без зависимости от LLM (фейк выключен — 200); readyz 503 без пульса контроллера; `/metrics` — админ-ключ, формат Prometheus (парсинг строк), значения после пары задач; `test_w_logging.py`: маска `Bearer`, `api_key=`, `lh_…`; тела запросов не попадают в лог (caplog); `test_w_app.py`: все маршруты и все их зависимости (обход `route.dependant.dependencies` рекурсивно) — `async def`; лимит тела 413; тело 4 МБ: задержка loop (`llmhub_loop_lag_s`) < 100 мс — тяжёлые функции в `to_thread` | `routes_health.py`, `metrics.py`, `logging.py`, `app.py` |

### Поток I — интеграция (opus; после G, H, R, W). Владеет: `src/llmhub/server.py`, `src/llmhub/__main__.py`, `src/llmhub/{api_gateway,local_inference,model_router}/__init__.py`, `tests/i_fixtures.py`, `tests/test_i_*.py`
| Шаг | Сначала тест | Реализация |
|---|---|---|
| I1 | `test_i_runtime.py`: `build_runtime` из `tests/fixtures/config_api.yaml` (mock + 3 фейковых бэкенда): клиенты синхронизированы, ключи llama-server из env (нет переменной → бэкенд без ключа, предупреждение); `python -m llmhub keys new` печатает ключ и его sha256; `SIGHUP` подключён к `ClientRegistry.reload` | `server.py`, `__main__.py` |
| I2 | `test_i_unavailable.py` — **шесть причин** (MockProvider/MockInference/бюджет): для каждой (а) псевдоним с локальной моделью → 200, `X-LLMHub-Backend: local:<b>`, `X-LLMHub-Fallback: <reason>`; (б) cloud-only псевдоним → 503 `cloud_unavailable`, `reason`, `retryable` по таблице §4; (в) private-клиент при каждой причине и недоступном локальном — 503, в облако ничего не поставлено. Источники: `insufficient_balance` (Fault + баланс), `quota_exceeded` (Fault.QUOTA), `provider_api_error` (API_5XX ×3 → breaker), `gpu_unavailable` (`gpu_available=False`), `deployment_error` (`test_fail` > retries, `MAX_FAILED_CYCLES`), `budget_limit_reached` (ledger ≥ budget) | связка |
| I3 | `test_i_e2e.py` (FakeClock, harness + app): 10 параллельных запросов BALANCED cloud-allowed → 1 create; private-клиент при тёплой GPU → всё локально; ступень 80 % → normal идёт ECONOMY; перезапуск процесса посреди локальной и облачной задачи → обе завершаются, клиент по `task_id` получает результат; idempotent-повтор после перезапуска | связка |
| I4 | `test_i_live.py` (`slow`): uvicorn на `127.0.0.1:<свободный порт>` + фейковые llama-server (uvicorn) + SystemClock + Mock с быстрыми ручками; openai-python: chat, stream (в т. ч. ответ с `tool_calls` и `reasoning_content` — SDK собирает дельты), ошибка 403, событие ошибки в SSE → `APIError`. Фикстура живого приложения в `tests/i_fixtures.py` переиспользуется в O и T-373 | связка |

### Поток O — OpenAPI, примеры, документы (sonnet; после W, I1). Владеет: `docs/openapi.yaml`, `scripts/export_openapi.py`, `examples/**`, `docs/integration.md`, `tests/test_o_*.py`
| Шаг | Сначала тест | Реализация |
|---|---|---|
| O1 | `test_o_openapi.py`: `docs/openapi.yaml` == свежий экспорт (нормализованный YAML); все пути §4 есть; у каждого `/v1/*` описаны `401` и схема `Error`; описаны все заголовки `X-LLMHub-*` и `Idempotency-Key` | `scripts/export_openapi.py`, `responses=`/описания в маршрутах (по согласованию с W — правки только в декораторах) |
| O2 | `test_o_curl_examples.py` (`slow`): поднимает приложение как в I4, запускает каждый `examples/curl/*.sh` через `bash` с env → код 0; список скриптов = список в `docs/integration.md` | `examples/curl/*.sh` |
| O3 | `test_o_python_examples.py` (`slow`): `examples/python/*.py` против живого приложения → код 0; `test_o_csharp_example.py` (`slow`): `dotnet build` + `dotnet run` примера против живого приложения → код 0, **skip, если `dotnet` нет** (на этой машине нет — в `integration.md` заметка для gameDev запускать там); плюс статическая сверка путей/заголовков `.cs` с OpenAPI (выполняется всегда) | `examples/python/*`, `examples/csharp/*` |
| O4 | — (ревью) `docs/integration.md`: подключение, таблица заголовков и ошибок, синхронное окно и `Retry-After` (openai-python повторяет 503 сам, Retry-After > 60 с не уважает — советы), tasks/leases по шагам, первый чанк stream приходит в конце (v1), разрыв ≠ отмена, оплата отменённой облачной задачи | `docs/integration.md` |

### Порядок и параллельность
1. **G** (один implementer) — G1–G5 (вкл. G3a).
2. Параллельно: **H** (H1–H6), **R** (R1–R4), **W** (W1–W10, с заглушками H/R до их готовности). Владение файлами не пересекается; `__init__.py` пакетов — только I.
3. **I** (I1–I4) — после H, R, W.
4. **O** (O1–O4) — после W и I1; O2/O3 используют фикстуру живого приложения из `tests/i_fixtures.py`.

Модели: G, H, R, O — `sonnet` (G5 и шаблоны curl — можно `haiku`); W — `sonnet` с двумя циклами ревью; I — `opus`.

## 6. Список тестов-сценариев (сводно к критериям)

| Критерий задачи | Тесты |
|---|---|
| Эндпоинты chat (+stream), models, tasks, leases, admin, healthz, metrics | W6–W10, I3, I4 |
| Ошибки OpenAI `error` + 6 причин + `X-LLMHub-Truncated` | W1, W5, W6, **I2** |
| Синхронное ожидание ≤ 8 мин → 503 + Retry-After + task_id (ускоренные часы) | **W6** |
| Idempotency: ключ 7 суток, неявная ~15 мин, 409 | W4, W6, I3 (+ ядро `test_d_submit.py`) |
| Клиенты: хеш ключа (перечитывание без перезапуска), allowed_models, max_priority, private по умолчанию, cloud opt-in, детектор секретов, отказы; private/секрет никогда не в облако | W2, W3, W4, R1, R2, I2, I3 |
| local_inference: HTTP-клиент, фейковый сервер, семафор 1, EWMA, проба | H1–H5 |
| model_router: ECONOMY/BALANCED/QUALITY, псевдонимы конфига, облако недоступно → локально с пометкой, без LLM | R2–R4, I2 |
| OpenAPI сверяется, examples (curl, C#, openai-python), контрактный тест curl | O1–O3 |
| Просрочка в очереди (`deadline_exceeded`/`queue_wait_exceeded`) | G3a, H5, W7 |
| MCP `llmhub_ask` | → T-373 |
| pytest, ruff, pyright | каждый шаг |

## 7. Риски

- **Общее соединение SQLite** в одном loop: синхронный обработчик сломает его (поток FastAPI) — тест W10 «все маршруты async»; долгие запросы в `/admin/status` держат loop — запросы агрегатные, ограничить `LIMIT`.
- **Регекс-детектор на больших телах**: блокирует loop или уходит в катастрофический backtracking — `to_thread`, шаблоны без вложенных квантификаторов, тест 4 МБ и патологический ввод (R1), метрика `llmhub_loop_lag_s`.
- **Много ожидающих запросов**: один общий опросчик (W5, 100 ожидающих).
- **Перехват локальных задач ядром** (`requeue_orphaned_running` каждого `step()`): закрыт решением §2 п. 6 и тестом H5; при регрессии локальные задачи бесконечно перезапускались бы.
- **openai-python сам повторяет 503** (по умолчанию 2 раза) и не ждёт `Retry-After` > 60 с: повтор присоединяется к той же задаче (канонизация + неявная дедупликация), поэтому вреда нет, но общее время до ~24 мин — описано в `integration.md`.
- **SSE после отправленных заголовков** не может сменить статус на 503: ошибка уходит событием `data: {"error":…}` — проверено на openai-python в I4.
- **`/slots` у llama-server может быть выключен** (флаг `--slots`/`--no-slots` зависит от версии) — переход на `/health`, внешняя занятость тогда не видна (только EWMA). Проверить на юнитах при T-371 (только чтение `/slots`, без изменений юнитов).
- **Адрес llama-server**: в projectVoiceBot юниты указаны как `10.8.0.1:8090–8092`, в задаче — `127.0.0.1`; URL только в конфиге, решает T-371 (Q1).
- **Ключ llama-server** обязателен у юнитов (`tools/llm` шлёт Bearer) — `api_key_env`, значение в `/etc/llmhub/*.env` (Q2).
- **Оценка токенов байт/3,5** грубая: проверки контекста консервативны; 400 от llama-server по контексту → `context_too_large` без повтора.
- **Mock-тело `b"ok"`**: обёртка только для не-JSON; реальный облачный бэкенд (Q6) должен отдавать chat.completion JSON.
- **Правки проверенного ядра** (G4): только аддитивные; регрессия по тестам ядра в шаге G4 и полный прогон перед релизом.

## 8. Вопросы (ни один не блокирует implementer; приняты умолчания)

- Q1. Адрес юнитов llama-server в проде: `127.0.0.1` или `10.8.0.1` — конфиг, решает T-371.
- Q2. Ключ(и) llama-server для llmhub: тот же, что в `~/.config/local-llm/key`, или отдельный — T-371 (в коде — только имя переменной).
- Q3. `/metrics` под админ-ключом (умолчание). Если Prometheus без ключа нужен — опция `api.metrics_public` позже.
- Q4. Hold клиентами: в v1 только `/admin/hold`; клиенты держат GPU explicit/prewarm-lease с пульсом (ADR п. 6). Нужен клиентский hold — этап 5.
- Q5. Настоящий token-streaming для локального пути — отдельная задача после T-371 (v1: keep-alive + буфер).
- Q6. Закрыт: реальный облачный `InferenceBackend` — T-370 (поток G); контракт для T-369: `run` возвращает тело chat.completion JSON.
- Q7. Закрыт (координатор): отмена облачной задачи в работе — потраченное время GPU оплачено, документируется.
- Q8. Перенесён в T-373.

## 9. Приёмка

- [ ] Все шаги G–O отмечены, тесты задачи зелёные: `uv run pytest tests/test_g_*.py tests/test_h_*.py tests/test_r_*.py tests/test_w_*.py tests/test_i_*.py tests/test_o_*.py` (вкл. `slow`).
- [ ] Регрессия ядра по затронутым файлам: `uv run pytest tests/test_d_*.py tests/test_f_tz15.py tests/test_s0_skeleton.py`.
- [ ] `uv run ruff check` и `uv run pyright` — чисто.
- [ ] Каждая строка таблиц §3.5 (правила) и §4 (коды ошибок) покрыта тестом; шесть причин — в I2 в трёх вариантах; отрицательные тесты «private/секрет → не облако» зелёные.
- [ ] `docs/openapi.yaml` совпадает с экспортом; все curl-примеры проходят; openai-python пример проходит.
- [ ] Нет импортов из projectVoiceBot (`grep -r "voicebot\|extllm\|tools.llm" src` пусто); секретов в репозитории, логах и ответах нет.

## 10. Ревью и критерии остановки (ТЗ §18, CLAUDE.md)

- Два независимых цикла ревью: (1) **контракт и безопасность** — auth, data_class/секреты, коды ошибок, OpenAPI/примеры; (2) **конкурентность и сбои** — общий loop/соединение, пульс локального воркера, отмена, синхронное окно, дедупликация при повторах и перезапуске, `Busy`.
- Остановка: тесты задачи зелёные, ruff/pyright чисто, таблицы §3.5 и §4 покрыты полностью, у ревьюера нет открытых blocker/major. Не больше 3 раундов ревью; затем список нерешённого — заказчику.
- Находки и отклонения от плана — в журнал задачи.

## 11. Ворота 2026-11-04: critic + advocate (ADR-0139)
Решения координатора:
1. MCP (бывший поток M и MCP-часть I4) вынесен в T-373 «MCP-сервер llmhub_ask для агентов» (зависит от T-369); находки critic по MCP — в её критериях.
2. Код `cancelled` — HTTP 409 (не 499).
3. Ключи клиентов перечитываются без перезапуска: `SIGHUP` и `POST /admin/reload_clients` (§2 п. 7, W2, W9, I1).
4. Разрыв синхронного HTTP не отменяет задачу; результат доступен по `task_id` (§2 п. 3, W5, W6).
5. Отмена облачной задачи в работе: потраченное время GPU оплачено, документируется (§1, §4, O4).
6. Пример C#: настоящий `dotnet build` + запуск, skip без `dotnet`; заметка для gameDev (O3).

Critic high (внесено):
- F2 никогда не отправляет private/`secret_detected` в облако — §2 п. 10, правило F2, отрицательные тесты R2, W4, I2(в).
- Детектор секретов сканирует все строки запроса (content-parts, `tool_calls.arguments`, ответы tools, `tools`, `response_format`); шаблон имени ключа `[\w-]*(pass|secret|token|api[_-]?key)[\w-]*["']?\s*[:=]`, JWT, учётные данные в URI, OpenSSH/PGP — §3.5, R1 (формы JSON и env).
- Просроченные задачи в очереди → `deadline_exceeded`/`queue_wait_exceeded` (локальные и облачные) — §2 п. 12, `expire_overdue`, G3a, H5, W7.

Critic medium (внесено): async-зависимости auth + проверка `dependant.dependencies` (W10); один общий опросчик ожидания и выход по `is_disconnected` + нагрузка 100 ожидающих (W5); детектор в `to_thread`, шаблоны без вложенных квантификаторов, тест 4 МБ, метрика `llmhub_loop_lag_s` (R1, W10); `complete_local` с CAS по `status='running' AND worker_id` (G3, H5); SSE с `tool_calls` (index) и `reasoning_content`, проверка openai-python (W5, I4); эффективный `data_class` = клиент ∧ заголовок ∧ поле (W3); `admin_key_sha256=None` → 403 на `/admin` и `/metrics` (W2).

Critic low (внесено): Q6 обновлён (облачный бэкенд — T-370, тело chat.completion JSON); private + контекст > локального → `context_too_large` (H1/H2); `requeue_local` сохраняет последний код ошибки; ядровый `requeue` с `restarts > 5` для `pool IS NULL` → `fail(local_unavailable)` (G4); неявная дедупликация учитывает data_class/policy/priority (§2 п. 4); строка в «Входящие» BOARD для T-371 (nginx `proxy_read_timeout` > 480 с, `proxy_buffering off` для SSE); keep-alive-тест W6 двигает часы отдельной задачей; страж H6 — фикстуры не указывают на 8090–8092.

Advocate (внесено): тяжёлые чистые функции (`detect_secrets`, `canonical_body`, `json.loads`, sha256 тела) — через `asyncio.to_thread` (§2 п. 1); `integration.md`: в v1 первый чанк stream приходит только в конце (§3.9, O4).
