# T-368 — план: ядро llmhub на MockProvider

> TL;DR: Ядро без сети и денег: SQLite WAL (stdlib `sqlite3`, короткие `BEGIN IMMEDIATE`, без `await` внутри транзакции), автомат GPU с PENDING_DELETE/LEAKED, leases, очередь с пакетами `low`, бюджет с резервами и ledger, сторож отдельным процессом, kill-switch, `ProviderAdapter` без `stop` и MockProvider со сбоями. Все таймеры хранятся в БД; ядро работает как детерминированный `Controller.step()` на подменяемых часах. 42 шага: S0 (сквозной каркас), затем 6 потоков A–F, B–E параллельно. Ворота critic + advocate пройдены 2026-11-04 (§14). Блокирующих вопросов нет.

Задача: `tasks/active/T-368-core-on-mock.md`. Источники: ADR-0138; `/var/www/projectVoiceBot/platform/docs/research/T-367-cloud-gpu-manager.md` §6.3–§6.9, §6.12, §9 и раздел «Ворота 2026-11-04» (при расхождении главнее он); ТЗ `T-367-tz-v2.md` §6, §7, §8, §11, §15, §18.

## 1. Объём и что не входит

**Входит:** configuration, persistence, lifecycle_manager (автомат, leases, teardown, recovery), task_queue (очередь, idempotency, файлы тел), scheduler (триггеры, раздача), budget_manager, cost_tracker, cloud_manager (пулы, офферы, provisioner create/reconcile), reaper (отдельный процесс) и kill-switch, provider_adapters/{base, labels, mock, mock_inference, contract}, `clock.py`, `controller.py`. Админ-операции (kill, resume, hold, release) — функции Python, без HTTP.

**Не входит:**
- HTTP API, auth, OpenAPI — T-369.
- model_router и local_inference — T-369. Ядро только записывает решение «на локальную модель» (`route='local:fallback'`, `fallback_reason`); выполняет такие задачи T-369.
- weights и образ — T-370; systemd, CLI, деплой — T-371.
- Реальные адаптеры и любые сетевые вызовы — T-372.
- health_monitor и `/metrics`: ядро только пишет incidents и controller_state.

**Запреты (CLAUDE.md):** ни одного реального ключа и сетевого вызова, в т. ч. в тестах; без импортов из projectVoiceBot; секрет HMAC в тестах фиксированный и фиктивный.

## 2. Ключевые архитектурные решения плана

1. **SQLite: stdlib `sqlite3`, не aiosqlite.**
   - Транзакции короткие (≤ 1 мс, десятки строк), поэтому синхронный вызов в event loop не мешает.
   - `aiosqlite` переносит работу в поток, и тогда `BEGIN IMMEDIATE` может пережить `await`, а между шагами транзакции вклинится другая корутина. Это прямо противоречит правилу §6.9: короткие транзакции у одного процесса-владельца. Кроме того, это лишняя зависимость.
   - Настройки: `isolation_level=None` (транзакциями управляем сами), `PRAGMA journal_mode=WAL`, `synchronous=NORMAL`, `foreign_keys=ON`. `busy_timeout`: у контроллера 250–500 мс (`db.busy_timeout_ms`, по умолчанию 300; при `SQLITE_BUSY` действие повторяется в следующем `step()`), у сторожа 5000.
   - Своё соединение у контроллера, у сторожа (отдельный процесс) и у каждого потока в тесте гонок.
   - Время хранится как `REAL` (секунды эпохи UTC): так проще с подменяемыми часами.
2. **Правило «нет `await` в транзакции»** проверяется во время работы. `persistence.tx()` ставит флаг contextvar. `GuardedAdapter` (обёртка над любым адаптером) и `Clock.sleep` вызывают `assert_not_in_tx()`.
3. **Все таймеры — в БД**: `state_since`, `idle_since`, `expires_at`, `next_destroy_at`, `max_lifetime_at`, `accrued_to`. Повторы и бэкофф тоже не живут в `asyncio.sleep`. Поэтому перезапуск ничего не теряет, а `step()` идемпотентен.
4. **`Controller.step(now)`** — детерминированный проход: KILL-файл (проверяется в начале **и** в конце `step()`) → сверка → leases → таймауты → teardown → провижининг → раздача → начисления → бюджетные предупреждения. Возвращает `next_wakeup`. Боевой цикл: `while: step(); await clock.sleep_until(min(next_wakeup, event))`, где событие — новая задача, lease или завершение задачи. В тестах `Harness.run_until(t)` переводит `FakeClock` к ближайшему из `next_wakeup`, ожидающих `sleep` и следующего прихода задачи.
5. **Переходы автомата — compare-and-set**: `UPDATE instances SET state=?, … WHERE id=? AND state=?`. Затронута 0 строк — переход проиграл гонку (контроллер против сторожа), и это не ошибка. Каждый успешный переход пишет строку `transitions` в той же транзакции.
6. **Противоречие §6.3 и ворот** (STOPPING → ERROR и STOPPING → STOPPED против PENDING_DELETE/LEAKED) решено в пользу ворот:
   - STOPPING означает «решено остановить, destroy ещё не принят»; это состояние в уникальном индексе;
   - STOPPING → PENDING_DELETE — когда destroy принят **или** истёк `stop_confirm_timeout` (3 мин; так в §6.3 п. 5 разрешается новый цикл);
   - PENDING_DELETE → STOPPED — при подтверждённом нуле;
   - PENDING_DELETE → LEAKED — если с `destroy_requested_at` прошло больше 15 мин;
   - ребра STOPPING → ERROR нет.
7. **MockProvider на общем файловом хранилище** (`MockStore`, SQLite-файл `mock_cloud.db`, отдельный от `state.db`). Сторож — отдельный процесс и должен видеть те же «облачные» ресурсы. В юнит-тестах — `:memory:`.
8. **Готовность и выполнение на инстансе** — Protocol `InferenceBackend` (health, test_request, run). В T-368 есть только `MockInference`. httpx-реализация — T-369/T-370.

## 3. Файлы и интерфейсы

Пути от `src/llmhub/`. Новые файлы помечены «+», остальные — изменение заглушки.

### 3.1 `clock.py` (+) — поток A
```python
class Clock(Protocol):
    def now(self) -> float: ...                      # epoch seconds UTC
    async def sleep_until(self, t: float) -> None: ...
class SystemClock: ...                                # time.time + asyncio.sleep
class FakeClock:
    def __init__(self, start: float = 1_793_750_400.0) -> None: ...  # 2026-11-04T00:00Z
    def now(self) -> float: ...
    async def sleep_until(self, t: float) -> None: ...  # registers waiter future
    def next_waiter(self) -> float | None: ...
    def advance_to(self, t: float) -> None: ...        # resolves waiters <= t in time order
```

### 3.2 `configuration/` — поток A
- `models.py`, pydantic v2, `extra="forbid"`:
  - `QueueClassConfig(minimum_batch_size: int ≥ 1, maximum_batch_wait_s: float ≥ 0, maximum_queue_wait_s: float ≥ 0)`;
  - `QueueConfig(urgent, normal, low, deadline_margin_s=60, implicit_dedup_window_s=900, idem_ttl_s=604800)`. Умолчания: urgent 1/0/0, normal 1/0/900, low 5/1800/5400;
  - `TimeoutsConfig(provisioning_s=600, booting_s=600, loading_model_s=1200, stop_confirm_s=180, leak_after_s=900, max_lifetime_s=10800 [600..10800; тесты паритета обходят валидатор через `model_construct`/override], ready_test_timeout_s=30, model_test_retries=3, model_test_window_s=60, worker_heartbeat_s=30, worker_stale_s=180, network_loss_s=180, offer_wait_s=600, create_visibility_grace_s=120)`;
  - `LeaseConfig(default_ttl_s=600, min_ttl_s=30, max_ttl_s=3600, hold_max_ttl_s=28800)`;
  - `PoolConfig(name, provider, gpu, vram_gb, max_price_usd_h=0.50, idle_timeout_s=300 [0..900], max_concurrency=1, pp_tps, tg_tps, cold_start_s_default=540, image, disk_gb, models: list[str], subbudget: str | None)`;
  - `BudgetConfig(hard_cap_month=50, budget_month=45, budget_day=3, budget_session=1, budget_job=0.25, subbudgets={"exception": 10}, warn_levels=(0.5,0.8,0.95), pace_factor=1.25, stage_economy=0.8, stage_urgent_only=0.95, reserve_usd=1.0, min_balance_hours=1.0, divergence_abs=0.05, divergence_rel=0.05, timezone="UTC")`;
  - `RetryConfig(get_retries=3, get_backoff_s=1.0, create_max_attempts=2, job_max_attempts=2, breaker_errors=3, breaker_window_s=600, breaker_open_s=900, host_blacklist_s=86400, destroy_backoff_s=5.0, destroy_backoff_max_s=120.0)`;
  - `ProvidersConfig(enabled: list[str] = ["mock"])`;
  - `DbConfig(busy_timeout_ms=300 [250..500], reaper_busy_timeout_ms=5000)`;
  - в `RetryConfig` добавлено `call_timeout_s=20`; в `QueueConfig` — `deadline_includes_work: bool = True` (False только в режиме паритета); в `LeaseConfig` — `hold_may_exceed_session: bool = False`;
  - `Config(data_dir: Path, pools: dict[str, PoolConfig], queue, timeouts, leases, budget, retry, providers, task_type_max_tokens: dict[str,int] = {"default": 8000})`.
- `loader.py`: `load_config(path: Path, env: Mapping[str,str] = os.environ) -> Config`; `load_label_secret(env) -> bytes` (`LLMHUB_LABEL_SECRET`, ≥ 32 байт, иначе `ConfigError`). Секрет в `Config` не входит, поэтому `repr(config)` его не покажет.
- Ошибки диапазона (idle вне 0–15 мин, hold > 8 ч) — `ConfigError` с путём поля.

### 3.3 `persistence/` — поток A
- `migrations/0001_initial.sql` (DDL — раздел 4).
- `db.py`:
  ```python
  def connect(path: Path | str) -> sqlite3.Connection          # pragmas, row_factory=sqlite3.Row
  @contextmanager
  def tx(conn) -> Iterator[sqlite3.Connection]                   # BEGIN IMMEDIATE / COMMIT / ROLLBACK, sets contextvar
  def assert_not_in_tx() -> None
  def migrate(conn) -> int                                       # applies migrations/*.sql by PRAGMA user_version; mirrors schema_version
  ```
- `integrity.py`:
  ```python
  class OpenResult(NamedTuple): conn: sqlite3.Connection; safe_mode: bool; reason: str | None
  def open_state_db(path: Path, clock: Clock) -> OpenResult
  ```
  `PRAGMA integrity_check`. Не `ok` или `DatabaseError` при открытии → файл переименовывается в `state.db.corrupt-<ts>`, создаётся новая БД. В `controller_state` пишутся `halted=1`, `halt_reason=db_corrupt`, `safe_mode=1`, плюс инцидент `db_corrupt` (critical).
- `backup.py`: `backup(conn, dst_dir, clock, keep=7) -> Path` через `Connection.backup`; `maybe_daily_backup(conn, dst_dir, clock)` — не чаще раза в сутки, по ключу `last_backup_at`. Автовосстановления из копии нет (Q4, принято).
- **Перед снятием HALTED после порчи БД** обязательна сверка: `billing_since(month_start)` у каждого провайдера → строки ledger `source=provider` (по провайдеру, `instance_id` NULL). Без неё `resume()` отказывает (`ReconcileRequired`), иначе месячный бюджет обнулился бы.
- `state.py` (controller_state): `get(conn,key)`, `set(conn,key,value)`, `is_halted`, `halt(conn, reason, clock)`, `unhalt(conn, clock)`, `heartbeat(conn, clock)`.

### 3.4 `provider_adapters/` — поток B
`base.py`:
```python
class Capability(enum.Flag): NATIVE_LABELS, OFFER_SEARCH, BALANCE_API, BILLING_API, TRAFFIC_BILLED,
    INTERRUPTIBLE, CUSTOM_IMAGE, PROVIDER_TTL, SPEND_RATE
class UnavailableReason(StrEnum): INSUFFICIENT_BALANCE="insufficient_balance", QUOTA_EXCEEDED, PROVIDER_API_ERROR,
    GPU_UNAVAILABLE, DEPLOYMENT_ERROR, BUDGET_LIMIT_REACHED
class RemoteStatus(StrEnum): PENDING, RUNNING, EXITED, TERMINATED, UNKNOWN
class ResourceKind(StrEnum): INSTANCE, VOLUME, OTHER
@dataclass(frozen=True) class OfferQuery: gpu: str; min_vram_gb: int; max_price_usd_h: float; exclude_hosts: frozenset[str]; need_public_ip: bool = False
@dataclass(frozen=True) class Offer: provider: str; offer_id: str; gpu: str; gpu_count: int; vram_gb: int;
    price_usd_h: float; disk_usd_h: float; host_id: str; interruptible: bool; public_ip: bool
@dataclass(frozen=True) class InstanceSpec: pool: str; offer_id: str; image: str; disk_gb: int; max_price_usd_h: float;
    max_lifetime_s: int; env: Mapping[str, str]        # non-secret only
@dataclass(frozen=True) class RemoteResource: provider: str; ref: str; label: str; kind: ResourceKind; status: RemoteStatus;
    endpoint: str | None; host_id: str | None; price_usd_h: float; spend_rate_usd_h: float | None; created_at: float
class DestroyResult(StrEnum): ACCEPTED, ALREADY_GONE
@dataclass(frozen=True) class Balance: usd: float; at: float
@dataclass(frozen=True) class BillingRecord: ref: str | None; kind: str; from_ts: float; to_ts: float; amount_usd: float
@dataclass(frozen=True) class NormalizedError: reason: UnavailableReason; retryable: bool; outcome_unknown: bool;
    retry_after_s: float | None; http_status: int | None; message: str   # no secrets, no request text
class ProviderError(Exception): err: NormalizedError
class ProviderAdapter(Protocol):
    name: str
    capabilities: Capability
    async def list_offers(self, q: OfferQuery) -> list[Offer]: ...
    async def create(self, spec: InstanceSpec, label: str) -> RemoteResource: ...
    async def get(self, ref: str) -> RemoteResource | None: ...          # None == gone (404)
    async def list_owned(self, prefix: str = "llmhub-") -> list[RemoteResource]: ...   # all kinds
    async def destroy(self, ref: str) -> DestroyResult: ...             # idempotent; 404 -> ALREADY_GONE
    async def balance(self) -> Balance | None: ...                      # None without BALANCE_API
    async def billing_since(self, ts: float) -> list[BillingRecord]: ...
    def normalize_error(self, exc: BaseException) -> NormalizedError: ...
```
**Метода `stop`, `pause`, `start` нет.** Это проверяет контрактный тест.

`labels.py`:
```python
def new_op_id() -> str                                        # uuid4().hex[:12]
def make_label(op_id: str, secret: bytes) -> str              # f"llmhub-{op_id}-{hmac_sha256(secret, op_id)[:8]}"
def parse_label(label: str) -> tuple[str, str] | None
def verify_label(label: str, secret: bytes) -> bool           # hmac.compare_digest
```

`guarded.py`: `GuardedAdapter(inner, clock, retry_cfg, breaker: CircuitBreaker)`:
- перед каждым вызовом `assert_not_in_tx()`;
- `get`, `list_owned`, `balance`, `billing_since` повторяются до 3 раз с экспоненциальной задержкой через `clock`;
- `create` и `destroy` не повторяет;
- у каждого вызова жёсткий таймаут `retry.call_timeout_s` (20 с, `asyncio.timeout`). Таймаут `create` считается неизвестным исходом. Долгие вызовы (`create`, `destroy`) идут фоновыми задачами, а намерение записано в БД заранее (`create_called_at` / `destroy_requested_at`), поэтому зависший вызов не блокирует `step()` и kill;
- ошибки отдаёт в breaker.

`CircuitBreaker` хранит состояние в `provider_accounts`: 3 ошибки за 10 мин → open на 15 мин → half_open, одна пробная попытка. **Breaker ограничивает только `create` и `list_offers`.** `destroy`, `get`, `list_owned`, `balance` и `billing_since` проходят всегда. Ошибки сторожа (`actor="reaper"`) breaker контроллера не открывают.

`mock.py`, `mock_store.py`:
```python
@dataclass class MockKnobs:
    boot_s: float = 60; price_usd_h: float = 0.22; disk_usd_h: float = 0.0; destroy_delay_s: float = 0
    list_owned_lag_s: float = 0          # created resources invisible for this long
    balance_usd: float = 25.0; gpu_available: bool = True; offers: int = 3
class Fault(StrEnum):
    CREATE_TIMEOUT_CREATED, CREATE_TIMEOUT_NOT_CREATED, CREATE_TIMEOUT_CREATED_LATE, CREATE_5XX,
    CREATE_GHOST_REF,      # create returns a ref that never appears; the real resource has another ref
    CREATE_HANG,           # create never returns (until call_timeout)
    RATE_LIMIT_429, API_5XX, INSUFFICIENT_BALANCE, QUOTA, GPU_UNAVAILABLE, DESTROY_FAIL, DESTROY_NEVER,
    DESTROY_429,           # destroy -> 429 with retry_after
    DESTROY_PARTIAL,       # instance gone, attached VOLUME with same label remains (billed)
    LIST_STALE             # list_owned returns a snapshot older than N s
class MockProvider(ProviderAdapter):
    def __init__(self, store: MockStore, clock: Clock, knobs: MockKnobs | None = None, name: str = "mock")
    def inject(self, op: Literal["create","get","list_owned","destroy","balance","any"], fault: Fault, times: int = 1) -> None
    def interrupt(self, ref: str) -> None                 # status -> TERMINATED (GPU loss)
    def stopped_but_billing(self, ref: str) -> None       # status EXITED, still billed, spend_rate>0
    def add_foreign(self, label: str) -> str              # non-llmhub or bad-HMAC resource
    def add_orphan(self, label: str) -> str               # valid label, no DB row
    def live_refs(self) -> list[str]                      # ground truth for tests (incl. invisible)
    def billed_total(self, since: float = 0) -> float
```
- Время берётся из `clock`.
- Начисления — посекундно: от create до `destroyed_at + destroy_delay_s`.
- `balance` уменьшается на начисленное. При балансе ≤ 0 ресурсы удаляются, как у RunPod.
- `DESTROY_NEVER` — destroy принят, но ресурс не исчезает (путь в LEAKED).
- Mock добавляет флаг `Capability.SPEND_RATE` — расширение списка §6.5 (записано в журнале задачи).

`mock_inference.py`:
```python
class InferenceBackend(Protocol):     # protocol itself lives in cloud_manager/inference.py (stream B owns it)
    async def ping(self, endpoint: str) -> bool: ...
    async def health(self, endpoint: str) -> bool: ...
    async def test_request(self, endpoint: str) -> bool: ...
    async def run(self, endpoint: str, job: JobRunSpec) -> JobRunResult: ...
@dataclass class MockInferenceKnobs: load_s: float = 6; pp_tps: float = 1200; tg_tps: float = 78
    health_never: bool = False; test_fail: int = 0; hang_load: bool = False
class MockInference(InferenceBackend):
    def __init__(self, provider: MockProvider, clock: Clock, knobs: MockInferenceKnobs | None = None)
    def lose_network(self, ref: str, until: float) -> None
```
`run` спит `in/pp + out/tg` через `clock.sleep_until`. Если ресурс прерван, `run` бросает `InstanceLost`; при потере сети — `NetworkLost`.

`contract.py` — набор проверок, не зависящих от провайдера:
```python
class AdapterHarness(Protocol):   # how the contract drives time / visibility for a given provider
    adapter: ProviderAdapter
    async def settle(self) -> None: ...
async def run_contract(h: AdapterHarness, secret: bytes) -> None   # raises AssertionError with item id
CONTRACT_ITEMS: tuple[str, ...]  # C1..C10 listed in §7.2
```

### 3.5 `lifecycle_manager/` — поток C
`states.py`:
```python
class State(StrEnum): STOPPED, PROVISIONING, BOOTING, LOADING_MODEL, READY, BUSY, IDLE, STOPPING,
    PENDING_DELETE, LEAKED, ERROR
LIVE_IN_INDEX = frozenset(State) - {STOPPED, PENDING_DELETE, LEAKED}
BILLABLE = frozenset(State) - {STOPPED}
@dataclass(frozen=True) class Edge: src: State; dst: State; trigger: str
TRANSITIONS: frozenset[Edge]     # exactly table §5
def check_edge(src: State, dst: State, trigger: str) -> Edge   # raises IllegalTransition
```
`machine.py`:
```python
def insert_provisioning(conn, *, pool, provider, op_id, label, max_lifetime_at, now,
                        gate: Callable[[sqlite3.Connection], Gate]) -> str | None
    # ONE tx: gate(conn) re-checks HALTED, provider block and check_launch, then INSERT + session reservation;
    # None on UNIQUE violation or failed gate
def set_provider_ref(conn, instance_id, ref, host_id, price_usd_h, now) -> State
    # UPDATE by id WITHOUT state CAS; returns current state. state != PROVISIONING -> caller destroys ref at once
def transition(conn, instance_id: str, src: State, dst: State, trigger: str, now: float,
               actor: Literal["controller","reaper","admin"], **fields) -> bool                  # CAS + transitions row
def live_instance(conn, pool) -> sqlite3.Row | None
def try_idle_to_stopping(conn, instance_id, now, reason) -> Literal["stopping","busy","noop"]  # ONE tx: queue+leases check
```
`leases.py`:
```python
class LeaseKind(StrEnum): IMPLICIT, EXPLICIT, HOLD, PREWARM
def create_implicit(conn, job_id, pool, client_id, now) -> str
def create_explicit(conn, *, holder, client_id, pool, ttl_s, now, prewarm=False) -> str
def create_hold(conn, *, holder, pool, ttl_s, now) -> str          # ttl_s <= hold_max_ttl
def renew(conn, lease_id, now) -> float                            # raises LeaseExpired/LeaseClosed
def close(conn, lease_id, now, *, final=False, reason="closed") -> None
def close_for_job(conn, job_id, now) -> None
def expire_due(conn, now) -> list[str]                             # closes + incident lease_expired
def live_count(conn, pool, now, kinds: Iterable[LeaseKind] | None = None) -> int
def drain_requested(conn, pool, since: float) -> bool              # final close after last activity
```
Живой lease: `closed_at IS NULL AND (expires_at IS NULL OR expires_at > now)`. У implicit `expires_at` равен NULL: такой lease живёт вместе с задачей.

`teardown.py` — общий для контроллера и сторожа:
```python
async def request_destroy(conn, adapter, inst_row, now, cfg, actor) -> None   # STOPPING: destroy; ACCEPTED->PENDING_DELETE
async def confirm_zero(conn, adapter, inst_row, now, cfg, actor) -> bool      # PENDING_DELETE/LEAKED -> STOPPED
def mark_leaks(conn, now, cfg, actor) -> list[str]                            # PENDING_DELETE > leak_after -> LEAKED, incident, block, halt
```
Ноль подтверждён, если в `list_owned(label)` нет ресурсов **ни одного `ResourceKind`** (instance, volume, other) с этим label и `spend_rate` равен 0 или не поддерживается. Остатки другого вида (DESTROY_PARTIAL) удаляются по своему `ref`. Для строк без `provider_ref` ещё нужно, чтобы с create прошло не меньше `create_visibility_grace_s`.

`recovery.py`: `async def reconcile_on_start(conn, adapters, inference, secret, now, cfg, boot_id) -> ReconcileReport` (§6.3 п. 13, детали — раздел 8).

### 3.6 `task_queue/` — поток D
```python
class Priority(StrEnum): URGENT="urgent", NORMAL="normal", LOW="low"   # rank 0/1/2
class JobStatus(StrEnum): SUBMITTED, QUEUED, RUNNING, COMPLETED, FAILED, CANCELLED
@dataclass(frozen=True) class JobSpec: client_id: str; body: bytes; priority: Priority; model_alias: str;
    policy: str; data_class: str; route: str; pool: str | None; route_reason: str; task_type: str | None = None;
    idem_key: str | None = None; deadline_at: float | None = None; max_queue_wait_s: float | None = None;
    timeout_s: float | None = None; est_tokens_in: int = 0; max_tokens: int | None = None
@dataclass(frozen=True) class SubmitResult: job_id: str; created: bool; status: JobStatus
class IdempotencyConflict(Exception): job_id: str
def submit(conn, bodies: BodyStore, spec: JobSpec, now: float, cfg: Config) -> SubmitResult
def pool_queue(conn, pool) -> list[sqlite3.Row]          # queued cloud jobs of pool
def pick_next(conn, pool, now) -> sqlite3.Row | None     # prio rank, then round-robin client, then created_at
def mark_running(conn, job_id, instance_id, worker_id, now) -> bool
def heartbeat(conn, job_ids, worker_id, now) -> None
def complete(conn, job_id, result: JobRunResult, cost_usd, now) -> None   # closes implicit lease same tx
def requeue(conn, job_id, now, *, count_attempt: bool, reason: str) -> Literal["queued","fallback","failed"]
def fallback_local(conn, job_id, reason: UnavailableReason | str, now) -> None   # route='local:fallback'
def cancel(conn, job_id, now) -> JobStatus
def requeue_orphaned_running(conn, boot_id, now, stale_s) -> list[str]   # count_attempt=False: restart is not an attempt (restarts++ separately)
def purge_expired(conn, now) -> None                     # idem_key NULL after 7d, result files after 7d
```
`submit` в одной транзакции:
1. Освобождает просроченный явный ключ: `UPDATE jobs SET idem_key=NULL … idem_expires_at<=now`.
2. Явный ключ уже есть: тело совпадает → вернуть существующую задачу (`created=False`); тело другое → `IdempotencyConflict` (409).
3. Ключа нет → неявная дедупликация по `(client_id, body_sha256)`, пока задача `queued`/`running` или с её завершения прошло ≤ `implicit_dedup_window_s`.
4. INSERT с `status='queued'` и неявным lease (`pool IS NOT NULL`).

Тело пишется в файл **до** транзакции (tmp + rename, каталог 0700); при конфликте файл удаляется.

`round-robin`: внутри ранга приоритета первым идёт клиент с самым старым `MAX(started_at)` в этом пуле (`NULLS FIRST`), затем `created_at`. Отдельного состояния не нужно: всё выводится из `jobs`.

`bodies.py`: `BodyStore(root: Path)` — `write_request(job_id, body, meta: dict) -> Path`, `write_result`, `read_*`, `iter_recoverable() -> Iterator[(job_id, meta, body)]`, `purge(older_than)`. Права: файлы 0600, каталог 0700. `meta` нужна для восстановления после порчи БД. Текстов запросов в журналах нет.

### 3.7 `scheduler/` — поток D
```python
@dataclass(frozen=True) class TriggerDecision: launch: bool; reason: str; wake_at: float | None   # earliest future trigger
def launch_trigger(jobs: Sequence[QueuedJob], now: float, qcfg: QueueConfig, t_cold_p90: float,
                   work_s: Callable[[QueuedJob], float], prewarm: bool) -> TriggerDecision   # pure
def effective_deadline(job: QueuedJob, qcfg: QueueConfig) -> float | None   # min(deadline_at, created_at+max_queue_wait)
def dispatch_capacity(running: int, max_concurrency: int) -> int
```
Правило триггера (§6.7), запуск при любом условии:
- есть `urgent`;
- `count(prio) ≥ minimum_batch_size(prio)` для какого-либо приоритета;
- `now − min(created_at) ≥ maximum_batch_wait_s` для приоритета с `maximum_batch_wait_s > 0`;
- `effective_deadline − now ≤ t_cold_p90 + (work_s(job) if deadline_includes_work else 0) + deadline_margin_s`;
- живой prewarm-lease.

Иначе `wake_at` — ближайший момент, когда условие станет истинным.

Для normal `minimum_batch_size` = 1, поэтому любая normal-задача запускает пул сразу. Пакеты действуют только для `low`. Если в пуле уже есть живой инстанс, задачи `low` раздаются сразу, без ожидания пакета (отложенное выполнение).

### 3.8 `budget_manager/` и `cost_tracker/` — поток E
`cost_tracker/ledger.py`:
```python
def accrue(conn, now) -> None               # per BILLABLE instance: row [accrued_to, now) gpu (+disk), source=estimate
def finalize(conn, instance_id, until) -> None
def spent(conn, start: float, end: float, *, pool_subbudget: str | None = None) -> float
    # sum per provider of max(estimate_in_window, provider_billed_in_window); see §6
def session_cost(conn, instance_id) -> float
```
`cost_tracker/reconcile.py`: `async def reconcile(conn, adapter, now, cfg) -> ReconcileResult`. Делает snapshot баланса и `billing_since(month_start)`, сравнивает и при необходимости пишет инцидент `ledger_divergence`. Частота: раз в 15 мин при живом инстансе, иначе раз в сутки (по `controller_state.last_reconcile_at:<provider>`).

`budget_manager/`:
```python
# estimate.py
@dataclass(frozen=True) class LaunchEstimate: usd: float; t_cold_s: float; work_s: float; idle_s: float; per_job: dict[str, float]
def t_cold_p90(conn, pool, default_s: float, n: int = 20) -> float     # from transitions PROVISIONING->READY durations
def work_seconds(tokens_in: int, max_tokens: int, pp: float, tg: float) -> float
def estimate_launch(price_usd_h, jobs, t_cold_s, idle_s, pp, tg, traffic_usd=0.0, storage_usd=0.0) -> LaunchEstimate
def update_speed_ewma(conn, pool, pp_obs: float | None, tg_obs: float | None, alpha: float = 0.3) -> None
# limits.py
class Stage(IntEnum): NORMAL=0, ECONOMY=1, URGENT_ONLY=2, NO_LAUNCH=3
@dataclass(frozen=True) class Gate: ok: bool; reason: UnavailableReason | None; detail: str
def windows(now: float, tz: str) -> Windows                            # day/month bounds
def committed(conn, start, end, now) -> float                          # ledger + open reservation remainders
def stage(conn, now, cfg) -> Stage
def check_launch(conn, est: LaunchEstimate, provider: str, now, cfg, priority: Priority, subbudget: str | None) -> Gate
def check_job_on_running(conn, instance_id, job, now, cfg) -> Gate
def warnings(conn, now, cfg) -> list[str]                              # writes dedup incidents
# reservations.py
def reserve(conn, scope, ref_id, amount, now, instance_id=None) -> str
def release(conn, reservation_id | ref, now, reason) -> None
def remainder(conn, reservation_row) -> float                          # max(0, amount - consumed)
def consume_fifo(conn, instance_id, amount) -> None                    # accrual of an instance consumes its open reservations FIFO (session first)
# accounts.py
def block_provider(conn, provider, reason, now) -> None                # incidents + no retries
def resume_provider(conn, provider, now) -> None                       # manual only
def is_blocked(conn, provider) -> str | None
# allocation.py
def allocate_session(conn, instance_id) -> dict[str, tuple[float, float]]   # client -> (work_usd, shared_usd); writes cost_allocations
```

### 3.9 `cloud_manager/` и `controller.py` — поток F
- `cloud_manager/pools.py`: `PoolRuntime(cfg, adapter, inference)` и `resolve_pool(model_alias, cfg) -> str`.
- `cloud_manager/offers.py`: `select_offer(offers, query, blacklist) -> Offer | None`. Score — минимальная цена за час среди подходящих; без публичного IP, если он не требуется. Офферы с хостами из `host_blacklist` и превышением `max_price` отбрасываются.
- `cloud_manager/provisioner.py`:
  ```python
  async def provision_step(conn, pool_rt, inst_row, now, cfg, secret) -> None   # create / adopt / advance PROVISIONING..READY
  async def create_or_adopt(conn, adapter, inst_row, spec, now, cfg) -> CreateOutcome
  class CreateOutcome(StrEnum): CREATED, ADOPTED, RETRY_LATER, NO_OFFER, BLOCKED, FAILED
  def find_duplicates(owned: list[RemoteResource], inst_row) -> list[RemoteResource]
  ```
  - После ответа `create` ref сохраняется **всегда** (`set_provider_ref`). Если строка уже не в PROVISIONING (kill, таймаут, recovery), ресурс сразу уничтожается.
  - **Правило дублей** (контроллер в каждой сверке и сторож): ресурсы с label живой строки, у которых `ref ≠ provider_ref`, — дубли. Если `provider_ref IS NULL`, после adopt лишними считаются все, кроме выбранного: самый ранний `created_at`, при равенстве — меньший ref. Дубль → destroy + инцидент `duplicate_resource` (high). Если `provider_ref` не найден в `list_owned` дольше grace (CREATE_GHOST_REF), строка переходит на найденный по label ресурс, а пропавший ref считается дублем.
- `cloud_manager/inference.py`: `InferenceBackend` Protocol, `JobRunSpec`, `JobRunResult`, `InstanceLost`, `NetworkLost` (владелец — поток B, шаг B1).
- `controller.py` (+):
  ```python
  class Controller:
      def __init__(self, conn, cfg: Config, clock: Clock, adapters: Mapping[str, ProviderAdapter],
                   inference: Mapping[str, InferenceBackend], bodies: BodyStore, secret: bytes, boot_id: str) -> None
      async def start(self) -> ReconcileReport          # integrity done by caller; reconcile_on_start
      async def step(self) -> float                     # returns next_wakeup
      def submit(self, spec: JobSpec) -> SubmitResult   # sync, tx only; wakes loop
      # admin ops (used by T-369 /admin/*):
      async def kill(self, reason: str = "admin") -> KillReport
      def resume(self, provider: str | None = None) -> None
      def hold(self, pool: str, ttl_s: float, holder: str = "admin") -> str
      def release(self, pool: str) -> None
      async def run_forever(self, stop: asyncio.Event) -> None
  ```
  Выполнение задач: `step()` поднимает `asyncio.Task` на каждую отданную задачу (`inference.run`). Завершение задачи пишет `complete` и будит цикл. Пульс воркера — `jobs.heartbeat` для задач из своего реестра, при каждом `step()` и не реже `worker_heartbeat_s`.
- `killswitch.py` (+, поток F):
  ```python
  async def kill_all(conn, adapters, secret, clock, cfg, actor, deadline_s=60) -> KillReport
  ```
  Шаги:
  1. Транзакция: `halt`, закрыть hold и prewarm, все живые инстансы → STOPPING (`trigger=kill`).
  2. Параллельно destroy всех строк и всех ресурсов из `list_owned`, у которых совпал HMAC.
  3. Опрос до нуля с интервалом 2 с, но не дольше `deadline_s`. Нет нуля — инцидент critical.

  `check_kill_file(data_dir) -> bool` — файл `KILL`. Kill не ждёт зависших вызовов create: их строки уже записаны, и ref, вернувшийся позже, уничтожается по правилу `set_provider_ref`.

### 3.10 `reaper/` — поток F (после C и E)
- `reaper/core.py`: `class Reaper(conn, cfg, adapters, clock, secret)` с методом `async run_once() -> ReaperReport`. Порядок:
  1. KILL-файл → `kill_all(actor="reaper")`.
  2. `hard_cap_month`: сторож сначала начисляет по CAS (`UPDATE … SET accrued_to=? WHERE id=? AND accrued_to IS ?`, без двойного счёта с контроллером), затем считает `spent(month)` с оценкой живых BILLABLE до `now`. Это работает и при мёртвом контроллере. `≥ 50` → kill с причиной `hard_cap`.
  3. `confirm_zero` и повтор destroy для PENDING_DELETE и LEAKED.
  4. `mark_leaks`.
  5. Orphan sweep:
     - верный HMAC и строка в STOPPED/ERROR, либо без строки при `safe_mode=1` → destroy;
     - дубли по правилу §3.9 → destroy + инцидент;
     - **без строки в обычном режиме** (решение Q1): инцидент `unknown_owned_resource` critical и HALT новых запусков; сверка каждые 15 мин, пока ресурс жив. Через 30 мин после обнаружения — автоудаление, если HMAC проверяется текущим секретом (`local_secret_id` совпадает).
  6. Независимый предохранитель `max_lifetime_at + 600 с` → STOPPING (actor reaper) и destroy.
  7. Устаревший heartbeat контроллера (> 10 мин) при живых инстансах — инцидент.
  8. Чужие ресурсы (без HMAC) не трогаются никогда.
- `reaper/__main__.py`: `python -m llmhub.reaper --config PATH [--once | --loop SECONDS]`, по умолчанию `--loop 15` (не больше 15 с, чтобы kill одним сторожем укладывался в 60 с; systemd timer 5 мин — только страховка перезапуска). Свой `connect()`, свои адаптеры. Для mock — `MockStore` по пути `providers.mock.store_path`.

## 4. Схема SQLite (`persistence/migrations/0001_initial.sql`)
```sql
CREATE TABLE controller_state (
  key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at REAL NOT NULL
) STRICT;  -- keys: schema_version, halted, halt_reason, safe_mode, heartbeat, boot_id,
           -- local_secret_id (sha256(secret)[:8]), last_backup_at, last_reconcile_at:<p>, ewma:<pool>:pp|tg

CREATE TABLE clients (
  id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, key_hash TEXT NOT NULL,
  default_policy TEXT NOT NULL DEFAULT 'BALANCED' CHECK (default_policy IN ('ECONOMY','BALANCED','QUALITY')),
  default_priority TEXT NOT NULL DEFAULT 'normal' CHECK (default_priority IN ('urgent','normal','low')),
  max_priority TEXT NOT NULL DEFAULT 'normal' CHECK (max_priority IN ('urgent','normal','low')),
  allowed_models_json TEXT NOT NULL DEFAULT '[]',
  budget_overrides_json TEXT,
  data_class TEXT NOT NULL DEFAULT 'private' CHECK (data_class IN ('private','cloud_allowed')),
  created_at REAL NOT NULL, disabled_at REAL
) STRICT;

CREATE TABLE instances (
  id TEXT PRIMARY KEY, op_id TEXT NOT NULL UNIQUE, pool TEXT NOT NULL, provider TEXT NOT NULL,
  provider_ref TEXT, label TEXT NOT NULL UNIQUE, offer_json TEXT, host_id TEXT, gpu TEXT, price_usd_h REAL,
  state TEXT NOT NULL CHECK (state IN ('STOPPED','PROVISIONING','BOOTING','LOADING_MODEL','READY','BUSY',
         'IDLE','STOPPING','PENDING_DELETE','LEAKED','ERROR')),
  state_since REAL NOT NULL, idle_since REAL, endpoint TEXT, tls_fingerprint TEXT,
  created_at REAL NOT NULL, create_called_at REAL, ready_at REAL, max_lifetime_at REAL NOT NULL,
  stop_after_current INTEGER NOT NULL DEFAULT 0, stop_reason TEXT,
  destroy_requested_at REAL, destroy_accepted_at REAL, destroy_attempts INTEGER NOT NULL DEFAULT 0,
  next_destroy_at REAL, zero_confirmed_at REAL,
  attempts INTEGER NOT NULL DEFAULT 0, model_test_failures_json TEXT NOT NULL DEFAULT '[]',
  net_fail_since REAL, accrued_to REAL, last_error TEXT
) STRICT;
CREATE UNIQUE INDEX instances_one_live_per_pool ON instances(pool)
  WHERE state NOT IN ('STOPPED','PENDING_DELETE','LEAKED');
CREATE INDEX instances_state ON instances(state);

CREATE TABLE jobs (
  id TEXT PRIMARY KEY, client_id TEXT NOT NULL REFERENCES clients(id),
  idem_key TEXT, idem_expires_at REAL, body_sha256 TEXT NOT NULL, created_at REAL NOT NULL,
  priority TEXT NOT NULL CHECK (priority IN ('urgent','normal','low')),
  prio_rank INTEGER NOT NULL CHECK (prio_rank IN (0,1,2)),
  task_type TEXT, model_alias TEXT NOT NULL, policy TEXT NOT NULL, data_class TEXT NOT NULL,
  pool TEXT, deadline_at REAL, max_queue_wait_s REAL, timeout_s REAL,
  status TEXT NOT NULL CHECK (status IN ('submitted','queued','running','completed','failed','cancelled')),
  route TEXT NOT NULL, route_reason TEXT, fallback_reason TEXT,
  attempts INTEGER NOT NULL DEFAULT 0, max_attempts INTEGER NOT NULL DEFAULT 2, restarts INTEGER NOT NULL DEFAULT 0,
  est_tokens_in INTEGER NOT NULL DEFAULT 0, est_tokens_out INTEGER NOT NULL,
  tokens_in INTEGER, tokens_out INTEGER, finish_reason TEXT,
  est_cost_usd REAL, cost_usd REAL, error_code TEXT,
  instance_id TEXT REFERENCES instances(id), worker_id TEXT, worker_heartbeat_at REAL,
  req_path TEXT NOT NULL, res_path TEXT, started_at REAL, finished_at REAL,
  cancel_requested INTEGER NOT NULL DEFAULT 0
) STRICT;
CREATE UNIQUE INDEX jobs_idem ON jobs(client_id, idem_key) WHERE idem_key IS NOT NULL;
CREATE INDEX jobs_pool_queue ON jobs(pool, status, prio_rank, created_at);
CREATE INDEX jobs_body ON jobs(client_id, body_sha256, status, finished_at);
CREATE INDEX jobs_instance ON jobs(instance_id, status);

CREATE TABLE leases (
  id TEXT PRIMARY KEY, holder TEXT NOT NULL, client_id TEXT REFERENCES clients(id),
  kind TEXT NOT NULL CHECK (kind IN ('implicit','explicit','hold','prewarm')),
  pool TEXT NOT NULL, job_id TEXT REFERENCES jobs(id), ttl_s REAL, expires_at REAL,
  created_at REAL NOT NULL, renewed_at REAL, closed_at REAL, close_reason TEXT,
  final INTEGER NOT NULL DEFAULT 0,
  CHECK ((kind = 'implicit') = (job_id IS NOT NULL)),
  CHECK (kind = 'implicit' OR expires_at IS NOT NULL)
) STRICT;
CREATE UNIQUE INDEX leases_one_per_job ON leases(job_id) WHERE kind = 'implicit';
CREATE INDEX leases_live ON leases(pool, closed_at, expires_at);

CREATE TABLE transitions (
  id INTEGER PRIMARY KEY, instance_id TEXT NOT NULL REFERENCES instances(id), ts REAL NOT NULL,
  from_state TEXT NOT NULL, to_state TEXT NOT NULL, reason TEXT NOT NULL,
  actor TEXT NOT NULL CHECK (actor IN ('controller','reaper','admin'))
) STRICT;
CREATE INDEX transitions_inst ON transitions(instance_id, ts);

CREATE TABLE ledger (
  id INTEGER PRIMARY KEY, instance_id TEXT REFERENCES instances(id), provider TEXT NOT NULL, pool TEXT,
  kind TEXT NOT NULL CHECK (kind IN ('gpu','disk','traffic','storage')),
  from_ts REAL NOT NULL, to_ts REAL NOT NULL, rate REAL, amount_usd REAL NOT NULL CHECK (amount_usd >= 0),
  source TEXT NOT NULL CHECK (source IN ('estimate','provider')), created_at REAL NOT NULL,
  CHECK (to_ts >= from_ts),
  UNIQUE (instance_id, kind, source, from_ts)
) STRICT;
CREATE INDEX ledger_time ON ledger(provider, from_ts);

CREATE TABLE reservations (
  id TEXT PRIMARY KEY, scope TEXT NOT NULL CHECK (scope IN ('session','job','hold','prewarm')),
  ref_id TEXT NOT NULL, instance_id TEXT REFERENCES instances(id), amount_usd REAL NOT NULL CHECK (amount_usd >= 0),
  consumed_usd REAL NOT NULL DEFAULT 0 CHECK (consumed_usd >= 0),
  created_at REAL NOT NULL, released_at REAL, release_reason TEXT
) STRICT;
CREATE INDEX reservations_open ON reservations(released_at);

CREATE TABLE provider_accounts (
  provider TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 1,
  blocked_reason TEXT, blocked_at REAL,
  circuit_state TEXT NOT NULL DEFAULT 'closed' CHECK (circuit_state IN ('closed','open','half_open')),
  circuit_opened_at REAL, error_ts_json TEXT NOT NULL DEFAULT '[]',
  last_balance_usd REAL, last_balance_at REAL
) STRICT;

CREATE TABLE provider_snapshots (
  id INTEGER PRIMARY KEY, provider TEXT NOT NULL, ts REAL NOT NULL, balance_usd REAL,
  billed_usd_period REAL, period_from REAL
) STRICT;

CREATE TABLE incidents (
  id INTEGER PRIMARY KEY, ts REAL NOT NULL, kind TEXT NOT NULL,
  severity TEXT NOT NULL CHECK (severity IN ('info','warning','high','critical')),
  ref TEXT, dedup_key TEXT UNIQUE, details_json TEXT NOT NULL DEFAULT '{}', resolved_at REAL
) STRICT;

-- вне списка §6.6, нужны логике:
CREATE TABLE host_blacklist (
  provider TEXT NOT NULL, host_id TEXT NOT NULL, until REAL NOT NULL, reason TEXT NOT NULL,
  PRIMARY KEY (provider, host_id)
) STRICT;
CREATE TABLE cost_allocations (
  instance_id TEXT NOT NULL REFERENCES instances(id), client_id TEXT NOT NULL,
  work_usd REAL NOT NULL, shared_usd REAL NOT NULL, PRIMARY KEY (instance_id, client_id)
) STRICT;
```
`details_json` не содержит текстов запросов и секретов.

## 5. Таблица переходов автомата (`states.TRANSITIONS`)
«Пул STOPPED» — в пуле нет строки из `LIVE_IN_INDEX`. Все переходы — CAS со строкой `transitions`. Колонка «Ситуация» — номер строки таблицы §6.3.

| # | Из → В | Триггер / охрана | Действия (в той же транзакции, если не сказано иное) | Ситуация |
|---|---|---|---|---|
| E1 | (нет) → PROVISIONING | `launch_trigger` = истина; не HALTED; провайдер не заблокирован, breaker не open; `check_launch` ok | `insert_provisioning` (op_id, label, `max_lifetime_at`); резерв `session` = E_launch. UNIQUE-конфликт → noop | 1, 2 |
| E2 | PROVISIONING → BOOTING | create вернул ref или ref найден в `list_owned(label)`, `get` = RUNNING | `provider_ref`, `host_id`, `price_usd_h` | 6 |
| E3 | PROVISIONING → ERROR | таймаут 10 мин; ошибка без шанса на успех (quota, deployment); `insufficient_balance` (провайдер блокируется); неизвестный исход и `attempts ≥ 2`; нет оффера дольше `offer_wait` | `last_error`; задачи пула — fallback/очередь (§6.8) | 6, 7, 19 |
| E4 | BOOTING → LOADING_MODEL | `inference.ping` = истина | — | — |
| E5 | BOOTING → ERROR | таймаут 10 мин; `get` = None/TERMINATED/EXITED | хост в blacklist | 9 |
| E6 | LOADING_MODEL → READY | `health` и `test_request` ок | `ready_at`; T_cold для p90 | — |
| E7 | LOADING_MODEL → ERROR | таймаут 20 мин (зависание); 3 провала теста за 60 с; ресурс пропал | хост в blacklist на 24 ч | 8, 10 |
| E8 | READY → BUSY | `pick_next` отдал задачу | `mark_running` | 2 |
| E9 | READY → IDLE | очередь пула пуста, живых leases нет (в т. ч. после recovery) | `idle_since = now` | 13 |
| E10 | READY → ERROR | ресурс пропал / сеть > 3 мин | — | 11 |
| E11 | BUSY → BUSY | задача завершена, в очереди есть ещё (строки transitions нет) | `pick_next` | 3 |
| E12 | BUSY → IDLE | нет задач `running` и `queued` пула, нет живых leases | `idle_since = now` | — |
| E13 | IDLE → BUSY | новая задача или lease (решение в той же транзакции, что и проверка) | таймер сброшен (`idle_since = NULL`) | 4 |
| E14 | IDLE → STOPPING | `try_idle_to_stopping`: (idle_timeout истёк **или** drain **или** stage NO_LAUNCH **или** max_lifetime **или** hold истёк) **и** очередь пуста **и** leases = 0; иначе → E13 | `stop_reason` | 14, 15 |
| E15 | READY/IDLE/BUSY/PROVISIONING/BOOTING/LOADING_MODEL → STOPPING | kill (из любого живого состояния) | задачи `running` → requeue без `attempts++` | — |
| E16 | BUSY → STOPPING | `stop_after_current = 1` (max_lifetime, `budget_session`, stage NO_LAUNCH) и `running = 0` | новых задач не отдаём | — |
| E17 | BUSY → ERROR, IDLE → ERROR | `InstanceLost` / `get` = TERMINATED / сеть > `network_loss_s` / `stopped_but_billing` (EXITED) | задачи `running` → requeue с `attempts++` (при `max` → fallback/failed) | 9, 11 |
| E18 | ERROR → STOPPING | всегда, в том же `step()` | — | 8, 9 |
| E19 | STOPPING → PENDING_DELETE | destroy вернул ACCEPTED/ALREADY_GONE **или** с `destroy_requested_at` прошло ≥ `stop_confirm_s` | `destroy_accepted_at`; пул свободен для нового цикла | 5, 18 |
| E20 | PENDING_DELETE → STOPPED | ноль подтверждён (`confirm_zero`) | `zero_confirmed_at`, `finalize` ledger, release резервов, `allocate_session` | 5 |
| E21 | PENDING_DELETE → LEAKED | `now − destroy_requested_at > 900` без нуля | инцидент `leak` critical, `block_provider(leak)`, `halt(leak)` | 18 |
| E22 | LEAKED → STOPPED | ноль подтверждён позже (сторож продолжает destroy) | как E20; HALTED и блок снимаются только вручную | 18 |

- Повторы destroy в STOPPING, PENDING_DELETE и LEAKED: `next_destroy_at = now + min(5·2^k, 120)`, до подтверждения нуля.
- Запрещённые рёбра (STOPPED → BUSY, PENDING_DELETE → READY и др.) бросают `IllegalTransition`; это проверяет тест.

## 6. Бюджет: формулы

Обозначения:
- `r = (price_usd_h + disk_usd_h) / 3600`;
- `T_work_j = in_j / pp + out_j / tg`, где `out_j` = `max_tokens` или `task_type_max_tokens[task_type]`, иначе `default`; pp и tg — EWMA (α = 0,3) по факту, старт — из профиля пула;
- `T_cold_p90` — p90 по последним 20 длительностям `created_at → ready_at` пула (метод nearest-rank); пока стартов меньше 3 — `cold_start_s_default` (540 с, замер);
- `T_idle` = `idle_timeout_s` пула.

Формулы:
- **E_launch** = `r · (T_cold_p90 + Σ_j T_work_j + T_idle) + traffic + storage`; в Mock `traffic` и `storage` равны 0, кроме тестов с `TRAFFIC_BILLED`.
- **Доля задания**: `share_j = r · T_work_j + r · (T_cold_p90 + T_idle) / n`, где n — число задач в пачке запуска. Должно быть `share_j ≤ budget_job`; иначе задача не идёт в облако (`budget_limit_reached`, fallback).
- **Задача на живой GPU**: резерв `job` = `r · T_work_j + r · max(0, T_idle − idle_reserved_remaining)` — простой не умножается на число задач (не n·T_idle): новая задача только продлевает уже зарезервированный простой до полного `T_idle`. Проверка: `session_cost + Σ резервов сеанса + резерв ≤ budget_session`, иначе `stop_after_current`.
- **Committed(окно)** = `spent(окно) + Σ_{открытые резервы} (amount − consumed)`. Каждое начисление инстанса (`accrue`) в той же транзакции списывает `consumed_usd` с его открытых резервов в порядке FIFO (session, затем job по `created_at`); остаток сверх резервов просто ложится в ledger. Здесь `spent` суммирует по провайдерам `max(Σ ledger source=estimate, Σ source=provider)` в окне. Начисления по ресурсам в PENDING_DELETE и LEAKED идут до `zero_confirmed_at`, поэтому они уже входят в `spent`.
- **check_launch** (все проверки обязательны, первая проваленная даёт причину):
  0. Проверка выполняется **внутри транзакции `insert_provisioning`** (через `gate`), чтобы HALTED, блокировка или чужой резерв, появившиеся между решением и вставкой, не пропускали запуск.
  1. HALTED → `controller_halted`.
  2. `is_blocked(provider)` → `insufficient_balance` / `quota_exceeded`.
  3. Ступень: `stage = max` по долям `Committed(month)/budget_month` и `Committed(day)/budget_day`. NO_LAUNCH (≥ 1,0) → `budget_limit_reached`. URGENT_ONLY (≥ 0,95) пропускает только `urgent`. ECONOMY (≥ 0,8) только отмечается (политику применяет router T-369).
  4. `Committed(day) + E ≤ budget_day`, `Committed(month) + E ≤ budget_month`, `E ≤ budget_session` → иначе `budget_limit_reached`.
  5. Для подбюджета: `Committed_sub(month) + E ≤ subbudgets[name]`.
  6. Если есть `BALANCE_API`: `E + reserve_usd ≤ last_balance` и `last_balance ≥ r · 3600 · min_balance_hours` → иначе `insufficient_balance` и `block_provider`. Повторов нет.
- **Резервы**:
  - `session` создаётся в E1 и закрывается в E20/E22;
  - `job` закрывается при завершении задачи;
  - `hold` = `r · ttl_s` проверяется сразу (`check_launch`-подобная проверка без T_cold, если инстанс жив). **Hold, чья стоимость до TTL больше `budget_session`, отклоняется при создании** структурированной ошибкой `HoldExceedsSessionBudget` (`budget_limit_reached`); разрешить — `leases.hold_may_exceed_session: true`;
  - `prewarm` = E_launch без задач.
- **Предупреждения** — инциденты severity warning с `dedup_key` = `budget:<window>:<level>:<period>`, на 50/80/95 % месяца и суток. **Темп**: `spent(month_to_date) > 1.25 · (day_of_month / days_in_month) · budget_month` → `budget_pace:<period>`.
- **100 %**: новых запусков нет; hold и prewarm закрываются; живой сеанс получает `stop_after_current`.
- **Сверка**: `|ours − provider| > max(0.05, 0.05 · provider)` → инцидент `ledger_divergence`. Дальше `spent` берёт максимум (см. выше).
- **Сторож независимо**: `spent(month)` плюс оценка живых BILLABLE до `now` ≥ `hard_cap_month` (50) → kill.
- **Задача не прошла проверку на живой GPU** (`check_job_on_running`, `budget_job`) → `fallback_local` или requeue по `queue_on_unavailable`; в обоих случаях её неявный lease закрывается, чтобы задача не держала GPU.
- **Распределение** (`allocate_session`):
  - `work_c` = Σ `cost_usd` задач клиента в сеансе;
  - `hold_idle` = `r · Σ` пересечений интервалов hold/prewarm-leases с периодами без задач `running`; относится клиенту-держателю (`clients.id` или `admin`);
  - остаток `shared = session_total − Σ work − hold_idle` делится пропорционально `work_c`;
  - если работы не было, весь `shared` относится держателю, а без держателя — `unattributed`.

## 7. Очередь, leases, MockProvider: параметры и проверки

### 7.1 Leases
| Вид | Создание | TTL | Конец | Особое |
|---|---|---|---|---|
| implicit | `submit` (если `pool` задан) | нет (живёт с задачей) | `complete`, `cancel`, `fallback_local`, terminal | одна на задачу (UNIQUE) |
| explicit | `create_explicit` | 600 с по умолчанию (30..3600); `renew` раз в ttl/3 | `close` / истёк TTL → инцидент `lease_expired` (info) | `renew` истёкшего → `LeaseExpired` (T-369: 410) |
| hold | `Controller.hold` (admin) | ≤ 28 800 с | `release`/TTL/kill/100 % бюджета | продлевает `max_lifetime_at` до `expires_at`; резерв `hold`; `budget_session` всё равно действует |
| prewarm | `create_explicit(prewarm=True)` | как explicit | как explicit | при пуле STOPPED — триггер E1 (с проверкой бюджета) |

- **drain**: `close(final=True)` — если после закрытия очередь пула пуста и живых leases нет, а инстанс в BUSY или IDLE, то в этом же `step()` выполняется E12, затем E14 с `reason=drain` (без `idle_timeout`).
- **Агент молчит 20 мин**: явный lease с `renew` каждые 200 с держит BUSY/READY; IDLE не наступает; HTTP-запросы при этом не учитываются вовсе.

### 7.2 Контракт адаптера (`contract.py`, C1–C10)
| # | Проверка |
|---|---|
| C1 | в классе нет атрибутов `stop`, `pause`, `start`, `resume` |
| C2 | `create(spec, label)` → `RemoteResource.label == label` и `ref` не пуст |
| C3 | `list_owned("llmhub-")` содержит созданный ресурс и не содержит чужих |
| C4 | `get(ref)` = RUNNING после `settle`; `get(unknown)` = None |
| C5 | `destroy` дважды → ACCEPTED, затем ACCEPTED/ALREADY_GONE; `destroy(unknown)` → ALREADY_GONE |
| C6 | после destroy и `settle` ресурса нет в `list_owned`; без `settle` ядро не считает ресурс удалённым (проверка: `confirm_zero` без settle → False при lag) |
| C7 | `normalize_error` любого исключения → `NormalizedError` с причиной из перечня; в `message` нет секрета |
| C8 | `BALANCE_API` ⇔ `balance()` не None; `BILLING_API` ⇔ `billing_since` отдаёт записи |
| C9 | `create` с неизвестным исходом → `ProviderError(outcome_unknown=True)`, без побочного повтора |
| C10 | адаптер не делает сетевых вызовов при импорте (`socket` запрещён в тесте) |

### 7.3 Сбои MockProvider и где они проверяются
| Сбой | Ожидаемое поведение | Тест |
|---|---|---|
| CREATE_TIMEOUT_CREATED | `list_owned(label)` находит ресурс → ADOPTED, ровно 1 ресурс | `test_create_unknown_adopt` |
| CREATE_TIMEOUT_NOT_CREATED | повтор с тем же label, `attempts = 2`, 1 ресурс | `test_create_unknown_retry_same_label` |
| CREATE_TIMEOUT_CREATED_LATE (+ lag) | повтор сначала создаёт 2-й ресурс → сверка находит дубль по label → лишний удаляется; итог — 1 | `test_create_unknown_late_duplicate` |
| 3× unknown | E3 после 2 попыток, задачи → fallback | `test_create_unknown_max_attempts` |
| RATE_LIMIT_429 / API_5XX на get | 3 повтора с бэкоффом; 3 ошибки за 10 мин → breaker open 15 мин → half_open | `test_breaker` |
| INSUFFICIENT_BALANCE | `block_provider`, без повторов, E3, fallback; рост баланса не снимает блок; `resume` снимает | `test_insufficient_balance_no_retry` |
| GPU_UNAVAILABLE | оффер исключён → следующий; нет офферов `offer_wait` → `gpu_unavailable` fallback | `test_gpu_unavailable_offer_wait` |
| slow boot (`boot_s` > 600) | E5 по таймауту → STOPPING | `test_booting_timeout` |
| model load fail (`test_fail = 3`) | E7, хост в blacklist, следующий запуск на другом хосте | `test_model_load_fail_blacklist` |
| hang_load | таймаут 20 мин → E7 | `test_loading_hang_timeout` |
| interrupt во время BUSY | E17, задача в очередь `attempts = 1`, новый цикл выполняет её | `test_gpu_interruption_requeue` |
| stopped_but_billing | E17 → destroy → STOPPED; ledger учитывает время | `test_stopped_but_billing` |
| DESTROY_FAIL ×N | повторы, E19 по `stop_confirm_s`, затем ноль | `test_destroy_retry_then_zero` |
| DESTROY_NEVER | E21 LEAKED через 15 мин: инцидент, блок, HALTED | `test_destroy_never_leaked` |
| orphan | сторож удаляет при `safe_mode` или строке STOPPED; без строки в обычном режиме — инцидент | `test_reaper_orphan_*` |
| чужой ресурс / плохой HMAC | не трогается никогда | `test_reaper_never_touches_foreign` |
| живой дубль после неизвестного исхода (ref ≠ provider_ref; лишние при NULL после adopt) | destroy + инцидент `duplicate_resource`; итог 1 ресурс | `test_live_duplicate_destroyed` |
| CREATE_GHOST_REF | строка переходит на ресурс по label, ghost — дубль | `test_create_ghost_ref` |
| CREATE_HANG + kill | kill → 0 ресурсов ≤ 60 с; ref, пришедший позже, уничтожен | `test_create_hangs_kill_zero` |
| kill во время create + `list_owned_lag_s` > grace | ref сохранён без CAS → сразу destroy; 0 ресурсов | `test_kill_during_create_lag` |
| DESTROY_429 | повтор по `retry_after`, breaker не открывается | `test_destroy_429_no_breaker` |
| DESTROY_PARTIAL | volume с label удалён отдельно; STOPPED только после нуля по всем видам | `test_destroy_partial_volume` |
| LIST_STALE | устаревший список не подтверждает ноль раньше времени (ноль нужен 2 раза подряд с интервалом ≥ staleness) | `test_list_stale_no_false_zero` |

## 8. Восстановление после перезапуска (`reconcile_on_start`)
Перед восстановлением: новый `boot_id`; `open_state_db` (integrity) делает вызывающий код. Затем `list_owned` у всех включённых адаптеров и разбор каждой строки `LIVE_IN_INDEX ∪ {PENDING_DELETE, LEAKED}`:
- **PROVISIONING без `provider_ref`**: ресурс по label найден → adopt (E2 или остаётся в PROVISIONING до RUNNING); не найден → тот же путь, что у неизвестного исхода (повтор create с тем же label, `attempts` ≤ 2). Дубль, появившийся позже, отлавливает сверка по label.
- **BOOTING / LOADING_MODEL**: найден → продолжить; таймеры считаются от сохранённого `state_since`. Не найден → E5/E7.
- **READY / BUSY / IDLE**: найден и `health` = истина → IDLE с новым таймером (`idle_since = now`) или BUSY, если есть очередь. Не найден → E17. Найден, но `health` = ложь → ERROR → STOPPING.
- **STOPPING / PENDING_DELETE / LEAKED**: продолжить teardown.

Задачи и leases:
- задачи `running` с `worker_id ≠ boot_id` → `requeue(count_attempt=False)` сразу (`restarts++`): пульса старого процесса нет, а перезапуск — не вина задачи;
- просроченные leases → `expire_due`.

Ресурсы с верным HMAC без строки обрабатываются по правилам сторожа (§3.10). В `safe_mode` восстанавливаются задачи из `BodyStore.iter_recoverable()`, у которых нет результата.

## 9. Шаги (TDD) и потоки

Команда проверки каждого шага: `uv run pytest tests/<файлы шага> -q && uv run ruff check src tests && uv run pyright`. Тесты лежат в `tests/` плоско, с префиксом потока. Общий `tests/conftest.py` принадлежит потоку A; фикстуры потоков — в `tests/<поток>_fixtures.py`, их подключают через `pytest_plugins` в своих тестах.

### Поток A — фундамент (sonnet). Владеет: `clock.py`, `configuration/*`, `persistence/*`, `tests/conftest.py`, `tests/test_a_*.py`, раздел `[tool.pytest.ini_options]` в `pyproject.toml` (marker `slow`, `asyncio_default_fixture_loop_scope = "function"`)
| Шаг | Сначала тест | Реализация |
|---|---|---|
| [x] A1 | `test_a_clock.py`: FakeClock — `sleep_until` будится по `advance_to` в порядке времени; `next_waiter` | `clock.py` |
| [x] A2 | `test_a_config.py`: умолчания (idle 300, low 5/1800/5400, лимиты R3.5); idle 901 → ConfigError; hold > 28800 → ошибка; секрет < 32 байт → ошибка; `repr` без секрета | `configuration/models.py`, `loader.py` |
| [x] A3 | `test_a_schema.py`: `migrate` на пустой БД → `user_version` = 1, все 13 таблиц; повторный `migrate` — noop; WAL включён; CHECK отвергает неверный state | `migrations/0001_initial.sql`, `db.py` |
| [x] A4 | `test_a_unique_pool.py`: 2-я живая строка пула → IntegrityError; при PENDING_DELETE/LEAKED/STOPPED вставка новой PROVISIONING проходит; 10 потоков × свои соединения → ровно 1 вставка | (DDL) |
| [x] A5 | `test_a_tx.py`: `tx` откатывает при исключении; `assert_not_in_tx` бросает внутри `tx`; конкуренция: соединение сторожа держит запись → контроллер (300 мс) получает `Busy`, действие переносится на следующий `step()`; сторож (5000 мс) дожидается | `db.py` |
| A6 | `test_a_integrity.py`: испорченный файл (перезапись байтов страницы) → `safe_mode`, `halted`, инцидент, старый файл переименован; `backup` создаёт копию, хранит 7 | `integrity.py`, `backup.py`, `state.py` |

### S0 — сквозной каркас (sonnet, один implementer, после A1–A3, до параллельных B–E) — [x] выполнен 2026-11-04
Минимальные версии с финальными сигнатурами: `MockProvider` (create/get/list_owned/destroy без сбоев), `MockInference` (ping/health/test_request), `states.TRANSITIONS` + `machine.transition`, `Controller.step()` с одним проходом, `tests/harness.py`. Тест `test_s0_skeleton.py`: одна задача → PROVISIONING → BOOTING → LOADING_MODEL → READY → BUSY → IDLE → STOPPING → PENDING_DELETE → STOPPED на FakeClock. Дальше потоки расширяют свои файлы из S0 (владение — как в таблицах потоков), а harness переходит к F.

После S0 остальные потоки стартуют параллельно: интерфейсы A фиксированы этим планом.

### Поток B — адаптеры и Mock (sonnet). Владеет: `provider_adapters/*`, `cloud_manager/inference.py`, `tests/test_b_*.py`, `tests/b_fixtures.py`. Зависит от A1
| Шаг | Тест | Реализация |
|---|---|---|
| B1 | `test_b_types.py`: dataclass-ы frozen; у `ProviderAdapter` нет `stop` | `base.py`, `cloud_manager/inference.py` |
| B2 | `test_b_labels.py`: формат, verify, подделанный hmac → False, чужой префикс → None | `labels.py` |
| B3 | `test_b_mock_provider.py`: create/get/list_owned/destroy, начисления посекундно, баланс, каждый Fault из §7.3 на уровне адаптера, `list_owned_lag_s`, `destroy_delay_s`, общий `MockStore` из 2 соединений (файл) | `mock_store.py`, `mock.py` |
| B4 | `test_b_mock_inference.py`: `load_s`, `test_fail`, `hang_load`, `run` по времени, `InstanceLost` при interrupt, `NetworkLost` | `mock_inference.py` |
| B5 | `test_b_contract.py`: `run_contract(MockHarness)` проходит C1–C10; «сломанный» адаптер (с `stop`, без идемпотентного destroy) падает на C1/C5 | `contract.py` |
| B6 | `test_b_guarded.py`: повторы GET 3× через FakeClock; create не повторяется; таймаут вызова 20 с; `assert_not_in_tx`; breaker closed → open → half_open; при open `destroy`/`get`/`list_owned` проходят, `create`/`list_offers` — нет; ошибки с `actor=reaper` не считаются | `guarded.py` |

### Поток C — автомат, leases, teardown (sonnet; ревью ×2). Владеет: `lifecycle_manager/*`, `tests/test_c_*.py`. Зависит от A, B1
| Шаг | Тест | Реализация |
|---|---|---|
| C1 | `test_c_states.py`: `TRANSITIONS` совпадает с §5 (E1–E22, перечислены явно); `check_edge` на запрещённых рёбрах бросает | `states.py` |
| C2 | `test_c_machine.py`: CAS — 2 соединения переводят одну строку, побеждает одно; строка `transitions` пишется; `insert_provisioning` при живой строке → None | `machine.py` |
| C3 | `test_c_leases.py`: все виды из §7.1, `renew` истёкшего, `expire_due` → инцидент, hold > 8 ч → ошибка, живой счёт по `now` | `leases.py` |
| C4 | `test_c_idle_tx.py`: `try_idle_to_stopping` — пусто → stopping; задача, вставленная другим соединением до транзакции → busy; lease → busy; гонка: поток A вставляет задачу, поток B решает остановку, 200 повторов — нет ни одного «STOPPING при непустой очереди» | `machine.py` |
| C5 | `test_c_teardown.py` (Mock): STOPPING → PENDING_DELETE по ACCEPTED и по `stop_confirm_s`; `confirm_zero` с lag; без `provider_ref` — ожидание grace; `mark_leaks` → LEAKED, блок, halt; LEAKED → STOPPED | `teardown.py` |
| C6 | `test_c_recovery.py`: каждая ветка §8 (сценарии собираются прямыми вставками в БД и Mock) | `recovery.py` |

### Поток D — очередь и триггеры (sonnet). Владеет: `task_queue/*`, `scheduler/*`, `tests/test_d_*.py`. Зависит от A, B1 (типы `UnavailableReason`, `JobRunResult`), C3 (`create_implicit`, `close_for_job`)
| Шаг | Тест | Реализация |
|---|---|---|
| D1 | `test_d_bodies.py`: права 0700/0600, атомарная запись, `iter_recoverable`, `purge` | `bodies.py` |
| D2 | `test_d_submit.py`: явный ключ → та же задача; другое тело → `IdempotencyConflict`; ключ через 7 сут → новая задача; неявная дедупликация в очереди, ≤ 15 мин после и > 15 мин; 10 потоков с одним ключом → 1 строка | `queue.py` |
| D3 | `test_d_pick.py`: urgent > normal > low; round-robin 3 клиента (A,A,A,B,C → A,B,C,A,A) | `queue.py` |
| D4 | `test_d_requeue_cancel.py`: `requeue` с/без `attempts++`, на `max` → fallback; `cancel` из очереди → lease закрыт; `requeue_orphaned_running` по `boot_id`; `purge_expired` | `queue.py` |
| D5 | `test_d_triggers.py` (чистые функции): urgent → сразу; 4 low → нет, `wake_at` = oldest + 1800; 5 low → да; дедлайн с T_cold + T_work + margin; normal → сразу; prewarm; пустая очередь | `scheduler/triggers.py` |

### Поток E — бюджет и учёт (sonnet; ревью ×2). Владеет: `budget_manager/*`, `cost_tracker/*`, `tests/test_e_*.py`. Зависит от A, B1, C2 (`transitions` для p90)
| Шаг | Тест | Реализация |
|---|---|---|
| E-1 | `test_e_estimate.py`: формула E_launch на числах замера (0,22 $/ч; 540 с; 6000/8000 ток; pp 1200, tg 78; idle 300) = 0,22/3600·(540 + 5 + 102,6 + 300) ≈ $0,0579; p90 nearest-rank; EWMA | `estimate.py` |
| E-2 | `test_e_ledger.py`: `accrue` идемпотентен (UNIQUE), BILLABLE включая PENDING_DELETE/LEAKED, `finalize`, `spent` = max(estimate, provider) | `ledger.py` |
| E-3 | `test_e_reservations.py`: `consume_fifo` и `remainder`; `Committed` без двойного счёта (резерв 1,0, начислено 0,3 → 1,0, а не 1,3); сеанс + 3 задачи на живой GPU: суммарный резерв простоя = один `T_idle`, а не 3·T_idle | `reservations.py`, `limits.py` |
| E-4 | `test_e_limits.py`: hold дороже `budget_session` → `HoldExceedsSessionBudget` (с флагом — разрешён); сутки, месяц, сеанс, задание, подбюджет `exception` $10; ступени 0,79/0,80/0,95/1,0 → NORMAL/ECONOMY/URGENT_ONLY/NO_LAUNCH; баланс < E + reserve → `insufficient_balance`; окна по TZ | `limits.py` |
| E-5 | `test_e_warnings.py`: 50/80/95 % по одному разу (dedup); темп | `limits.py` |
| E-6 | `test_e_accounts.py`: `block_provider` → `is_blocked`; рост баланса блок не снимает; `resume_provider` снимает | `accounts.py` |
| E-7 | `test_e_reconcile.py`: Mock-биллинг против ledger: расхождение 0,04 → нет инцидента; 0,06 при $0,5 → инцидент; дальше лимиты по максимуму | `reconcile.py` |
| E-8 | `test_e_allocation.py`: 2 клиента 1:3 работы → shared 1:3; hold без работы → держателю; hold + работа | `allocation.py` |

### Поток F — интеграция (opus). Владеет: `cloud_manager/{pools,offers,provisioner}.py`, `controller.py`, `killswitch.py`, `reaper/*`, все `__init__.py` с экспортами пакетов, `tests/test_f_*.py`, `tests/harness.py`, `tests/fixtures/sim/*`, обновление `tests/test_smoke.py` (новые модули). Зависит от A–E
| Шаг | Тест | Реализация |
|---|---|---|
| F1 | `test_f_offers.py`: цена, blacklist, `max_price`, пусто → None | `offers.py`, `pools.py` |
| F2 | `test_f_provisioner.py`: путь PROVISIONING → READY; неизвестный исход — 4 ветки §7.3; `insufficient_balance`; `gpu_unavailable` / `offer_wait` | `provisioner.py` |
| F3 | `tests/harness.py` + `test_f_harness.py`: `Harness(cfg, knobs)` собирает FakeClock, Mock, Controller; `run_until`, `arrive_at(t, spec)`; детерминизм (2 прогона → одинаковые `transitions`) | `controller.py` (step, submit, run_forever) |
| F4 | `test_f_transitions.py`: параметризован по `TRANSITIONS` — на каждое ребро E1–E22 свой сценарий через Harness, ассерт строки `transitions`; guard-тест: множество id сценариев == множеству рёбер; E11 (BUSY → BUSY, без строки `transitions`) проверяется отдельно: 2 задачи подряд без IDLE и без второго create | `controller.py` |
| F5 | `test_f_situations.py`: 19 ситуаций §6.3 — по тесту на каждую (таблица §10.2) | — |
| F6 | `test_f_tz15.py`: сценарии ТЗ §15 (§10.1) | — |
| F7 | `test_f_killswitch.py`: 3 ресурса (один с `destroy_delay_s` 20 с, один orphan) → 0 ≤ 60 с FakeClock; KILL-файл; create висит (CREATE_HANG) + kill → 0 ≤ 60 с; kill во время create при lag > grace; после kill новых запусков нет, `resume` снимает | `killswitch.py` |
| F8 | `test_f_reaper.py`: `run_once` — PENDING_DELETE → STOPPED, LEAKED, orphan в safe_mode, живой дубль, без строки в обычном режиме (critical + HALT, через 30 мин удаление), чужой не тронут, hard_cap при мёртвом контроллере (accrue по CAS), max_lifetime-предохранитель; «только сторож»: контроллер остановлен, KILL-файл → 0 ресурсов ≤ 60 с при `--loop 15`; `test_f_reaper_process.py` (marker `slow`): `subprocess` `python -m llmhub.reaper --once` на файловом MockStore и state.db (SystemClock, задержки 0) удаляет orphan | `reaper/core.py`, `__main__.py` |
| F9 | `test_f_sim_parity.py` (marker `slow`): трассы S1–S6, RF2, RF4, RF6 из `tests/fixtures/sim/` → Harness с `deadline_includes_work=False` → число запусков **точно** равно эталону, $ в пределах ±5 %. Тест обязан пройти; «описать расхождение вместо прохождения» не допускается | — |
| F10 | обновить `test_smoke.py`; полный `uv run pytest`, ruff, pyright | экспорты `__init__` |

**Фикстуры симулятора (F9).** Скрипт в scratchpad (не в репозитории, без импорта в llmhub) загружает `/var/www/projectVoiceBot/tools/bench/T-367-sim/sim.py` по пути. Для 3 seeds и стратегий `idle 5m` и `batch 5/30m drain` (S1–S6 — Params заказчика: $0,36/ч, старт 300 с; RF — конфигурация «старт 9 мин», 0,22 $/ч) он выгружает JSON: заголовок (sha256 sim.py, команда, дата), заявки (`t`, `prio`, `in`, `out`, `max_wait_s`, `agent`), стратегию и эталон `simulate()` (launches, total $). Настройки Harness для паритета:
- `boot_s + load_s = startup_s`, `provisioning` 0, destroy мгновенный, lag 0;
- `pp`/`tg` = `Params`; `max_lifetime_s = 86400`; бюджет снят (лимиты 1e6);
- `deadline_margin_s = 0`, `deadline_includes_work = False`, `max_concurrency = 1`;
- агенты S6 → клиенты; `max_wait_s` → `max_queue_wait_s`.

### Порядок и параллельность
```
A1–A3 ─► S0 ─┬─► B (B1 сначала) ──────────────┐
        ├─► A4–A6                        │
        ├─► C (после B1) ─┬──────────────┤
        ├─► D (после C3) ─┤              ├─► F1–F10
        └─► E (после C2) ─┘──────────────┘
```
Три–четыре implementer-а одновременно: B, C, D, E (A заканчивает A4–A6 параллельно с ними). F — один implementer (opus) после слияния B–E. F1–F2 можно начать, когда готовы B и C5.

## 10. Список тестов-сценариев

### 10.1 ТЗ §15 и критерии задачи (`test_f_tz15.py`)
| Тест | Проверка |
|---|---|
| `test_10_concurrent_requests_one_create` | 10 `submit` через `asyncio.gather` + 10 потоков с отдельными соединениями, вызывающих `step` → `mock.create_calls == 1`, все 10 выполнены |
| `test_several_agents_urgent_skips_batch` | 3 клиента: 4 low ждут пакета, urgent запускает пул сразу; low раздаются на живой GPU; round-robin |
| `test_retries_same_idempotency_key_one_job` | 5 повторов → 1 задача и 1 ответ; другое тело → 409 |
| `test_restart_in_provisioning_no_extra_resources` | Controller уничтожается после `insert_provisioning` и до/после create (обе ветки) → новый Controller → 1 ресурс, задача выполнена |
| `test_restart_in_busy_no_loss` | рестарт во время `run` → задача requeue без `attempts++` (`restarts = 1`), выполнена до конца (at-least-once), ресурс подхвачен, не пересоздан; 3 рестарта подряд не переводят задачу в fallback |
| `test_gpu_interruption_requeue` | interrupt в BUSY → `attempts = 1` → новый цикл → completed |
| `test_request_during_stopping_new_cycle` | задача приходит в STOPPING → после PENDING_DELETE новый PROVISIONING; в индексе ≤ 1 живой; оба ресурса учтены в ledger |
| `test_new_cycle_while_pending_delete` | DESTROY_FAIL → `stop_confirm_s` → PENDING_DELETE + новый цикл; UNIQUE не нарушен; старый в итоге STOPPED |
| `test_budget_exhaustion_no_launch_local_fallback` | Committed ≥ 100 % → create не вызывается, задачи `route=local:fallback`, `reason=budget_limit_reached` |
| `test_balance_exhaustion_block_manual_resume` | INSUFFICIENT_BALANCE → 1 вызов create, блок; новые задачи create не вызывают; рост баланса не помогает; `resume` → запуск |
| `test_no_extra_launches` | 30 задач потоком при живом GPU → 1 запуск; S2-подобный всплеск → число запусков = число всплесков |
| `test_idle_shutdown_after_timeout` | последняя задача → IDLE → через 300 с STOPPING → STOPPED; задача на 299-й секунде → BUSY без старта |
| `test_idle_timeout_zero_and_15` | idle 0 → сразу STOPPING; idle 900 |
| `test_drain_after_batch` | `close(final=True)` → STOPPING без ожидания idle |
| `test_agent_silent_20min_with_lease_gpu_alive` | lease + `renew` каждые 200 с, 20 мин без задач → не IDLE, не STOPPING; после `close` — idle → stop |
| `test_lease_expired_without_heartbeat` | без `renew` → через 600 с lease истёк, инцидент, IDLE |
| `test_create_unknown_outcome_one_resource` | 3 ветки §7.3 → `len(mock.live_refs()) == 1` |
| `test_db_corruption_safe_mode` | порча state.db → HALTED, safe_mode; сторож удаляет все HMAC-ресурсы; чужой цел; задачи восстановлены из тел; новых create нет; `resume` без сверки → `ReconcileRequired`; после сверки ledger содержит `source=provider` с начала месяца и бюджет месяца не обнулён |
| `test_blocked_job_doesnt_hold_gpu` | задача не прошла `check_job_on_running` → fallback/requeue, lease закрыт, GPU уходит в IDLE → STOPPED |
| `test_insert_provisioning_gate_in_tx` | HALT/блок, выставленные другим соединением между решением и вставкой → строки нет, create не вызван |
| `test_kill_zero_resources_within_60s` | (F7) |
| `test_max_lifetime_stop_after_current` | BUSY на 3 ч → после текущей задачи STOPPING; hold продлевает до TTL |
| `test_budget_session_stop_after_current` | сеанс достиг $1 → `stop_after_current` |
| `test_ledger_vs_mock_billing_within_tolerance` | день S1 → ⎮ledger − mock.billed⎮ ≤ max(0,05; 5 %) |
| `test_sim_parity_*` | (F9) |

### 10.2 19 ситуаций §6.3 (`test_f_situations.py`, по тесту)
1. `s01_first_job_stopped` — строка instances есть **до** вызова create (spy в Mock проверяет БД в момент create), резерв session, неявный lease.
2. `s02_requests_during_provisioning` — 5 задач в PROVISIONING → 1 create, раздача по приоритету.
3. `s03_job_during_busy` — очередь, затем E11 без IDLE.
4. `s04_job_in_idle` — E13, нет нового create.
5. `s05_job_during_stopping` — destroy не отменён; новый цикл после PENDING_DELETE.
6. `s06_create_unknown` — ветки adopt / retry / max.
7. `s07_gpu_unavailable` — следующий оффер; `offer_wait` → fallback `gpu_unavailable`.
8. `s08_deploy_or_load_error` — 3 провала теста → ERROR → blacklist → другой хост; лимит попыток.
9. `s09_instance_crash_busy` — requeue, `attempts = max` → fallback.
10. `s10_loading_hang` — таймаут LOADING_MODEL.
11. `s11_gateway_instance_link_loss` — `NetworkLost` 3 мин → E17, destroy.
12. `s12_client_link_loss` — задача доведена, повтор с ключом → готовый результат (результат хранится 7 сут).
13. `s13_controller_restart` — подхват READY → IDLE, orphan, `running` → requeue, истёкшие leases.
14. `s14_long_agent_task_lease` — то же, что silent 20 min.
15. `s15_stale_leases` — TTL, инцидент; hold ≤ 8 ч.
16. `s16_duplicate_request` — idempotency (явная и неявная).
17. `s17_db_corruption` — safe mode.
18. `s18_destroy_429_error` — повторы до 15 мин → LEAKED, тревога, блок, HALTED.
19. `s19_funds_quota_budget` — `insufficient_balance`, `quota_exceeded`, `budget_limit_reached` → fallback/очередь по `queue_on_unavailable`.

## 11. Риски и вопросы
| # | Риск / вопрос | Блокирует? | Решение в плане |
|---|---|---|---|
| Q1 | Ресурс с верным HMAC без строки в БД при обычном режиме | решено координатором | Инцидент critical + HALT новых запусков + сверка каждые 15 мин; через 30 мин — автоудаление, если HMAC проверяется текущим `local_secret_id` (деньги важнее; заказчик уведомлён) |
| Q2 | Противоречие §6.3 (STOPPING → ERROR) и ворот (PENDING_DELETE/LEAKED) | нет | Решено по воротам (§2 п. 6) |
| Q3 | Окна бюджета: сутки и месяц в какой TZ | решено | `budget.timezone`, по умолчанию UTC (заказчик может сменить) |
| Q4 | Порча БД: автовосстановление из суточной копии? | решено | Нет; safe_mode, обязательная сверка с биллингом провайдера до снятия HALTED |
| Q5 | Паритет с симулятором: триггер дедлайна и начало начислений | решено | флаг `deadline_includes_work=False` для паритета; запуски — точно, $ — ±5 %; тест обязан пройти |
| R1 | FakeClock и asyncio: задачи `run` должны будиться детерминированно | — | `advance_to` будит ожидающих по порядку + `await asyncio.sleep(0)` до стабилизации; тест детерминизма F3 |
| R2 | Медленные тесты (S4: 480 задач/сут) | — | marker `slow`, 3 seeds; цель — < 60 с на весь F9 |
| R3 | Гонка контроллер ⇄ сторож на одной строке | — | только CAS-переходы; сторож пишет только teardown-рёбра, инциденты, halt и блокировки |
| R4 | `max_lifetime_s` для паритета > 3 ч | — | боевой валидатор ≤ 10800; тест паритета обходит его override-ом |

## 12. Приёмка (соответствие критериям задачи)
| Критерий | Где проверяется |
|---|---|
| Схема §6.6, миграции, integrity_check, бэкап, порча → HALTED | A3, A6, `test_db_corruption_safe_mode`, s17 |
| Частичный UNIQUE, новый цикл при PENDING_DELETE | A4, `test_new_cycle_while_pending_delete` |
| Автомат, PENDING_DELETE/LEAKED, таймауты, max_lifetime, IDLE → STOPPING в транзакции, каждое ребро и строка §6.3 | C1, C4, C5, F4 (guard-тест рёбер), F5 (s01–s19) |
| Leases всех видов, 20 мин с lease, drain, idle 0–15 / 5 | C3, A2, `test_agent_silent_20min…`, `test_drain_after_batch`, `test_idle_*` |
| Очередь, пакеты low, round-robin, Idempotency 7 сут, неявная ~15 мин, 409, триггеры §6.7 | D2, D3, D5, `test_several_agents…` |
| Бюджет: оценка, резервы, лимиты, предупреждения, ступени, баланс, распределение | E-1 … E-6, E-8, `test_budget_*`, `test_balance_*` |
| Ledger, сверка ≤ max($0,05; 5 %) | E-2, E-7, `test_ledger_vs_mock_billing…` |
| Сторож отдельным процессом, kill ≤ 60 с, HMAC + БД, восстановление по `list_owned` | F7, F8 (+ subprocess), C6 |
| `ProviderAdapter` без stop, Mock со сбоями, контракт | B1–B6 |
| Сценарии ТЗ §15 | F6 (§10.1) |
| S1–S6 / RF2–RF6 ±5 % | F9 |
| pytest, ruff, pyright, ревью | F10 + §13 |

## 13. Ревью и критерии остановки (ТЗ §18, CLAUDE.md)
- **Цикл по под-объёмам**: A+B, C, D+E, F — у каждого свой бюджет ≤ 3 раундов ревью. implementer закрывает под-объём → reviewer → исправления → повторная проверка; после 3 раундов — список нерешённого заказчику.
- **Два независимых цикла ревью** (разные экземпляры reviewer, не видят выводов друг друга):
  - (1) логика и гонки: C2, C4, D2, E-3, E-4, F2, F4;
  - (2) сбои и перезапуски: C5, C6, F2 (unknown create), F5 s13/s17/s18, F7, F8.
- **Остановка** (все условия):
  - тесты задачи зелёные; `ruff check` и `pyright` чисто;
  - guard-тест F4 подтверждает покрытие всех рёбер E1–E22; все 19 ситуаций и все сценарии §10.1 есть и проходят;
  - у ревьюера нет открытых blocker/major.
- Находки каждого раунда записываются в журнал задачи («Ревью» и «Журнал»).
- **Перед implementer** — ворота critic + advocate по этому плану (ADR-0139, P1).

## 14. Ворота 2026-11-04: critic + advocate
По ADR-0139 внесены изменения (решения координатора — в таблице §11):
- **critic high — живой дубль после create с неизвестным исходом**: правило дублей (`ref ≠ provider_ref`; лишние при NULL после adopt) в сверке контроллера и у сторожа → destroy + инцидент; тест `test_live_duplicate_destroyed` (§3.9, §3.10, §7.3).
- **critic medium**:
  1. `set_provider_ref` без CAS; если state ≠ PROVISIONING — сразу destroy; тест kill во время create при lag > grace.
  2. Проверки HALTED, блокировки и `check_launch` — в транзакции `insert_provisioning` (`gate`).
  3. Breaker ограничивает только `create`/`list_offers`; ошибки сторожа его не открывают.
  4. Начисления списывают резервы FIFO (`consumed_usd`); `T_idle` новой задачи только продлевает уже зарезервированный простой; тест «сеанс + 3 задачи».
  5. Сторож начисляет по CAS `accrued_to` и считает `hard_cap` с оценкой живых до `now`.
  6. После порчи БД — обязательная сверка с биллингом с начала месяца (`source=provider`) до снятия HALTED.
  7. Перезапуск контроллера не считается попыткой (`restarts`).
  8. Задача, не прошедшая проверку на живой GPU → fallback/requeue с закрытым lease; тест.
  9. Сбои Mock DESTROY_PARTIAL, LIST_STALE, DESTROY_429, CREATE_GHOST_REF (+ CREATE_HANG); ноль — по всем `ResourceKind`; C6 и без `settle`.
  10. `reaper --loop` по умолчанию 15 с; тест «только сторож: KILL → 0 ≤ 60 с».
  11. Паритет: флаг `deadline_includes_work`; запуски точно, $ ±5 %, тест обязан пройти.
- **critic low**: боевой `max_lifetime_s` ≤ 10800 (паритет — override); при восстановлении ресурс найден, но `health` = ложь → ERROR → STOPPING; `Capability.SPEND_RATE` — расширение, отметить в журнале.
- **advocate**:
  - (a) шаг S0 — сквозной каркас до параллельных потоков;
  - (b) бюджет ревью по под-объёмам A+B, C, D+E, F, у каждого ≤ 3 раундов;
  - (c) `busy_timeout` контроллера 300 мс с переносом действия, сторож — 5000; тест A5;
  - (d) таймаут вызова 20 с, create/destroy фоновыми задачами с намерением в БД, KILL в начале и конце `step()`; тест «create висит, kill → 0 ≤ 60 с»;
  - (e) E11 проверяется отдельно в F4.
- **Решения по вопросам**: Q1 (critical + HALT + сверка каждые 15 мин, автоудаление через 30 мин при совпадении `local_secret_id`); Q3 (TZ в конфиге, UTC); Q4 (принято со сверкой до снятия HALTED); hold дороже `budget_session` отклоняется при создании (флаг-override).
