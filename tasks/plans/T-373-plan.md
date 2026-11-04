# T-373 — план: MCP-сервер `llmhub_ask` для агентов

> TL;DR: Тонкий stdio MCP-сервер (`mcp==2.2.0`, `MCPServer`) в `src/llmhub/mcp_server/` — клиент HTTP API llmhub (`POST /v1/tasks` → опрос → `/result`), без импортов остального `llmhub.*` и projectVoiceBot. Четыре инструмента: `llmhub_ask`, `llmhub_result`, `llmhub_cancel`, `llmhub_models`. Файлы — только внутри `LLMHUB_MCP_ROOTS` (realpath, без dot-компонентов, без перезаписи), ключ из файла 600 и нигде не выводится, `data_class=private` по умолчанию (облако — параметр + env), `Idempotency-Key` из содержимого. Проверки результата — свои (`checks.py`), виды — `kinds.yaml`. Тесты: in-process против `ApiEnv` (ASGI, FakeClock) + один `slow` живой stdio-тест против `LiveStack`. 16 шагов, потоки A, B, C → D → E. Задача P2 — ворота critic + advocate не обязательны; план к ним готов, если координатор решит провести. Блокирующих вопросов нет.

Задача: `tasks/active/T-373-mcp-server.md`. Контракт: `docs/integration.md` §2, §4, §6; `docs/openapi.yaml` (`/v1/tasks*`, `TaskStatus`, `ChatCompletion`, объект `error`); план T-369 §4, §11. Образец синтаксиса проверок (не импортировать): `/var/www/projectVoiceBot/tools/llm/extllm_checks.py`. Правило делегирования: `/var/www/projectVoiceBot/platform/docs/llm-external.md` «Правило применения».

## 1. Объём и что не входит

**Входит:** пакет `llmhub.mcp_server` (stdio-точка входа, инструменты, HTTP-клиент, безопасность файлов, ключ, проверки, виды), extra `mcp` в `pyproject.toml`, тесты `tests/test_m_*.py`, раздел «MCP для агентов» в `docs/integration.md` (регистрация в `.mcp.json`, переменные, таймауты, что делегировать).

**Не входит:**
- Правка любых `.mcp.json`, `.claude/settings*.json` в projectVoiceBot, gameDev, llmhub — только документация (регистрирует заказчик/координатор отдельной задачей).
- Перевод `tools/llm/ask.py` на llmhub, миграция пилотных кейсов — отдельная задача после T-371.
- Chunking (`concat`/`map-reduce`), конвейер R→G→V (`pipeline.py`), автоповтор при провале проверок, «исправление» ответа — нет (эскалация к Claude по правилу пилота).
- Проверки, требующие источника (`numbers:`, `ini-keys:`, `placeholders:`, `glossary:`) — v1 нет; добавляются позже новым видом проверки (вопрос Q3, не блокирует).
- Lease, SSE, `/v1/chat/completions` — MCP ходит только в `/v1/tasks` и `/v1/models`.
- Выдача клиентского ключа агентам и запись клиента `agents` в конфиг сервера — T-371 (деплой); здесь только описание.
- Любые платные действия, реальные провайдеры, обращения к 10.8.0.1 в тестах.

## 2. Ключевые решения

1. **Изоляция.** `llmhub.mcp_server` импортирует только stdlib, `httpx`, `mcp`, `mcp_types` (дистрибутив-спутник `mcp==2.2.0`, из него `ToolAnnotations`), `jsonschema`, `yaml`, `pydantic` (уже зависимость `mcp`; нужна для структурного ответа инструмента) и `llmhub.mcp_server.*`. Коды ошибок и статусы llmhub дублируются строковыми константами в `client.py` (не импорт `llmhub.api_gateway`). Тест импортов — по AST всех файлов пакета. `llmhub/__init__.py` содержит только `__version__` — безопасно.
2. **stdout — канал протокола.** Никаких `print`; логирование только в stderr, уровень `WARNING`, логгер `httpx`/`httpcore` — `WARNING` (на `INFO` httpx пишет URL). В логах — только код ошибки, `task_id`, размеры, хеши; ни текста запроса/ответа, ни путей вне корней, ни ключа.
3. **Ключ.** `LLMHUB_KEY` (env) или `LLMHUB_KEY_FILE` (по умолчанию `~/.config/llmhub/key`). Файл: обычный (не симлинк, `O_NOFOLLOW`), владелец — текущий uid, `mode & 0o077 == 0`, иначе отказ `key_unavailable` с причиной без пути содержимого. Ключ — объект `Secret` (`__repr__`/`__str__` → `"***"`), достаётся только при сборке заголовка. Читается лениво при первом вызове инструмента (сервер стартует и без ключа, `llmhub_models` вернёт `error.code=key_unavailable`). Документация: в `.mcp.json` — только `LLMHUB_KEY_FILE`, никогда `LLMHUB_KEY` (файлы `.mcp.json` бывают в git).
4. **Файлы.** Корни — `LLMHUB_MCP_ROOTS` (через `:`), каждый `realpath`; scratch `LLMHUB_MCP_SCRATCH` (по умолчанию `~/.local/state/llmhub-mcp/out`, создаётся 0700) **неявно добавляется** к корням. Путь от агента — только абсолютный (иначе `path_not_absolute`: сервер запускается через `uv --directory /var/www/llmhub`, его cwd не совпадает с проектом агента). Правила (§3.3): `realpath` внутри корня; ни один компонент пути **ниже корня** (и в исходном, и после realpath) не начинается с `.`; вход — обычный файл; выход — родитель существует внутри корня, файла нет или `overwrite=true`, сам выход не симлинк; запись атомарна (`O_CREAT|O_EXCL|O_NOFOLLOW` во временный файл в том же каталоге, 0600, `os.replace`). Сам корень может содержать точку (`~/.local/...`) — проверяется только хвост.
5. **Тело задачи.** `messages = [system(kind.system), user(текст)]`; текст пользователя = `input` + блоки файлов `<<<FILE {basename}>>>\n…\n<<<END FILE>>>` (только basename — путь не уходит в llmhub и не влияет на ключ). `max_tokens` — параметр, иначе вид; `think` вида → `chat_template_kwargs.enable_thinking`; `schema` → `response_format={"type":"json_schema","json_schema":{"name":"result","schema":…,"strict":true}}`; `task_type` — только если задан в виде (неизвестный серверу тип → `400`, поэтому во встроенных видах `task_type=None`). `model` — параметр (`auto` по умолчанию). Лимит входа `LLMHUB_MCP_MAX_INPUT_BYTES` (400 000) — сумма байтов `input` + файлов, проверка до чтения целиком (по `st_size`) и после.
6. **Idempotency-Key** = `"mcp-" + sha256(canonical)`, где `canonical = json.dumps({"body": body, "data_class", "priority", "policy", "nonce"}, sort_keys=True, separators=(",",":"), ensure_ascii=False)`. В тело уже входит содержимое файлов (не пути). `data_class`/`priority`/`policy` в хеше обязательны: на сервере они входят в каноническое тело, и тот же ключ с иными опциями дал бы `409 idempotency_conflict`. `fresh=true` → `nonce = secrets.token_hex(16)`, иначе `nonce=None`. Поведение сервера (integration §4): повтор того же запроса присоединяется к задаче; после `retryable`-провала тот же ключ запускает новую; после неповторяемого — та же ошибка (агенту — `fresh=true`).
7. **data_class.** Заголовок `X-LLMHub-Data-Class: private` всегда, кроме `data_class="cloud_allowed"` **и** `LLMHUB_MCP_ALLOW_CLOUD=1` в env сервера MCP; иначе — локальный отказ `cloud_not_enabled` без сети. Сервер llmhub всё равно сужает по конфигу клиента (рекомендация T-371: клиент `agents` с `cloud_allowed=false`). Поле тела `llmhub.data_class` не ставим (заголовок достаточен, меньше расхождений).
8. **Ожидание и повторы** (§3.5). Опрос `GET /v1/tasks/{id}` с паузой `clamp(eta_s/4, 0.5, 5)` до `wait_s`; `wait_s` по умолчанию `LLMHUB_MCP_DEFAULT_WAIT_S=90`, максимум `LLMHUB_MCP_MAX_WAIT_S=300`. Повторы HTTP — только `retryable` (`x-should-retry: true` / `error.retryable`) и транспортные ошибки, ≤ 2 повтора на вызов, пауза `min(Retry-After, остаток wait_s)`; `submit` повторяется с тем же `Idempotency-Key`. Не уложились → `verdict=pending` + `task_id` + `eta_s`. Задача на сервере не отменяется ни при `pending`, ни при отмене вызова MCP (разрыв ≠ отмена, как в API).
9. **Ответ.** Результат проверок решает Python: `ok` — все проверки прошли; `check_failed` — текст **не** пишется в `output_file`, а уходит в scratch `<task_id>.rejected.txt` (агент не должен случайно использовать его); `error` — ошибка API/задачи или `finish_reason="error"` (код `upstream_rejected`). Большой текст (> `LLMHUB_MCP_MAX_INLINE_BYTES`, по умолчанию 16 000 — ниже порога предупреждения Claude Code о больших ответах MCP) пишется в scratch `<task_id>.txt` (или в `output_file`), в ответе путь, `truncated_inline=true`, `text` = первые `LLMHUB_MCP_PREVIEW_BYTES` (2000) с обрезкой по границе UTF-8.
10. **Описания инструментов — главное место правила делегирования** (агент читает именно их; §6). Описание `llmhub_ask` прямо говорит: «только извлечение структуры по `schema` и классификация по явному полю; результат обязательно просматривает Claude; любой verdict ≠ ok → делай подзадачу сам, не чини ответ; секреты во вход не класть».

## 3. Файлы и интерфейсы

Пакет `src/llmhub/mcp_server/`:

| Файл | Поток | Что |
|---|---|---|
| `__init__.py` | E | докстринг, без экспорта логики |
| `__main__.py` | E | `main() -> None`: `McpSettings.from_env(os.environ)` → `build_server(...)` → `run("stdio")`; настройка логов в stderr |
| `settings.py` | A | `McpSettings` (из env) |
| `secret.py` | A | `Secret`, `load_key` |
| `paths.py` | A | `Roots`, `PathDenied` |
| `checks.py` | B | спецификации и исполнение проверок |
| `kinds.py`, `kinds.yaml` | B | виды |
| `request.py` | C | сборка тела, ключ идемпотентности |
| `client.py` | C | `HubClient` (httpx async), `HubError` |
| `service.py` | D | `ask/result/cancel/models` — логика без MCP |
| `models.py` | A | pydantic-модели ответа (`AskResult`, `ErrorInfo`, `CheckOut`, `Tokens`) |
| `server.py` | E | `build_server(settings, *, transport=None, sleep=None) -> MCPServer` |

### 3.1 `settings.py`
```python
@dataclass(frozen=True)
class McpSettings:
    url: str                    # LLMHUB_URL (обязателен; без него инструменты → error config_missing)
    key_env: str | None         # LLMHUB_KEY
    key_file: Path              # LLMHUB_KEY_FILE, default ~/.config/llmhub/key
    roots: tuple[Path, ...]     # LLMHUB_MCP_ROOTS split ":" (пустые и несуществующие — пропуск + предупреждение в stderr)
    scratch: Path               # LLMHUB_MCP_SCRATCH, default ~/.local/state/llmhub-mcp/out
    kinds_file: Path | None     # LLMHUB_MCP_KINDS
    allow_cloud: bool           # LLMHUB_MCP_ALLOW_CLOUD == "1"
    max_input_bytes: int = 400_000      # LLMHUB_MCP_MAX_INPUT_BYTES
    max_inline_bytes: int = 16_000      # LLMHUB_MCP_MAX_INLINE_BYTES
    preview_bytes: int = 2_000          # LLMHUB_MCP_PREVIEW_BYTES
    default_wait_s: float = 90.0        # LLMHUB_MCP_DEFAULT_WAIT_S
    max_wait_s: float = 300.0           # LLMHUB_MCP_MAX_WAIT_S
    http_timeout_s: float = 30.0        # LLMHUB_MCP_HTTP_TIMEOUT_S (один HTTP-запрос; ожидание — опросом)
    default_model: str = "auto"         # LLMHUB_MCP_MODEL

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> McpSettings: ...
```
Неверные числа → `ValueError` при старте (сообщение без значений env).

### 3.2 `secret.py`
```python
class Secret:
    def __init__(self, value: str) -> None: ...
    def reveal(self) -> str: ...
    def __repr__(self) -> str: return "Secret('***')"
    __str__ = __repr__

class KeyUnavailable(Exception):  # reason: "missing" | "not_regular" | "bad_owner" | "bad_mode" | "empty"
    reason: str

def load_key(key_env: str | None, key_file: Path) -> Secret: ...
```
Пустые строки и пробелы обрезаются; несколько строк → берётся первая непустая.

### 3.3 `paths.py`
```python
class PathDenied(Exception):
    code: str   # path_not_absolute | outside_roots | dot_component | not_regular_file | exists | parent_missing | symlink
    # str(exc) не содержит пути вне корней

@dataclass(frozen=True)
class Roots:
    roots: tuple[Path, ...]   # realpath, scratch включён
    scratch: Path

    @classmethod
    def build(cls, roots: Sequence[Path], scratch: Path) -> Roots: ...  # создаёт scratch 0700
    def resolve_input(self, raw: str) -> Path: ...
    def resolve_output(self, raw: str, *, overwrite: bool) -> Path: ...
    def read_text(self, path: Path, limit: int) -> str: ...   # O_NOFOLLOW, проверка размера, utf-8 (ошибка → PathDenied("not_utf8"))
    def write_text(self, path: Path, text: str, *, overwrite: bool) -> None: ...  # атомарно, 0600
    def scratch_file(self, name: str) -> Path: ...   # name = "<task_id>.txt" | "<task_id>.rejected.txt"; task_id фильтруется [A-Za-z0-9_-]
```
Алгоритм `_inside(raw)`: абсолютный → `os.path.normpath` → проверка dot-компонентов хвоста относительно подходящего корня (по нормализованному пути) → `os.path.realpath` → снова поиск корня (`Path.is_relative_to`) и снова dot-компоненты хвоста. Оба этапа нужны: `root/link → root/.env` ловится на втором, `root/.git/../a` — на первом.

### 3.4 `checks.py`
```python
@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    reason: str = ""      # без текста результата; только числа/имена

class CheckSpecError(ValueError): ...

Check = Callable[[str, str | None], CheckResult]   # (text, finish_reason)

def parse_check(spec: str) -> Check: ...
def build_checks(specs: Sequence[str], schema: dict[str, Any] | None, *, kind_defaults: Sequence[str]) -> list[Check]: ...
def run_checks(text: str, finish_reason: str | None, checks: Sequence[Check]) -> list[CheckResult]: ...
```
Синтаксис (свой код):
- `max-bytes:N` — `len(text.encode()) <= N`.
- `not-truncated` — `finish_reason != "length"`; **добавляется всегда**.
- `json-schema` — неявно при `schema`; разбор JSON (допускается одна обёртка ```` ```json … ``` ````), `Draft202012Validator`; причина — путь первой ошибки (`/items/3/type`), не значение.
- `sections:A,B` — каждая строка-заголовок встречается как отдельная строка (после `strip`), в указанном порядке.
- `item-count:N` — если текст JSON-массив → `len`; если JSON-объект с ровно одним массивом → его `len`; иначе число непустых строк.
- `script:<latin|cyrillic|han>:<ratio>` — доля букв нужного письма среди всех букв ≥ ratio (0..1).
- `regex:<re>` / `no-regex:<re>` — `re.search(..., re.MULTILINE)`; паттерн ≤ 300 символов, ошибка компиляции → `CheckSpecError`. Текст ограничен размером ответа; ReDoS — риск R5.
Неизвестная проверка, плохое число → `CheckSpecError` → инструмент возвращает `verdict=error`, `code=invalid_check` **до** отправки задачи.

### 3.5 `kinds.py` + `kinds.yaml`
```python
@dataclass(frozen=True)
class Kind:
    name: str
    description: str          # попадает в описание инструмента
    system: str
    max_tokens: int
    think: bool | None = None
    task_type: str | None = None
    checks: tuple[str, ...] = ()
    requires_schema: bool = False

def load_kinds(user_file: Path | None) -> dict[str, Kind]: ...  # встроенный kinds.yaml + переопределения пользователя по имени
```
Встроенные (`kinds.yaml` рядом с модулем, читается через `importlib.resources`): `extract_json` (`requires_schema=true`, `think=false`, 2000), `classify` (`think=false`, 1000; описание: «по явному полю, ответ — JSON с меткой на элемент»), `summarize` (1500), `translate` (`think=false`, 3000), `review` (`think=true`, 2000), `general` (1500). В описаниях `summarize`/`translate`/`review`/`general` — пометка «пилот: не отдавать в рабочие задачи без согласования» (§6). Схема YAML проверяется (неизвестные поля → ошибка старта). `requires_schema` без `schema` → `invalid_request` до сети.

### 3.6 `request.py`
```python
@dataclass(frozen=True)
class InputDoc:
    name: str    # basename
    text: str

def build_body(kind: Kind, input_text: str, docs: Sequence[InputDoc], *, model: str,
               schema: dict[str, Any] | None, max_tokens: int | None) -> dict[str, Any]: ...
def idempotency_key(body: Mapping[str, Any], *, data_class: str, priority: str | None,
                    policy: str | None, nonce: str | None) -> str: ...   # "mcp-" + 64 hex
```

### 3.7 `client.py`
```python
TERMINAL = frozenset({"completed", "failed", "cancelled"})

@dataclass
class HubError(Exception):
    code: str                 # коды integration §6 + transport | bad_response | key_unavailable
    reason: str | None
    retryable: bool
    retry_after_s: float | None
    task_id: str | None
    http_status: int | None

@dataclass(frozen=True)
class Submitted:
    id: str
    status: str
    duplicate: bool           # 200 вместо 202

@dataclass(frozen=True)
class TaskResult:
    body: dict[str, Any]      # chat.completion
    backend: str | None       # X-LLMHub-Backend
    fallback: str | None      # X-LLMHub-Fallback
    truncated: bool           # X-LLMHub-Truncated == "1"
    warnings: tuple[str, ...] # X-LLMHub-Warning

class HubClient:
    def __init__(self, base_url: str, key: Callable[[], Secret], *, timeout_s: float,
                 transport: httpx.AsyncBaseTransport | None = None,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 monotonic: Callable[[], float] = time.monotonic) -> None: ...
    async def submit(self, body: dict[str, Any], *, idem_key: str, data_class: str,
                     priority: str | None, policy: str | None, deadline_s: float | None) -> Submitted: ...
    async def status(self, task_id: str) -> dict[str, Any]: ...       # TaskStatus
    async def result(self, task_id: str) -> TaskResult: ...           # 409 task_not_ready → HubError(retryable)
    async def cancel(self, task_id: str) -> dict[str, Any]: ...
    async def models(self) -> list[dict[str, Any]]: ...
    async def wait(self, task_id: str, wait_s: float) -> dict[str, Any] | None: ...  # None = не дождались
    async def aclose(self) -> None: ...
```
- Ключ — `key()` вызывается в `_headers()` при каждом запросе (ленивая загрузка, ошибка → `HubError("key_unavailable")`).
- Разбор ошибки: тело `{"error": {...}}` → `HubError`; не JSON → `bad_response`; `retryable` = `x-should-retry` (если есть) иначе `error.retryable`; `retry_after_s` = `error.retry_after_s` или `Retry-After`.
- Транспорт (`httpx.TransportError`) → `HubError("transport", reason=type(exc).__name__, retryable=True)` — текст исключения не передаётся (в нём может быть URL; ключа там нет, но правило одно).
- `_request` повторяет только retryable, ≤ 2 раза, с паузой `sleep(min(retry_after or 1, budget))`; бюджет передаётся вызывающим (остаток `wait_s`).
- `wait`: цикл `status` → терминальный → вернуть; иначе `sleep(clamp(eta_s/4, 0.5, 5))` пока `monotonic() < deadline`. `sleep`/`monotonic` инъектируются для тестов на FakeClock.

### 3.8 `models.py` (ответ инструмента, pydantic)
```python
Verdict = Literal["ok", "check_failed", "pending", "error"]

class ErrorInfo(BaseModel):
    code: str; reason: str | None = None; retryable: bool = False; retry_after_s: float | None = None

class CheckOut(BaseModel):
    name: str; ok: bool; reason: str = ""

class Tokens(BaseModel):
    prompt: int | None = None; completion: int | None = None

class AskResult(BaseModel):
    verdict: Verdict
    task_id: str | None = None
    status: str | None = None           # статус задачи llmhub
    eta_s: float | None = None          # при pending
    text: str | None = None             # весь текст или превью
    output_file: str | None = None
    truncated_inline: bool = False
    truncated: bool = False             # finish_reason=length
    finish_reason: str | None = None
    checks: list[CheckOut] = []
    backend: str | None = None
    fallback: str | None = None
    warnings: list[str] = []
    tokens: Tokens = Tokens()
    error: ErrorInfo | None = None
    hint: str | None = None             # «вызови llmhub_result(task_id)», «сделай подзадачу сам»
```
Тест: `AskResult.model_dump_json()` любого ответа не содержит ключа (поиск подстроки ключа и `Bearer`).

### 3.9 `service.py`
```python
@dataclass
class AskParams:
    kind: str = "general"
    input: str = ""
    input_files: list[str] = field(default_factory=list)
    schema: dict[str, Any] | str | None = None   # str = абсолютный путь к JSON-схеме внутри корней
    checks: list[str] = field(default_factory=list)
    model: str | None = None
    priority: Literal["urgent", "normal", "low"] | None = None
    policy: Literal["ECONOMY", "BALANCED", "QUALITY"] | None = None
    max_tokens: int | None = None
    data_class: Literal["private", "cloud_allowed"] = "private"
    wait_s: float | None = None
    deadline_s: float | None = None
    output_file: str | None = None
    overwrite: bool = False
    fresh: bool = False

class McpService:
    def __init__(self, settings: McpSettings, client: HubClient, roots: Roots, kinds: dict[str, Kind]) -> None: ...
    async def ask(self, p: AskParams) -> AskResult: ...
    async def result(self, task_id: str, *, schema: dict[str, Any] | str | None = None, checks: list[str] | None = None,
                     kind: str | None = None, output_file: str | None = None, overwrite: bool = False,
                     wait_s: float = 0) -> AskResult: ...
    async def cancel(self, task_id: str) -> AskResult: ...
    async def models(self) -> dict[str, Any]: ...
```
Порядок `ask`: вид → проверки (`build_checks`) → `data_class`/cloud-guard → пути выхода (`resolve_output` **до** отправки, чтобы не тратить LLM на заведомо отклонённую запись) → входы (`resolve_input` + `read_text` с общим лимитом) → схема → `build_body` → ключ идемпотентности → `submit` → `wait` → `result` → `_finish` (проверки, запись, превью). `result` — то же без submit (проверки задаёт вызывающий; без `kind`/`checks` — только `not-truncated`). Все исключения (`PathDenied`, `CheckSpecError`, `HubError`, `KeyUnavailable`) превращаются в `verdict=error` с `code`; неожиданное → `internal_error` без трассы в ответе (трасса — в stderr без данных).

### 3.10 `server.py`
```python
def build_server(settings: McpSettings, *, transport: httpx.AsyncBaseTransport | None = None,
                 sleep: Callable[[float], Awaitable[None]] | None = None) -> MCPServer: ...
```
Инструменты (`@server.tool(name=..., description=..., annotations=..., structured_output=True)`, возврат `AskResult`/`dict`):
- `llmhub_ask(kind="general", input="", input_files=[], schema=None, checks=[], model=None, priority=None, policy=None, max_tokens=None, data_class="private", wait_s=None, deadline_s=None, output_file=None, overwrite=False, fresh=False)` — `openWorldHint=true`, не read-only.
- `llmhub_result(task_id, kind=None, schema=None, checks=[], output_file=None, overwrite=False, wait_s=0)`.
- `llmhub_cancel(task_id)` — `destructiveHint=true`.
- `llmhub_models()` — `readOnlyHint=true`; возвращает псевдонимы, `local`/`cloud`, `ctx_tokens` + список видов с описаниями.
Описание `llmhub_ask` строится из констант + описаний видов (§6). `server = MCPServer("llmhub", instructions=<краткое правило делегирования>)`.

### 3.11 `pyproject.toml` (поток E, единственный владелец)
```toml
[project.optional-dependencies]
mcp = ["mcp==2.2.0", "jsonschema>=4.20"]
[dependency-groups]
dev = [..., "mcp==2.2.0", "jsonschema>=4.20"]
[project.scripts]
llmhub-mcp = "llmhub.mcp_server.__main__:main"
```
`kinds.yaml` должен попасть в wheel (hatch включает файлы пакета; тест `importlib.resources`). `mcp==2.2.0` и `jsonschema` уже в кэше uv — `uv sync` без сети должен пройти (проверить `uv sync --offline`; если нет — вопрос Q1).

## 4. Регистрация агентами (только документация, `docs/integration.md` §8 «MCP для агентов»)
Пример для `.mcp.json` проекта (projectVoiceBot, gameDev) — **файл не правим**:
```json
{
  "mcpServers": {
    "llmhub": {
      "type": "stdio",
      "command": "/home/user/.local/bin/uv",
      "args": ["--directory", "/var/www/llmhub", "run", "--quiet", "--extra", "mcp", "python", "-m", "llmhub.mcp_server"],
      "env": {
        "LLMHUB_URL": "http://10.8.0.1:8095",
        "LLMHUB_KEY_FILE": "/home/user/.config/llmhub/agents.key",
        "LLMHUB_MCP_ROOTS": "/var/www/projectVoiceBot:/tmp/claude-1000",
        "LLMHUB_MCP_SCRATCH": "/home/user/.local/state/llmhub-mcp/out"
      }
    }
  }
}
```
gameDev — то же с `LLMHUB_MCP_ROOTS=/var/www/gameDev:/tmp/claude-1000`. Документ описывает: ключ — только файлом (600, владелец), никогда в `env` `.mcp.json`; клиент `agents` на сервере с `cloud_allowed=false`, `max_priority=normal` (T-371); `LLMHUB_MCP_ALLOW_CLOUD` не ставить без решения заказчика; все пути — абсолютные; `.env`, `.git`, `.ssh`, `.claude` недоступны по построению; разрешения инструментов в `settings.json` (`mcp__llmhub__llmhub_ask` и т. д.) — на усмотрение владельца проекта.
**Таймауты:** Claude Code ограничивает вызов MCP-инструмента `MCP_TOOL_TIMEOUT` (мс, env процесса Claude Code). Правило: `wait_s` (≤ `LLMHUB_MCP_MAX_WAIT_S`=300) + 30 с запаса < `MCP_TOOL_TIMEOUT`/1000; рекомендуемо `MCP_TOOL_TIMEOUT=360000`. Если клиент оборвёт вызов раньше — задача на сервере живёт, агент получает её `task_id` только из повторного `llmhub_ask` с теми же параметрами (тот же ключ идемпотентности → та же задача) или `llmhub_result`. Долгие задачи: `wait_s=0..30` → `pending` → позже `llmhub_result(task_id, wait_s=…)`. Размер ответа: `LLMHUB_MCP_MAX_INLINE_BYTES` держать ниже `MAX_MCP_OUTPUT_TOKENS` Claude Code (16 000 байт ≈ 4–8k токенов).

## 5. Шаги (TDD) и потоки
Тесты — `tests/test_m_*.py`, общие фикстуры — `tests/m_fixtures.py` (поток D). Команда для шага: `uv run pytest tests/test_m_<x>.py -q` + `uv run ruff check src/llmhub/mcp_server tests/test_m_*.py` + `uv run pyright src/llmhub/mcp_server tests/test_m_*.py`.

### Поток 0 — подготовка (haiku, первым, 10 мин). Владеет: `pyproject.toml`, `uv.lock`
| Шаг | Тест сначала | Что |
|---|---|---|
| 0.1 | `uv run python -c "import mcp.server.mcpserver, jsonschema, mcp_types"` | §3.11: extra `mcp`, dev-группа, `project.scripts`; `uv sync --offline` (или `uv sync`) |

### Поток A — настройки, ключ, файлы, модели ответа (sonnet). Владеет: `settings.py`, `secret.py`, `paths.py`, `models.py`, `tests/test_m_settings.py`, `tests/test_m_secret.py`, `tests/test_m_paths.py`. Зависит от 0
| Шаг | Тест сначала | Что |
|---|---|---|
| A1 | `test_m_settings.py`: умолчания; `:`-список корней, пустые элементы; неверное число → `ValueError` без значения; `ALLOW_CLOUD` только `"1"` | `settings.py` |
| A2 | `test_m_secret.py`: env главнее файла; файл 0644 → `bad_mode`; симлинк → `not_regular`; чужой uid (мок `os.fstat`) → `bad_owner`; пустой → `empty`; `repr`/`str`/`f"{s}"` не содержат значения; сообщение исключения не содержит ключа | `secret.py` |
| A3 | `test_m_paths.py` — **по тесту на каждый отказ**: относительный путь; `..` наружу; симлинк файла наружу; симлинк каталога наружу; `root/.env`; `root/.git/config`; `root/sub/.ssh/id`; `root/link` → `root/.env` (realpath-хвост); `root/.git/../a.txt` (нормализованный хвост); каталог вместо файла; FIFO; вход > лимита (по `st_size`); не-UTF-8; выход существует без `overwrite`; выход существует с `overwrite` → перезапись; выход — симлинк (даже внутрь корня) → `symlink`; родителя нет; выход вне корней; корень с точкой (`tmp/.state/root/a.txt`) — разрешён; scratch неявно корень и создаётся 0700; запись атомарна (временный файл не остаётся при ошибке), права 0600; `scratch_file("../x")` фильтруется | `paths.py` |
| A4 | `test_m_models.py`: сериализация `AskResult` по умолчанию, `verdict` только из 4 значений | `models.py` |

### Поток B — проверки и виды (haiku для B1, sonnet для B2). Владеет: `checks.py`, `kinds.py`, `kinds.yaml`, `tests/test_m_checks.py`, `tests/test_m_kinds.py`. Зависит от 0
| Шаг | Тест сначала | Что |
|---|---|---|
| B1 | `test_m_checks.py`: на каждую проверку — проходит/не проходит/плохая спецификация; `not-truncated` всегда добавлен; `json-schema` с ```` ```json ```` обёрткой; не-JSON → провал с причиной `not_json`; причина не содержит фрагментов текста; `item-count` массив / объект с одним массивом / строки; `regex` длиной 301 → `CheckSpecError`; `script:han:0.5` | `checks.py` |
| B2 | `test_m_kinds.py`: встроенные 6 видов грузятся через `importlib.resources`; пользовательский файл добавляет вид и переопределяет `max_tokens` существующего; неизвестное поле → ошибка; `extract_json.requires_schema`; во встроенных `task_type is None` | `kinds.py`, `kinds.yaml` |

### Поток C — тело, ключ идемпотентности, HTTP-клиент (sonnet). Владеет: `request.py`, `client.py`, `tests/test_m_request.py`, `tests/test_m_client.py`. Зависит от 0; `Secret` и `Kind` — по сигнатурам §3.2/§3.5 (до готовности A/B — локальные заглушки в тесте)
| Шаг | Тест сначала | Что |
|---|---|---|
| C1 | `test_m_request.py`: тело содержит system вида, блоки файлов с basename, `response_format` при схеме, `enable_thinking` по `think`, `task_type` только из вида; **тот же путь, другое содержимое → другой ключ**; другой каталог, тот же basename и содержимое → тот же ключ; `fresh` → разные ключи; `data_class`/`priority`/`policy` меняют ключ; ключ ≤ 255, печатный ASCII | `request.py` |
| C2 | `test_m_client.py` на `httpx.MockTransport` (ответы — по `docs/openapi.yaml`): `202`/`200` submit; заголовки `Authorization`, `Idempotency-Key`, `X-LLMHub-Data-Class`; разбор `error` всех кодов таблицы §6; `x-should-retry: false` при `retryable=true` в теле → не повторять; `503 busy` → повтор с тем же ключом; `Retry-After` больше бюджета → без повтора, ошибка наружу; транспортная ошибка → `transport`, причина = имя класса; 401 → без повтора; `result` заголовки → `backend/fallback/truncated/warnings`; ключ не встречается в `repr(HubError)` и в записях `caplog` | `client.py` (`_request`, разбор, повторы) |
| C3 | `test_m_client.py::wait*`: опрос с паузой `clamp(eta/4, 0.5, 5)` (фейковые `sleep`/`monotonic`), выход по терминальному статусу, `None` по сроку, `eta_s=None` → 2 с | `HubClient.wait` |

### Поток D — сервис (sonnet; ревью ×2: логика; сбои/перезапуски). Владеет: `service.py`, `tests/m_fixtures.py`, `tests/test_m_service.py`, `tests/test_m_service_api.py`. Зависит от A, B, C
| Шаг | Тест сначала | Что |
|---|---|---|
| D1 | `m_fixtures.py`: фикстура `hub` = `ApiEnv(tmp)` + `HubClient(transport=ASGITransport(env.app), sleep=env.pump)` — всё in-process, FakeClock, `StubWorker` (`answer`, `hold`, `hang`); корни в `tmp_path` | фикстуры |
| D2 | `test_m_service_api.py`: `ok` (ответ `StubWorker.answer`, `backend`, `tokens`); `check_failed` (схема не совпала) → текст в `<id>.rejected.txt`, `output_file` не создан; `pending` (`hold=True`, `wait_s=5`) → `task_id` → снять hold → `result(task_id)` → `ok`; повтор того же `ask` после `pending` → тот же `task_id` (идемпотентность); `fresh=true` → новый `task_id`; заголовок `X-LLMHub-Data-Class: private` в каждом запросе (перехват транспорта); `data_class=cloud_allowed` без `ALLOW_CLOUD` → `cloud_not_enabled` без запроса; с `ALLOW_CLOUD` и клиентом `PRIV_KEY` (на сервере `cloud_allowed=false`) — задача всё равно локальная; `finish_reason=length` → `check_failed` (`not-truncated`), `truncated=true`; `finish_reason=error` → `verdict=error upstream_rejected`; задача `failed` → `error` с `code/reason/retryable`; `cancel` → `cancelled`; `result` после `cancel` → `error cancelled` | `McpService.ask/result/cancel` |
| D3 | `test_m_service.py` (без API): вход > `max_input_bytes` → `input_too_large` без запроса; запрещённый `output_file` → отказ **до** submit (счётчик запросов транспорта = 0); `invalid_check` до submit; `requires_schema` без схемы; схема строкой-путём (внутри корней / с точкой → отказ); большой текст > inline → файл в scratch, превью ≤ `preview_bytes` и валидный UTF-8 (кириллица на границе), `truncated_inline=true`; с `output_file` — запись туда; `overwrite=false` и файл появился между проверкой и записью → `exists`; `wait_s` > `max_wait_s` → обрезается; ни в одном `AskResult` нет ключа | `McpService` (ветки ошибок, запись) |
| D4 | `test_m_service.py::models`: `models()` возвращает псевдонимы и виды; без ключа → `key_unavailable` | `McpService.models` |

### Поток E — MCP-сервер, точка входа, живой тест, документация (sonnet). Владеет: `server.py`, `__main__.py`, `__init__.py`, `tests/test_m_server.py`, `tests/test_m_imports.py`, `tests/test_m_live.py`, `docs/integration.md` (новый §8). Зависит от D
| Шаг | Тест сначала | Что |
|---|---|---|
| E1 | `test_m_imports.py`: AST всех `src/llmhub/mcp_server/*.py` — импорты только из `sys.stdlib_module_names` ∪ {`httpx`, `mcp`, `mcp_types`, `jsonschema`, `yaml`, `pydantic`} ∪ `llmhub.mcp_server.*`; относительные импорты — только внутри пакета; нет `print(`; нет `voicebot`, `tools` | — (страж) |
| E2 | `test_m_server.py` через `mcp.client.client.Client(build_server(...))` in-process (как `test_devtools_mcp.py` в projectVoiceBot): список из 4 инструментов, схемы параметров (`data_class` enum, умолчание `private`), аннотации; описание `llmhub_ask` содержит правило делегирования (подстроки «schema», «classif», «verdict»); вызов `llmhub_ask` на `ApiEnv`-транспорте → структурный ответ `ok`; ошибка → `isError=false` и `verdict=error` (агенту нужен разбор, а не исключение) | `server.py` |
| E3 | `test_m_server.py::main_stdout_clean`: `__main__` с неверным `LLMHUB_MCP_MAX_INPUT_BYTES` → код ≠ 0, сообщение в stderr, stdout пуст; логгер `httpx` на `WARNING` | `__main__.py` |
| E4 | `test_m_live.py` (`pytestmark = slow`): `LiveStack(tmp).start()`; подпроцесс `sys.executable -m llmhub.mcp_server` с env (`LLMHUB_URL=stack.url`, ключ файлом 600 в tmp, корни в tmp) через `mcp.client.stdio.stdio_client` → `ok`; `check_failed` (`regex:` не найден); `pending` (`wait_s=0`, `RichLlama` с медленным `tg_tps`) → `llmhub_result(task_id, wait_s=30)` → `ok`; stderr подпроцесса не содержит ключа; подпроцесс закрывается своим PID | — |
| E5 | `test_m_docs.py`: `docs/integration.md` содержит раздел MCP, пример JSON парсится, в нём нет `LLMHUB_KEY"` (только `_FILE`), упомянут `MCP_TOOL_TIMEOUT`; TL;DR файла дополнен | `docs/integration.md` §8 (регистрация, переменные, таймауты, `pending`, что делегировать — §6) |

### Порядок и параллельность
```
0 ──► A ─┐
   ├► B ─┼─► D ──► E
   └► C ─┘
```
A, B, C — параллельно (разные файлы; C пишет заглушки `Secret`/`Kind` по сигнатурам). D — после A, B, C. E — последним (интеграция: `pyproject.toml` уже сделан в 0; `docs/integration.md` и `__init__.py` — только E). Модели: 0, B1 — haiku; A, B2, C, D, E — sonnet. Итоговый прогон: `uv run pytest tests/test_m_*.py -q` (без slow) + `uv run pytest -m slow tests/test_m_live.py -q` + ruff + pyright.

## 6. Что агентам делегировать (правило в описаниях инструментов и в integration §8)
Основа — пилот p2 (`llm-external.md`): accept+minor 40 %, 5 тихих reject из 11 «ok». До нового пилота на llmhub:
- **Можно** (с `schema`/`checks` и обязательным просмотром результата Claude): извлечение структуры из данных, где ответ однозначно сверяется с источником (`extract_json` + `schema` + `item-count`); классификация по явному полю (`classify` + `schema` с `enum`). Выигрыш — на входах ≥ 8k токенов; ожидание — минуты (`pending` + `llmhub_result`).
- **Нельзя:** заметки к релизу/changelog, переводы (особенно китайский), свободная классификация по строкам, числовые агрегаты, ревью кода как вердикт, всё с секретами или ключами во входе, всё, где ошибку не поймает проверка и не увидит Claude.
- **Эскалация:** `verdict ≠ ok` → подзадачу делает Claude целиком, ответ не «чинится», не повторяется `fresh=true` ради другого ответа (кроме `error.retryable=true` после `retry_after_s`). `ok` → Claude просматривает; если не accept — делает сам.
- Виды `summarize`, `translate`, `review`, `general` есть (запрос задачи), но их описание помечено «пилот: не для рабочих задач». Решение о расширении — новый пилот (Q4).
- Нагрузка: локальная LLM одна (CPU, ~9 ток/с); параллельные агенты встают в общую очередь — `priority` по умолчанию от клиента, `urgent` не использовать.

## 7. Риски
| # | Риск | Мера |
|---|---|---|
| R1 | Запись в stdout ломает протокол MCP | E1 (нет `print`), E3, логи только stderr |
| R2 | TOCTOU: подмена каталога симлинком между проверкой и записью | `O_NOFOLLOW|O_EXCL` на временный файл, `os.replace` в том же каталоге, повторный `realpath` родителя после открытия (`/proc/self/fd`); остаток риска — в документе (агенты — свои процессы, не противник) |
| R3 | `MCP_TOOL_TIMEOUT` клиента меньше `wait_s` — вызов обрывается без `task_id` | §4: правило и умолчание `wait_s=90`; повтор `ask` присоединяется к задаче по ключу идемпотентности (тест D2) |
| R4 | `mcp==2.2.0` тянет `httpx2`, `starlette`, `uvicorn` — тяжёлое окружение; конфликт версий с `httpx` llmhub | extra `mcp` отдельно; шаг 0 проверяет `uv sync`; при конфликте — Q1 |
| R5 | ReDoS в `regex:` | паттерн ≤ 300, текст ≤ размера ответа; проверки выполняются в `asyncio.to_thread` с таймаутом 5 с → `check_failed`, причина `regex_timeout` (поток продолжает работать — допустимо для stdio-процесса агента) |
| R6 | Схема `strict` не поддерживается конкретной моделью/llama-server → ответ не JSON | проверка `json-schema` на нашей стороне ловит; verdict `check_failed` |
| R7 | Ключ попадает в трассы исключений httpx | ключ только в заголовке; `HubError` без текста исключения; тесты C2, E4 по `caplog`/stderr |
| R8 | Повтор `ask` после `retryable`-провала с тем же ключом запускает новую задачу (поведение сервера) — неожиданно для агента | описано в hint и integration §8 |

## 8. Вопросы (ни один не блокирует implementer; приняты умолчания)
- Q1: если `uv sync --offline` не находит всех зависимостей `mcp==2.2.0` — нужна сеть к PyPI (не платно). Умолчание: implementer выполняет обычный `uv sync`.
- Q2: точное поведение/умолчание `MCP_TOOL_TIMEOUT` в текущей версии Claude Code — implementer сверяет по документации Claude Code при написании §8; рекомендация `360000` остаётся в любом случае.
- Q3: проверки с источником (`numbers:<file>`, `ini-keys:<file>`) — нужны ли в v1? Умолчание: нет (отдельная задача; входы-источники всё равно доступны агенту).
- Q4: расширять ли список делегируемого после пилота на llmhub — решение заказчика после нового пилота (не в T-373).
- Q5: клиент `agents` и файл ключа на машине разработки — T-371 (деплой); до него живого использования нет, только тесты.

## 9. Приёмка (к критериям задачи)
| Критерий | Шаги/тесты |
|---|---|
| Файлы только внутри корней, realpath, симлинки, dot-каталоги, без перезаписи; тест на каждый отказ | A3, D3 |
| `private` по умолчанию; облако только явным `cloud_allowed` (+ сервер) | §2.7, D2, E2 (схема) |
| `Idempotency-Key` = sha256 тела с содержимым файлов; `fresh`; тот же путь — другой ключ | C1, D2 |
| Лимит `LLMHUB_MCP_MAX_INLINE_BYTES` → файл в scratch, путь, `truncated_inline`, начало текста | D3 |
| Лимит `LLMHUB_MCP_MAX_INPUT_BYTES` 400 000 | A1, A3, D3 |
| Документировано `wait_s` vs `MCP_TOOL_TIMEOUT` | E5, §4 |
| Ответ `verdict/text|output_file/checks/task_id/backend/fallback/truncated/tokens/error`; ключ нигде | A4, D2, D3, C2, E4 |
| Тест импортов (`mcp_types`, `pydantic` — как часть `mcp`, R4) | E1 |
| Живой `slow` stdio-тест: `ok`, `check_failed`, `pending` → `llmhub_result` (фикстура — `tests/int_live.py::LiveStack`; в задаче ошибочно `i_fixtures.py`) | E4 |
| Регистрация в `docs/integration.md`; pytest, ruff, pyright; ревью ≤ 3 раундов | E5, §10 |

## 10. Ревью и остановка (CLAUDE.md, ТЗ §18)
Ревью ×2 для потока D и `paths.py`: (1) логика/гонки (повторы, идемпотентность, `pending`→`result`, TOCTOU записи); (2) сбои (сервер недоступен, 5xx, обрыв клиента MCP, ключ испорчен, scratch недоступен). Остальное — один цикл. Остановка: тесты `test_m_*` зелёные (включая slow), ruff и pyright чисто, у ревьюера нет blocker/major; лимит 3 раунда.

## 11. Ворота
Приоритет задачи P2 → ворота critic + advocate (ADR-0139) не обязательны. План оформлен так, чтобы координатор мог провести их без доработки: решения — §2, риски — §7, вопросы — §8. Если ворота проводятся, их находки вносятся новым разделом здесь (главнее разделов выше).

## 12. Выполнение (implementer, 2026-11-04)
- [x] 0.1 extra `mcp`, dev-группа, `llmhub-mcp` (`uv sync --offline` не нашёл `python-multipart` → обычный `uv sync`, Q1)
- [x] A1 `settings.py` · [x] A2 `secret.py` · [x] A3 `paths.py` · [x] A4 `models.py`
- [x] B1 `checks.py` · [x] B2 `kinds.py` + `kinds.yaml`
- [x] C1 `request.py` · [x] C2 `client.py` · [x] C3 `HubClient.wait`
- [x] D1 `tests/m_fixtures.py` · [x] D2 `test_m_service_api.py` · [x] D3 `test_m_service.py` · [x] D4 `models`
- [x] E1 `test_m_imports.py` · [x] E2 `server.py` · [x] E3 `__main__.py` · [x] E4 `test_m_live.py` (slow) · [x] E5 `docs/integration.md` §8 + `test_m_docs.py`
