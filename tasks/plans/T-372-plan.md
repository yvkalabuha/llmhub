# План T-372 (v2 после ворот): реальный RunpodAdapter, предохранители трат и живой тест RunPod ≤ $5

> TL;DR: v2 учитывает ворота 2026-11-04 (critic — «accept with changes», 5 high; advocate — «needs-decision») и все решения заказчика. Образ — публичный GHCR по digest. Mock-ledger помечается исключённым, без DELETE. Разрешение одно на весь тест. Обрыв сети заменён учением «контроллер недоступен». На время теста работает автоудаление по таймеру на рабочей станции.
> Объём урезан (critic F12): нет spike dstack, `_eta`, HF 429, пачек `IN` и `direct`; удаляются только поды. Всё вынесенное собрано в новой задаче (BOARD «Входящие»).
> Код: потоки A–F + I, 24 шага, TDD на фейке RunPod без сети. Живой тест P0, L0–L8 выполняет координатор. Ожидается ≈ $0,25, с запасом ×2 ≈ $0,50. Потолок llmhub — $4,00. Аварийная остановка — при падении баланса на ≥ $3,00 или при ошибке разбора ответа RunPod.

Задача: `tasks/active/T-372-stage4-real-provider.md` · ADR-0138 (п. 3, 12, 13; риски) · T-367: «Замер RunPod 2026-11-04», «Поправка: лимит трат», «Ворота 2026-11-04» пп. 6, 13, 14 · BOARD «Входящие» · `docs/runbook.md` §8.

## 0. Ворота (P1, ADR-0139) — пройдены, решения учтены
- Маркер: `gate: done (P1) — critic: accept with changes (5 high); advocate: needs-decision → решения заказчика 2026-11-04: Q1, Q5, D1, D2, D3 (§10)`.
- Сопоставление находок — §11. Тексты critic F6, F10 и F13 координатор передал implementer'у (2026-11-04); они учтены в §11.

## 1. Объём и не-цели
**В объёме**
1. `RunpodAdapter` и `RunpodApi`: REST v1 + GraphQL на httpx, без SDK `runpod`. Фейк RunPod — стейт-машина поверх `httpx.MockTransport`; `run_contract` C1–C11. Схема сверяется по публичному `openapi.json` RunPod.
2. Конфиг `providers.runpod` с предохранителями-валидаторами. Ключ — из `LLMHUB_RUNPOD_API_KEY_FILE`. Общая фабрика адаптеров для `serve` и reaper.
3. Ядро:
   - `last_error` при сбое `list_offers` (входящее);
   - `host_blacklist_s ≥ 3600` (входящее);
   - неизвестный исход create: **0 повторов** → FAILED и сигнал оператору (F1);
   - хост из чёрного списка после create → удалить и повторить;
   - TTL токена весов ≤ 30 мин (F9).
4. Ledger: строки Mock исключаются полем `excluded_at` (F14), без DELETE.
5. Публикация `llmhub-infer` в GHCR: поуровневый скан секретов, digest в `imageName` (Q1, F8).
6. Независимый внешний сторож на рабочей станции:
   - только stdlib и сырой JSON, без импортов `llmhub`;
   - снимок, diff, запись фикстур по allowlist, `watch`;
   - автоудаление по таймеру `systemd --user` только на время теста (D3).
7. CLI оператора: `provider-check`, `lifecycle-probe`, `ledger-exclude`, `scan-secrets`, `weights-token-check`.
8. Деплой ключа (§5), runbook живого теста (§6), отчёт в журнале.

**Вынесено в новую задачу** (critic F12; строка в BOARD «Входящие»):
- spike dstack «за столом» (0,5 дня). Вердикт по умолчанию — `RunpodAdapter`, `DstackAdapter` возможен позже, интерфейс тот же;
- пессимизм `_eta`;
- HF 429 → hold пула;
- `waiter._tick` пачками `IN`;
- удаление network volumes и шаблонов;
- `transport: direct` и `endpoint` в BOOTING;
- учение «обрыв сети» правилом iptables (D2).

**Не входит**
- Vast, Secure Cloud, serverless, network volumes, шаблоны, registry auth: адаптер их не создаёт и не удаляет.
- Самоуничтожение пода — этап 5.
- Облако для реальных клиентов: `cloud_allowed: false` у всех, кроме временного `t372`.
- Сетевые вызовы RunPod в тестах — никогда. Analyst и implementer ключи RunPod не используют.

## 2. Что уже есть (проверено)
- `base.py`: Protocol без `stop`. `contract.py`: C1–C11. `guarded.py`: create/destroy без повторов. `labels.py`: `llmhub-<op12>-<hmac8>`.
- Ядро сверяет `label` перед удалением (`teardown.py:94`, reaper `verify_label`). Поэтому адаптер отдаёт **настоящее** имя из ответа.
- `list_owned(label)` вызывается с полной меткой как префиксом.
- `list_staleness_s` — необязательный атрибут адаптера.
- Повтор create при неизвестном исходе есть в двух местах: `provisioner._provisioning` (≈ стр. 332–345) и `recovery.py` (≈ стр. 124–137).
- Порт и транспорт пода — в `spec.env` (`LLMHUB_TRANSPORT`, `LLMHUB_PORT`), секреты — в `spec.secret_env`.
- Токен весов: `instance_env.py:168`, `exp = plan["deadline_unix"]`. Отзыв после выхода из `LOADING_STATES` — `weights/tokens.py:99`.
- `proxy`: `https://<podId>-<port>.proxy.runpod.net`, TLS на стороне RunPod.
- Endpoint весов `self_hosted` развёрнут: 65.109.95.35:9443, dist 0.2.2, qwen3.8-27b UD-Q4_K_M + MTP.
- База образа `llmhub-infer` — `ghcr.io/ggml-org/llama.cpp:server-cuda12-b12063@sha256:d5ff…` (CUDA 12.8.1).
- `/etc/llmhub/secrets/providers` сейчас `0:0 600` (`llmhub-deploy.sh:518/535`). Контейнер с uid 64100 его не прочтёт (§5).

## 3. Адаптер
### 3.1 API RunPod (все поля сверяются с `openapi.json` в P0, F2a)
| Действие | Вызов |
|---|---|
| офферы | GraphQL `https://api.runpod.io/graphql`: `gpuTypes(input:{id}) { id displayName memoryInGb lowestPrice(input:{gpuCount:1, secureCloud:false}) { uninterruptablePrice stockStatus } }` |
| create | `POST https://rest.runpod.io/v1/pods` `{name, imageName (обязательно @sha256), cloudType:"COMMUNITY", computeType:"GPU", gpuTypeIds:[id], gpuCount:1, containerDiskInGb, volumeInGb:0, ports:["8080/http"], interruptible:false, allowedCudaVersions:[…≥ "12.8"], env}`; **без** `networkVolumeId`, `templateId`, `containerRegistryAuthId` |
| get / list | `GET /v1/pods/{id}`, `GET /v1/pods` (пагинация — по openapi) |
| destroy | `DELETE /v1/pods/{id}` — **только поды** (F4) |
| только видеть | `GET /v1/networkvolumes`, `GET /v1/templates` |
| баланс | GraphQL `myself { clientBalance currentSpendPerHr spendLimit }` |
| биллинг | `GET /v1/billing/pods?startTime&endTime&bucketSize=hour&grouping=podId` |

- Ключ передаётся только заголовком `Authorization: Bearer`; `?api_key=` запрещён (тест). Логгер `httpx` — WARNING.
- Фикстуры: `synthetic/` (по openapi, `"_source":"synthetic"`) и `recorded/` (L1, по allowlist, F7).

### 3.2 Файлы
| Файл | Поток |
|---|---|
| `src/llmhub/provider_adapters/runpod_api.py`, `runpod.py`; `tests/runpod_fake.py`; `tests/fixtures/runpod/{openapi.json,synthetic/,recorded/}`; `tests/test_t372_runpod_{api,errors,adapter,contract,schema}.py`; `tests/test_t372_no_runpod_network.py`; `tests/conftest.py` | A |
| `configuration/models.py`, `loader.py`, `provider_adapters/factory.py`, `server.py` / `reaper/__main__.py` (только `build_adapters`); `tests/test_t372_config.py`, `test_t372_factory.py` | B |
| `cloud_manager/provisioner.py`, `lifecycle_manager/recovery.py`, `cloud_manager/instance_env.py`; `tests/test_t372_core_fixes.py` | C |
| `persistence/migrations/0004_ledger_excluded.sql`, `cost_tracker/ledger.py` (и прочие суммы ledger), `admin_cli.py`; `tests/test_t372_ledger_exclude.py`, `test_t372_cli.py` | D |
| `deploy/server/llmhub-deploy.sh`, `deploy/config.t372.example.yaml`, `scripts/image_secret_scan.py`, `deploy/docker/publish-infer.sh`; `tests/test_t372_deploy_runpod.py`, `test_t372_image_scan.py` | E |
| `scripts/runpod_guard.py` (stdlib), `deploy/workstation/llmhub-runpod-guard.{service,timer}`, `deploy/workstation/guard.env.example`; `tests/test_t372_guard.py`, `test_t372_fixture_scan.py` | F |
| `provider_adapters/__init__.py`, `docs/t372-live-test.md`, `docs/runbook.md`, `deploy/README.md`, `BOARD.md`, `STATUS.md` | I |

### 3.3 Интерфейсы
```python
# provider_adapters/runpod_api.py  (no I/O at import — C10)
REST_BASE = "https://rest.runpod.io/v1"; GRAPHQL_URL = "https://api.runpod.io/graphql"

class RunpodHttpError(Exception):
    def __init__(self, *, method: str, path: str, status: int | None, code: str | None,
                 retry_after_s: float | None, sent: bool) -> None: ...
    # str() = "<METHOD> <path template> -> <status> <code>"; never body, never key

class RunpodParseError(RunpodHttpError): ...   # unexpected shape -> stop condition (F2)

@dataclass(frozen=True)
class PodInfo:
    id: str; name: str; desired_status: str; cost_per_hr: float | None
    machine_id: str | None; image: str | None; created_at: float | None  # env never kept
@dataclass(frozen=True)
class NamedRef: id: str; name: str           # network volume / template (report only)
@dataclass(frozen=True)
class GpuTypeInfo: id: str; display_name: str; memory_gb: int
                   community_price: float | None; stock_status: str | None
@dataclass(frozen=True)
class AccountInfo: balance_usd: float; spend_per_hr: float; spend_limit: float | None
@dataclass(frozen=True)
class BillingBucket: pod_id: str | None; start: float; end: float; amount_usd: float

class RunpodApi:
    def __init__(self, key: Callable[[], str], *, transport: httpx.AsyncBaseTransport | None = None,
                 timeout_s: float = 20.0, rest_base: str = REST_BASE,
                 graphql_url: str = GRAPHQL_URL) -> None: ...
    async def list_pods(self) -> list[PodInfo]: ...            # all pages or raise
    async def get_pod(self, pod_id: str) -> PodInfo | None: ...  # 404 -> None
    async def create_pod(self, body: Mapping[str, Any]) -> PodInfo: ...
    async def delete_pod(self, pod_id: str) -> bool: ...        # False = 404
    async def list_network_volumes(self) -> list[NamedRef]: ...
    async def list_templates(self) -> list[NamedRef]: ...       # user's only
    async def gpu_types(self, ids: Sequence[str]) -> list[GpuTypeInfo]: ...
    async def myself(self) -> AccountInfo: ...
    async def billing_pods(self, start: float, end: float) -> list[BillingBucket]: ...
    async def aclose(self) -> None: ...
```
```python
# provider_adapters/runpod.py
REF_VOLUME = "vol:"; REF_TEMPLATE = "tpl:"   # listed only; destroy() refuses them (F4)

class RunpodAdapter:
    name = "runpod"
    capabilities = (Capability.OFFER_SEARCH | Capability.BALANCE_API | Capability.BILLING_API
                    | Capability.CUSTOM_IMAGE | Capability.SPEND_RATE)
    def __init__(self, api: RunpodApi, cfg: RunpodConfig, clock: Clock, *, disk_gb_hint: int): ...
    @property
    def list_staleness_s(self) -> float: ...
    async def list_offers(self, q: OfferQuery) -> list[Offer]: ...
    async def create(self, spec: InstanceSpec, label: str) -> RemoteResource: ...
    async def get(self, ref: str) -> RemoteResource | None: ...
    async def list_owned(self, prefix: str = "llmhub-") -> list[RemoteResource]: ...
    async def destroy(self, ref: str) -> DestroyResult: ...
    async def balance(self) -> Balance | None: ...
    async def billing_since(self, ts: float) -> list[BillingRecord]: ...
    def normalize_error(self, exc: BaseException) -> NormalizedError: ...

def pod_endpoint(pod: PodInfo, port: int) -> str: ...      # proxy only in T-372
def pod_status(pod: PodInfo) -> RemoteStatus: ...
def create_body(spec: InstanceSpec, label: str, cfg: RunpodConfig, gpu_type_id: str) -> dict[str, Any]: ...
```
Правила (каждое — тест A4/A6):
1. **create** — ровно один POST, без повторов. *Уточнено L2b (2026-11-04):* один POST на каждую версию из `allowed_cuda_versions` (по одной, от старшей; `allowedCudaVersions: [v]`); к следующей версии — только после определённого отказа `no_capacity` (реальный 500 `"no instances currently available"`); успех, неизвестный исход или иной отказ — стоп. Максимум POST = число версий.
   - До POST проверяется:
     - `spec.image` содержит `@sha256:<64 hex>`;
     - `transport == "proxy"`;
     - цена оффера ≤ `min(spec.max_price_usd_h, cfg.price_ceiling_usd_h)`;
     - живых `llmhub-*` подов < `cfg.max_live_pods`.
   - Если проверка не прошла — `ProviderError(QUOTA_EXCEEDED | DEPLOYMENT_ERROR, outcome_unknown=False)`. Сбой `list_pods` → тоже отказ с `outcome_unknown=False`.
   - После 2xx: если `name != label` или `cost_per_hr > ceiling` → сразу `delete_pod` и `ProviderError(DEPLOYMENT_ERROR, outcome_unknown=True)`.
   - 5xx, таймаут или обрыв после отправки → `outcome_unknown=True`.
2. **list_owned** — поды (INSTANCE), тома (VOLUME, ref `vol:`) и шаблоны (OTHER, ref `tpl:`) с `name.startswith(prefix)`. Сбой любого списка или страницы, а также `RunpodParseError` → исключение. Никогда не частичный список и не `[]` при сбое.
3. **destroy**: под — 2xx → `ACCEPTED`, 404 → `ALREADY_GONE`. Для `vol:`/`tpl:` → `ProviderError(DEPLOYMENT_ERROR, retryable=False)` без HTTP-вызова; kill тогда даёт `kill_incomplete`, это условие остановки (F4).
4. `env` из ответов не разбирается и не хранится (C11).
5. `list_offers`: `offer_id = gpuTypeId`, `host_id=""`, `disk_usd_h = disk_gb_hint × container_disk_usd_gb_month / 730`; `stock_status is None` → оффера нет.
6. `host_id = machine_id`; `price_usd_h = cost_per_hr`; `spend_rate_usd_h = cost_per_hr` при RUNNING, иначе `None`.

### 3.4 Нормализация ошибок
| Вход | reason | retryable | outcome_unknown |
|---|---|---|---|
| 401/403 | provider_api_error | нет | нет |
| 402, GraphQL «insufficient»/«balance» | insufficient_balance | нет | нет |
| «no longer any instances available», 409/503 capacity | gpu_unavailable | да | нет |
| «spend limit» | quota_exceeded | нет | нет |
| 429 (`Retry-After`) | provider_api_error | да | нет |
| прочие 4xx | deployment_error | нет | нет |
| 5xx, timeout/reset после отправки | provider_api_error | да | **да** |
| ConnectError до отправки | provider_api_error | да | нет |
| `RunpodParseError` | provider_api_error | нет | да |
| прочее | provider_api_error | да | да |

### 3.5 Конфиг
```python
class RunpodConfig(_Model):
    cloud_type: Literal["COMMUNITY"] = "COMMUNITY"
    gpu_type_ids: dict[str, str] = {"RTX3090": "NVIDIA GeForce RTX 3090"}
    allowed_cuda_versions: list[str] = ["12.8", "12.9", "13.0"]   # advocate F7b; tried one by one, highest first (L2b)
    price_ceiling_usd_h: float = Field(default=0.50, gt=0, le=0.50)
    max_live_pods: int = Field(default=1, ge=1, le=4)
    list_staleness_s: float = Field(default=30, ge=0, le=600)
    container_disk_usd_gb_month: float = 0.10
    call_timeout_s: float = Field(default=20, gt=0, le=60)
class ProvidersConfig(_Model):
    enabled: list[str] = ["mock"]
    runpod: RunpodConfig | None = None
class RetryConfig(_Model):
    host_blacklist_s: float = Field(default=86400, ge=3600)
    create_unknown_max_retries: int = Field(default=1, ge=0, le=3)   # F1
class WeightsConfig(...):
    token_ttl_max_s: int = Field(default=1800, ge=300, le=1800)       # F9
# Config validators when "runpod" in providers.enabled:
#   providers.runpod is set; timeouts.create_visibility_grace_s >= 600; retry.create_unknown_max_retries == 0;
#   every runpod pool: gpu in gpu_type_ids, max_price_usd_h <= price_ceiling, image matches
#   r"^[^@\s]+@sha256:[0-9a-f]{64}$", infer.transport == "proxy", disk_gb > 0.
# loader.py
def load_provider_key(provider: str, env: Mapping[str, str] = os.environ) -> Callable[[], str]:
    """Lazy reader of LLMHUB_<P>_API_KEY_FILE (read_secret_file rules); no plain env value."""
# provider_adapters/factory.py
def build_provider_adapters(cfg: Config, env: Mapping[str, str], clock: Clock, *,
                            runpod_transport: httpx.AsyncBaseTransport | None = None
                            ) -> dict[str, ProviderAdapter]: ...
```

### 3.6 Внешний сторож (F2b, F3; advocate F3; D3 = да) — `scripts/runpod_guard.py`
- Только stdlib (`urllib.request`, `json`), импорт `llmhub` запрещён (тест по AST).
- Свой минимальный разбор сырого JSON: у пода берутся только `id`, `name`, `desiredStatus`, `costPerHr`, `createdAt`. Нет поля или неожиданный тип → код 4 «parse error», это условие остановки.
- Подкоманды:
  - `snapshot --key-file RO --out F`: баланс, `currentSpendPerHr`, поды, тома, шаблоны, registry auth, serverless endpoints (id, name, status, cost);
  - `diff A B`: новые и исчезнувшие ресурсы, Δ баланса **в центах**; код 3, если есть новые `llmhub-*`;
  - `record --key-file RO --dir D`: GET-ответы, сохраняются **только** поля allowlist (F7);
  - `watch --key-file RO --before F [--once]`: одна строка в минуту;
  - `destroy-leftovers --key-file RW --before F [--yes]`: без `--yes` — dry-run.
- `watch --auto-destroy --rw-key-file RW`: удаляет **только поды**, которых нет в снимке «до», с именем по regex метки `^llmhub-[0-9a-f]{12}-[0-9a-f]{8}$`. Срабатывает при любом пороге:
  - живой под старше 75 мин;
  - `currentSpendPerHr > 0.30`;
  - подов `llmhub-*` больше 1.

  Удаляются все новые поды из diff. Тома и шаблоны только печатаются, код 5 — тест останавливается. Каждое удаление пишется строкой в `~/.local/state/llmhub-guard/actions.log` (без ключей).
- Юниты (D3, только на время теста):
  - `deploy/workstation/llmhub-runpod-guard.service` — `Type=oneshot`, `ExecStart=… watch --once --auto-destroy`, ключи и путь к снимку «до» из `EnvironmentFile` (`guard.env`: только пути);
  - `.timer` — `OnBootSec=60`, `OnUnitActiveSec=60`.

  Включение — в L0, **до первого пода**: `systemctl --user enable --now llmhub-runpod-guard.timer`. Отключение и удаление — в L8, после итоговой проверки «0 подов и spend 0»: `disable --now`, удалить юниты и `guard.env`.

## 4. Предохранители трат
| Слой | Значение |
|---|---|
| бюджет llmhub | `hard_cap_month = budget_month = budget_day = 4.00`, `budget_session 0.60`, `budget_job 0.10`, `reserve_usd 1.0`; Mock-ledger исключён (`ledger-exclude`) |
| размер | один пул `cloud-27b` (`provider: runpod`), UNIQUE(pool) → ≤ 1 инстанс; `max_live_pods: 1` в адаптере |
| цена | `pool.max_price_usd_h 0.30`, ceiling 0.50, проверка `cost_per_hr` после create |
| время | `idle_timeout_s 300`, `max_lifetime_s 3600`, `leases.max_ttl_s 1800`, `offer_wait_s 300`, `booting_s 600`, `loading_model_s 900` |
| неизвестный create | `create_visibility_grace_s 600`, `create_unknown_max_retries 0` → FAILED и инцидент high `create_unknown_unresolved` (F1) |
| доступ | `cloud_allowed: false` у всех, кроме `t372`; `providers.enabled: [runpod]` |
| kill-switch | `kill` (API), `kill --file` (reaper) |
| внешний сторож | §3.6: таймер `--user` с `--auto-destroy` (D3), ключ RO для чтения и RW только для удаления новых подов выше порогов |
| аккаунт | баланс ≤ $25, auto-pay выключен, Low balance alert включён (P0) |
| ноль после шага (F5) | 0 подов `llmhub-*` и `currentSpendPerHr == 0` **дважды с интервалом ≥ 60 с** |
| конец сессии | сессия не закрывается, пока нет 0 подов и `currentSpendPerHr == 0` (правило runbook) |
| разрешение (D1) | одно на весь живой тест: координатор спрашивает один раз перед L2b; дальше работает в пределах потолка $4 и останавливается при любом условии §6 |

## 5. Ключи и токены
- **RunPod на сервере.** Файл `/etc/llmhub/secrets/runpod`: одна строка, `0400`, `64100:0`.
  - Монтируется `:ro` только в `llmhub-api` и `llmhub-reaper` как `/run/secrets/runpod`, плюс `LLMHUB_RUNPOD_API_KEY_FILE`.
  - Доставка: `ssh root@65.109.95.35 'install -m 0400 -o 64100 -g 0 /dev/stdin /etc/llmhub/secrets/runpod' < ~/.config/runpod/api-key[-rw]`. Сначала RO-ключ (L1), затем rw (L2).
  - Значения нет в argv, env, `docker inspect`, `run-args/`, journald и `hashes.txt`.
- **RunPod на рабочей станции** (сторож): `~/.config/runpod/api-key` (RO) и `api-key-rw` (0600). В `guard.env` только пути.
- **GitHub (Q1).** Заказчик кладёт `~/.config/ghcr/token` (0600, `write:packages`). `publish-infer.sh`:
  1. `image_secret_scan.py` (E3); без `clean` пуш не делается;
  2. `docker login ghcr.io -u yvkalabuha --password-stdin < token`;
  3. `docker push`;
  4. digest пишется в `dist/<ver>/infer.digest`;
  5. `docker logout ghcr.io`.

  Видимость `public` ставит заказчик. Проверка: анонимный `docker pull …@sha256:…` после logout.
- **Токен весов** (F9): `exp = min(plan.deadline_unix, issued_at + token_ttl_max_s)` (≤ 30 мин); отзывается при выходе из LOADING_STATES.
  - В L4 после READY: `weights-token-check --op <op_id>` выводит токен внутри контейнера, делает запрос на 9443 и печатает только HTTP-код. Ожидается `401`.
  - Логи пода из консоли → `scan-secrets --op <op_id> < logs.txt` печатает только число совпадений. Ожидается 0.
- **После теста** (L8): `shred -u` ключа на сервере, `providers.enabled: [mock]`. Заказчик отзывает rw-ключ RunPod и GitHub-токен.

## 6. Живой тест (координатор): шаги, цены, остановка
Ставка: 3090 Community $0,22/ч плюс диск 40 ГБ ≈ $0,005/ч ⇒ **≈ $0,225/ч ≈ $0,0038/мин**.
После **каждого** платного шага: 0 подов и `currentSpendPerHr == 0` дважды с интервалом ≥ 60 с; `guard diff` без новых `llmhub-*`.

| Шаг | Действие | Под | Оценка $ |
|---|---|---|---|
| P0 | Публичный `openapi.json` RunPod (без ключа) → `tests/fixtures/runpod/openapi.json`, тест A1b зелёный. GHCR: скан → push → public → анонимный pull по digest, **размер образа** (F7c) → digest в конфиг (тест E2). Консоль: auto-pay off, alert on, баланс ≤ $25 | 0 | 0 |
| L0 | `guard snapshot` «до» (RO). **Включить таймер сторожа с `--auto-destroy`** (D3), проверить один прогон в журнале `--user` | 0 | 0 |
| L1 | RO-ключ на сервер. Релиз 0.3.0 (s1/s3/s6). `provider-check`: офферы, `list_owned`=0, баланс, биллинг. `guard record` → `recorded/`; A2b и F-scan зелёные. `ledger-exclude --provider mock --yes` (бэкап до). Конфиг теста (E2), `config-check` | 0 | 0 |
| L2 | **Единственное разрешение заказчика (D1)** — записать в журнал (≤ $5, RunPod, срок — сессия теста). rw-ключ на сервер. **L2b lifecycle-под** (advocate F10): `lifecycle-probe` через `RunpodAdapter`, малый официальный образ (`nvidia/cuda:12.8.1-base-ubuntu24.04@sha256:…`, `sleep 600`), без весов. Проверить: `name == label`, `cost_per_hr`, время create → виден в `list_owned`, сторож видит. Затем `destroy`; время исчезновения → `list_staleness_s` | ≈ 4 мин | ≈ 0,02 |
| L3 | **Старт 1: только HF + учение «контроллер недоступен»** (advocate F8/F9, D2). Prewarm `t372` → READY; замер pull, скорости HF, фаз. **Контрольная точка**: первый READY и один ответ → дальше только после «да» координатора. 2 запроса (5k/300, 1,3k/4k), ток/с. Учение: `docker stop llmhub-api` на 5 мин → `kill --file` → reaper удаляет под ≤ 60 с без API → `docker start`, recovery, удалить `KILL`, `resume` | ~4 + 8 + 1 + 2 + 5 + 1 ≈ 21 мин | ≈ 0,08 |
| L4 | **Старт 2: `self_hosted` → HF, proxy.** Скорость Hetzner→RunPod; после READY `weights-token-check` → 401; логи пода → `scan-secrets` = 0; 3 запроса, один с ответом > 100 с; idle 5 мин → destroy | ~4 + 4 + 1 + 3 + 5 + 1 ≈ 18 мин | ≈ 0,07 |
| L5 | **Старт 3: `self_hosted`.** 1 запрос; idle → destroy | ≈ 15 мин | ≈ 0,06 |
| L6 | **Учение kill.** Старт; в LOADING_MODEL `kill` через API → 0 подов ≤ 60 с (`list_owned`, сторож, консоль); `resume` | ≈ 6 мин | ≈ 0,025 |
| L7 | Через ≥ 2 ч. **Главная сверка — Δ баланса** RunPod «до/после» в центах против суммы ledger, допуск max(5¢; 5 %) (F11). Биллинг **по каждому `podId`**: каждый podId есть в нашей БД, сумма ≈ ledger (F5). `get(<старые ref>)` → None | 0 | 0 |
| L8 | Удалить клиента `t372`; `providers.enabled: [mock]`; `shred` ключа. Итоговая проверка: 0 подов и spend 0 дважды ≥ 60 с. **Затем отключить и удалить таймер сторожа** (D3). Снимок «после», `diff` = 0 ресурсов, отчёт в журнал | 0 | 0 |
| | **Итого** | ≈ 64 мин пода | **≈ $0,25**; с запасом ×2 (медленные хосты, повтор шага) **≈ $0,50** |

**Запасной путь, если публикация в GHCR задерживается** (advocate F7a). Шаги L3′–L5′ идут через `lifecycle-probe` с официальным `ghcr.io/ggml-org/llama.cpp:server-cuda12-b12063@sha256:d5ff…`:
- `--hf-repo`; ключ llama-server — из `secret_env` (`LLAMA_ARG_API_KEY`);
- замеряются старт, HF и ток/с;
- учения kill и «контроллер недоступен» проходят на тех же подах (строка БД есть, метка наша);
- проверка токена весов и `self_hosted` откладываются; в отчёте пометка «частично».

**Условия остановки** (сразу `kill`, затем `guard destroy-leftovers --yes`, затем разбор):
1. **Ошибка разбора ответа RunPod** (`RunpodParseError`, код 4 сторожа; F2).
2. Под без строки в БД или вне шага; под жив > 5 мин после шага; `currentSpendPerHr ≠ 0` после шага; сработало автоудаление сторожа.
3. Найдены network volume или шаблон `llmhub-*`; в diff появился любой новый том или шаблон (F4).
4. Под RUNNING без прогресса к READY > 15 мин → `kill` (F5).
5. Инцидент critical (`unknown_owned_resource`, `kill_incomplete`, `ownership_mismatch`, `hard_cap`, `create_unknown_unresolved`) или high `tls_pin_mismatch`.
6. `cost_per_hr > 0,30` или `currentSpendPerHr > 0,30`.
7. Баланс упал от «до» на ≥ $3,00. Мягкие стопы: Δ ≥ $1,50 или ledger ≥ $2,00 → продолжать только с «да» заказчика.
8. Шаг длится > 2× оценки; баланс < $15; rw-ключ получил 401/403; ключ или токен виден в выводе; reaper `unhealthy`.

**Наблюдения, не критерии** (F13; n = 3 старта, статистики нет):
- READY ≤ 10 мин в 80 %;
- медиана с `self_hosted` ≤ 5 мин;
- прогноз ±30 %;
- ток/с против 75–80;
- скорость Hetzner→RunPod и HF;
- размер и время pull образа.

Всё это идёт в отчёт. Если цифры меняют выбор модели или GPU — новый ADR.

## 7. Шаги (TDD)
Команда шага: `uv run pytest tests/<файл> -q && uv run ruff check <файлы> && uv run pyright <файлы>`.

**Поток A — адаптер (sonnet)**
- [x] **A1.** `test_t372_no_runpod_network.py`: `getaddrinfo` для `*.runpod.io` и `*.runpod.net` в тестах → `PermissionError`. Затем guard в `conftest.py`. Синтетические фикстуры.
- [x] **A1b.** `test_t372_runpod_schema.py`: каждое поле `create_body` (включая `allowedCudaVersions`) есть в схеме `POST /pods` из `openapi.json`; поля, которые читает `PodInfo`, есть в схеме ответа пода. Без файла тест пропускается; в P0 координатор требует зелёный (F2a, advocate F7b).
- [x] **A2.** `test_t372_runpod_api.py` → `runpod_api.py`:
  - парсеры: running/exited/pending, без `machine`; `env` отброшен, `repr` без env;
  - `Bearer`; ключа нет в URL и в `str(err)`;
  - 404 → None/False;
  - GraphQL `errors` при 200 → `RunpodHttpError`;
  - неожиданная форма → `RunpodParseError`;
  - ошибка второй страницы → исключение.

  **A2b** — параметризованный тест по `recorded/*.json`, обязателен после L1.
- [x] **A3.** `test_t372_runpod_errors.py`: строки §3.4; `message` без секрета.
- [x] **A4.** `test_t372_runpod_adapter.py` → `runpod.py`, правила §3.3:
  - один POST при таймауте;
  - отказ без digest, при `max_live_pods`, при цене;
  - удаление при `name != label` или цене после create;
  - тело без `networkVolumeId`/`templateId`, с `allowedCudaVersions`;
  - `destroy("vol:…"/"tpl:…")` → ProviderError без HTTP;
  - `list_owned` при сбое любого списка → исключение;
  - `billing_since`, `balance`.
- [x] **A5.** `tests/runpod_fake.py`: поды pending→running, лаг удаления и видимости, чужие поды, `inject_unknown_create`, `visible_after_s`. `RunpodHarness`. `test_t372_runpod_contract.py`: `run_contract` с `lagging` False/True; C10.
- [x] **A6.** Ядро на фейке (`test_core_*` в `test_t372_runpod_adapter.py`; ждёт B1, C2):
  - provisioning → E2 → teardown → ноль с `list_staleness_s=30`;
  - kill удаляет только HMAC-валидные;
  - reaper: под без строки → `unknown_owned_resource`;
  - **F1**: таймаут create, под виден через 150 с, grace 600 → второго POST нет, строка усыновляет под;
  - не виден 600 с → FAILED + `create_unknown_unresolved`, второго POST нет;
  - то же через `recovery` после перезапуска.

**Поток B — конфиг и фабрика (sonnet)**
- [x] **B1.** `test_t372_config.py` → `models.py` (§3.5). Каждый случай даёт ошибку:
  - `host_blacklist_s < 3600`;
  - `runpod` в `enabled`, но нет секции;
  - grace < 600;
  - `create_unknown_max_retries ≠ 0`;
  - image без digest;
  - `transport ≠ proxy`;
  - цена выше ceiling;
  - неизвестный `gpu`;
  - `token_ttl_max_s > 1800`;
  - неизвестный провайдер.

  Регрессия: `uv run pytest tests/test_fix2_t370.py tests/test_a_config.py -q`.
- [x] **B2.** `load_provider_key`: лениво, только `_FILE`; биты other → ошибка; значения нет в сообщениях.
- [x] **B3.** `test_t372_factory.py` → `factory.py`; `server` и `reaper` используют фабрику. Регрессия: `uv run pytest tests/test_f_reaper_process.py tests/test_int_e2e.py -q`.

**Поток C — ядро (sonnet; ждёт B1)**
- [x] **C1.** Сбой `list_offers` → `last_error = provider_api_error` (через `normalize_error`). По `offer_wait_s` — FAILED с этой причиной, `_reason_of` → `PROVIDER_API_ERROR`. «Нет оффера» остаётся `gpu_unavailable`.
- [x] **C2.** F1: в `provisioner._provisioning` и `recovery.py`, если после grace под не найден и `attempts - 1 >= create_unknown_max_retries`, вызывать `_fail_provisioning(..., "create_unknown_unresolved")` и писать инцидент high. Повторного `create_called_at=NULL` нет. Значение по умолчанию 1 сохраняет поведение Mock: `uv run pytest tests/test_f_provisioner.py tests/test_c_recovery.py -q`.
- [x] **C3.** Хост из чёрного списка после create → `destroy`, `exclude_hosts`, повтор в пределах `create_max_attempts`, инцидент low `blacklisted_host_assigned`.
- [x] **C4.** F9: в `instance_env.build_material` `exp = min(deadline_unix, issued_at + token_ttl_max_s)`. Тест: при 40-минутном плане токен живёт ≤ 1800 с.

**Поток D — ledger и CLI (sonnet; D3 ждёт A4, B3)**
- [x] **D1.** `0004_ledger_excluded.sql`: `ALTER TABLE ledger ADD COLUMN excluded_at REAL`, `excluded_reason TEXT`. Все суммы ledger (`spent`, `session_cost`, сверка, `/admin/budget`) считаются с `excluded_at IS NULL`. Тест миграции и сумм. Grep всех `FROM ledger` — часть шага.
- [x] **D2.** `ledger-exclude --provider mock --yes`:
  - бэкап до операции;
  - одна транзакция `UPDATE … SET excluded_at=?, excluded_reason='t372-mock'`, инцидент info;
  - отказ, если `mock` в `enabled` или есть живые mock-instances;
  - без `--yes` — dry-run с суммой;
  - строки не удаляются (F14).
- [x] **D3.** Команды CLI, тесты на фейке:
  - `provider-check --provider runpod` — только чтение;
  - `lifecycle-probe --image <digest> [--max-s 600]`: строка instances пула `probe`; create → виден → RUNNING → destroy → ноль; печать замеров; `destroy` в `finally`;
  - `scan-secrets --op <op_id>` — stdin, печать числа совпадений;
  - `weights-token-check --op <op_id> --url …` — печать HTTP-кода.

**Поток E — деплой и образ (sonnet; ждёт B1, B2)**
- [x] **E1.** `llmhub-deploy.sh`: s4 проверяет `secrets/runpod` (`0400 64100:0`, если файл есть). s6 монтирует его и `LLMHUB_RUNPOD_API_KEY_FILE` только в api и reaper, только при непустом файле. Тест: у weights-auth и nginx монтирования нет, значения нигде нет.
- [x] **E2.** `deploy/config.t372.example.yaml` по §4: пул `cloud-27b`, `gpu: RTX3090`, `disk_gb: 40`, image `ghcr.io/yvkalabuha/llmhub-infer@sha256:<64>`. Тест: `load_config` проходит; значения предохранителей проверены утверждениями; image соответствует regex digest и совпадает с `dist/<ver>/infer.digest`, если файл есть.
- [x] **E3.** `scripts/image_secret_scan.py <image.tar>`. После `docker save` сканируются **каждый слой** и config/history:
  - шаблоны: `-----BEGIN .*PRIVATE KEY`, `ghp_`, `github_pat_`, `hf_`, `rpa_`, `lh_`, `sk-`, `.env`, `id_rsa`, `LLMHUB_*_SECRET=`;
  - whiteout: секрет в нижнем слое, удалённый в верхнем.

  Вердикт `clean`/`dirty`; печатаются только пути. Плюс `publish-infer.sh` (§5). `test_t372_image_scan.py` на синтетическом tar: секрет в нижнем слое, удалённый в верхнем → `dirty` (critic F8).

**Поток F — внешний сторож (sonnet; независим)**
- [x] **F1.** `test_t372_guard.py` → `runpod_guard.py` (§3.6):
  - AST: нет импортов `llmhub`;
  - сырой JSON; код 4 на неожиданной форме;
  - `diff` в центах, код 3;
  - `destroy-leftovers` и `--auto-destroy`: не трогают чужой под, наш под из «до» и наш новый ниже порога; удаляют новый выше порога (75 мин / 0,30 $/ч / > 1 пода);
  - том или шаблон → код 5, без удаления;
  - ключ не попадает в stdout/stderr/файлы; `actions.log` без ключей.

  HTTP подменяется opener'ом `urllib`.
- [x] **F2.** `record` по allowlist + `test_t372_fixture_scan.py`: все `tests/fixtures/runpod/**` проверяются на шаблоны E3, e-mail и поля вне allowlist (F7).
- [x] **F3.** Юниты `--user` (`.service`, `.timer`) и `guard.env.example` (только пути). Тест — статическая проверка:
  - `ExecStart` содержит `watch --once --auto-destroy`;
  - в `EnvironmentFile` нет значений ключей;
  - в `docs/t372-live-test.md` есть шаги enable (L0) и disable + удаление (L8).

**Интеграция (I)**
- [x] **I1.** Экспорт в `provider_adapters/__init__.py`. `docs/t372-live-test.md`: §6 как чек-лист с командами, правило конца сессии, включение и снятие сторожа. `docs/runbook.md` §8. `deploy/README.md` (§5).
- [x] **I2.** `uv run pytest tests/test_t372_*.py tests/test_b_contract.py tests/test_f_provisioner.py tests/test_c_recovery.py tests/test_f_reaper.py tests/test_f_killswitch.py tests/test_e_ledger.py tests/test_fix2_t370.py -q`, затем `uv run ruff check` и `uv run pyright`. Полный прогон — перед релизом 0.3.0.
- [x] **I3.** Релиз `nice -n 10 deploy/docker/build.sh` → 0.3.0. Деплой делает координатор.
- [x] **L2b-fix.** Реальный 500 «no instances currently available» → `GPU_UNAVAILABLE` (определённый отказ); create перебирает версии CUDA по одной (13.0 → 12.9 → 12.8); `lifecycle-probe` пишет `attempts`; фейк моделирует ёмкость по версиям (`tests/test_t372_cuda_fallback.py`). dist/0.3.2 пересобран на месте.
- [x] **L2b-rate (0.3.5).** Находки ночи 2026-11-05: (1) `lifecycle-probe` — определённый отказ без ref переводит строку `_probe` сразу в STOPPED (`zero_confirmed_at`, без листинга); старая строка ERROR без ref и без неизвестного исхода очищается после grace и пустого листинга метки; ref или неизвестный исход — строго, ждёт контроллер. (2) REST 429 → `rate_limited`, определённый отказ (и на POST); `RestLimiter` в `RunpodApi` (удержание по Retry-After или `rest_hold_s` 900 с: POST не шлётся, DELETE всегда, чтения пачкой раз в минуту; бюджет `post_budget_per_hour` 60), общее состояние `controller_state` `rest_rate:<provider>` через `GuardedAdapter`; rate-limited не ретраится и не считается брейкером; ожидание DELETE ≤ `destroy_backoff_max_s`. (3) Провижнер: все офферы — определённый no_capacity → ожидание пула `retry.capacity_backoff_s` 120 → 300 → 600 ±20 %; удержание/бюджет REST — без раунда; проверка стока GraphQL `secureCloud:false` до POST (`stock_precheck`); `/admin/status` — `capacity_wait`, `providers[].rest`. (4) Сторож: тома/шаблоны/registry/endpoints раз в 10 проходов, 429 → код 9, таймер 120 с. (5) `docs/t372-live-test.md` — «Ожидание мощности и бюджет REST», runbook §8. Тесты: `test_t372_rate_capacity.py`, `test_t372_probe_rows.py`, `test_t372_guard_budget.py`.

Ревью create/reconcile (A4–A6, C2): 2 независимых цикла (логика и гонки; сбои и перезапуски), ≤ 3 раундов.

## 8. Параллельные потоки
| Поток | Шаги | Владеет | Зависит от | Модель |
|---|---|---|---|---|
| A | A1–A6 | файлы A (§3.2) | A6 ← B1, C2; до B1 — заглушка `RunpodConfig` с полями §3.5 | sonnet |
| B | B1–B3 | файлы B | B3 ← A4 (конструктор) | sonnet |
| C | C1–C4 | `provisioner.py`, `recovery.py`, `instance_env.py`, `test_t372_core_fixes.py` | B1 | sonnet |
| D | D1–D3 | миграция, `cost_tracker/ledger.py` и суммы ledger, `admin_cli.py`, тесты D | D3 ← A4, B3 | sonnet |
| E | E1–E3 | `llmhub-deploy.sh`, `config.t372.example.yaml`, `image_secret_scan.py`, `publish-infer.sh`, тесты E | B1, B2 | sonnet |
| F | F1–F3 | `scripts/runpod_guard.py`, `deploy/workstation/*`, тесты F | — | sonnet (F3 — haiku) |
| I | I1–I3 | `__init__.py`, docs, `deploy/README.md`, BOARD, STATUS | все | sonnet |

Порядок:
1. Сразу: A1–A5, B, D1–D2, F, E3.
2. После B1/B2: C, E1–E2.
3. После A4/B3: A6, D3.
4. Последним: I.

## 9. Риски
| Риск | Мера |
|---|---|
| Ложный ноль `list_owned` | исключение при любом сбое, странице или ошибке разбора; `list_staleness_s` из L2b; независимый сторож |
| Схема RunPod не совпадает | A1b по `openapi.json` до трат; запись в L1; стоп при parse error |
| Под появляется после grace → второй create | grace 600 с, 0 повторов, FAILED + сигнал (F1) |
| Сервер или контроллер умер при живом поде | `max_lifetime 3600`, автоудаление сторожем на рабочей станции (D3), малый баланс |
| Сторож удалит лишнее | только поды по regex метки, только новые относительно снимка «до», только выше порога; `actions.log`; тест F1 |
| Секреты в образе, фикстурах, логах пода | поуровневый скан (E3), allowlist записи + сканер (F2), `scan-secrets`, токен ≤ 30 мин и 401 после READY |
| `env` пода с ключом инстанса виден в консоли RunPod | ключ на инстанс, токен отзывается; Secrets RunPod — этап 5 (в отчёт) |
| Прокси RunPod рвёт > 100 с [Предп.] | `force_stream`; проверка в L4 |
| Публикация в GHCR задерживается | запасной путь F7a |
| Биллинг почасовой и грубый | главная сверка — Δ баланса в центах (F11); L7 через ≥ 2 ч |

## 10. Решения заказчика (2026-11-04)
| # | Решение |
|---|---|
| Q1 | Публичный образ GHCR, pull по digest. Токен `write:packages` даёт заказчик файлом `~/.config/ghcr/token`. Перед push — поуровневый скан |
| Q5 | Mock-ledger исключается (`excluded_at`) с бэкапом, не удаляется (critic F14) |
| D1 | Одно разрешение на весь живой тест. Координатор спрашивает один раз перед первым платным шагом (L2b), дальше идёт в пределах потолка $4 и останавливается при любом условии §6 |
| D2 | Учение «обрыв сети» заменено учением «контроллер недоступен»: `docker stop llmhub-api` на 5 мин, под удаляет reaper через `KILL`. Критерий в файле задачи сужен |
| D3 | Да: таймер `systemd --user` с автоудалением на рабочей станции только на время теста. Включается до первого пода (L0), снимается после итоговой проверки «0 подов и spend 0» (L8) |
| Q2, Q4 | Файл ключа — как в §5; после теста — откат на Mock и `shred` |
| открыто | Входит ли диск в `costPerHr` — выяснить в L2b (не блокирует) |

## 11. Ворота: сопоставление находок
| Находка | Где учтено |
|---|---|
| critic F1: grace ≥ 600, 0 повторов, FAILED + сигнал | §3.5 валидаторы, C2, A6 (под через 150 с → второго POST нет) |
| critic F2a: openapi.json до трат; F2b: сторож с независимым разбором; стоп при parse error | P0, A1b; §3.6, F1; §6 условие 1 |
| critic F3 / advocate F3: таймер `--user`, auto-destroy, правило конца сессии | §3.6, F3, §4, D3 |
| critic F4: удалять только поды | §3.3 п. 3, A4, F1, §6 условие 3 |
| critic F5: spend/h == 0 дважды, биллинг по podId, RUNNING без прогресса 15 мин | §4; §6 L7 и условие 4 |
| critic F6: `max_live_pods` считает **все** поды `llmhub-*`, кроме TERMINATED (EXITED/CREATED/RESTARTING тоже); любой сбой `list_pods` → отказ create | §3.3 п. 1 (`runpod.is_live`), A4: `test_max_live_pods_counts_every_status_but_terminated` (EXITED/CREATED/RESTARTING), `test_list_failure_refuses_create` |
| critic F10: учение «обрыв сети» | закрыто решением заказчика D2: учение «контроллер недоступен» (§6 L3); критерий в файле задачи уже сужен |
| critic F7: allowlist записи + сканер | §3.6 `record`, F2 |
| critic F8: поуровневый скан перед push | E3, §5 |
| critic F9: 401 после READY, логи пода, TTL ≤ 30 мин | C4, D3, §5, L4 |
| critic F11: главная сверка — Δ баланса в центах | L7, F1 `diff` |
| critic F12: урезание объёма | §1 «Вынесено», BOARD «Входящие» |
| critic F13: наблюдения, не критерии | §6 «Наблюдения», §12; в файле задачи «READY ≤ 10 мин в 80 % / медиана ≤ 5 мин / прогноз ±30 %» и «spike dstack» переписаны как наблюдения (implementer, 2026-11-04) |
| critic F14: исключать, а не удалять | D1, D2 |
| advocate F7a / F7b / F7c | §6 запасной путь; `allowed_cuda_versions` + A1b; P0 и L3 (размер образа, pull) |
| advocate F8, F9: сначала HF; HF вместе с учением «контроллер недоступен» | L3 |
| advocate F10: lifecycle-под и контрольная точка | L2b, D3 `lifecycle-probe`; L3 |

## 12. Приёмка
| Критерий задачи | Чем закрывается |
|---|---|
| Разрешение письменно | L2 (D1: одно на тест) |
| Spike dstack ≤ 1 дня | **вынесен** (F12): по умолчанию `RunpodAdapter`, desk-spike — новая задача. Модуль проходит `contract.py` (A5) |
| Платный тест ≤ $5: 3 холодных старта, учения kill и «контроллер недоступен» (D2), 0 утечек, баланс ≤ $25, alert, без auto-pay | L3–L5, L6, L3, снимки и `diff`, P0 |
| READY ≤ 10 мин / медиана ≤ 5 мин / прогноз ±30 % | **наблюдения** (F13) в отчёте |
| Замер Hetzner→провайдер; режим TLS | L4; TLS = `proxy` (direct вынесен) |
| Ledger против биллинга ≤ max($0,05; 5 %) | L7: Δ баланса в центах + по podId |
| Ключи не в репозитории, логах, отчётах | §5, E1, E3, F1, F2, D3 `scan-secrets` |
| Входящие T-372 | C1, B1 (`host_blacklist_s`); остальные вынесены (BOARD) |
| ruff, pyright, тесты; 2 цикла ревью create/reconcile | I2, ревью |
