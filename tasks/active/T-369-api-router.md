# T-369: 3b: API

> TL;DR: OpenAI-совместимый шлюз, tasks, leases, admin, ошибки, идемпотентность, OpenAPI и curl-примеры, адаптер llama-server, router ECONOMY/BALANCED/QUALITY, клиенты key+allowed_models+max_priority.

- Проект: llmhub
- Тип: impl
- Агент: analyst → implementer → reviewer
- Статус: in-progress
- План: tasks/plans/T-369-plan.md
- Приоритет: P1
- Зависит от: T-368
- Документы: decisions/ADR-0138-cloud-gpu-manager.md; /var/www/projectVoiceBot/platform/docs/research/T-367-cloud-gpu-manager.md (§6.10, §6.11, §6.12); T-367-tz-v2.md (ТЗ)

## Цель
Внешний контракт llmhub поверх ядра T-368: нормативные OpenAPI и raw HTTP/curl (клиент gameDev — C#/.NET), локальный путь к llama-server, детерминированный router.
## Критерии приёмки
- [ ] `POST /v1/chat/completions` (в т.ч. `stream`), `GET /v1/models` (псевдонимы), `/v1/tasks`, `/v1/leases`, `/admin/*` (hold, release, kill, resume, budget, client; только админ-ключ), `/healthz`, `/metrics`.
- [ ] Ошибки — объект OpenAI `error` с `code`, `reason`, `retryable`, `retry_after_s`; шесть причин недоступности облака покрыты тестами; обрезка — `X-LLMHub-Truncated`.
- [ ] Синхронное ожидание ≤ 8 мин (`X-LLMHub-Wait`), затем `503` + `Retry-After` + `task_id` (тест на ускоренных часах).
- [ ] Идемпотентность: `Idempotency-Key` 7 суток, неявная sha256(client+тело) ~15 мин, 409 при конфликте.
- [ ] Клиенты: ключ (в БД только хеш), `allowed_models`, `max_priority`; **`data_class` по умолчанию `private`** (только локально), облако — явный `cloud_allowed: true`; детектор секретов -> локально. Тесты отказа вне прав.
- [ ] `local_inference`: HTTP-клиент llama-server 127.0.0.1:8090/8091/8092 (в тестах — фейковый HTTP-сервер, реальные юниты не трогаем), CPU-семафор concurrency 1, EWMA задержки + периодическая проба занятости.
- [ ] `model_router`: правила ECONOMY / BALANCED (по умолчанию) / QUALITY, модели — псевдонимы конфига; облако недоступно -> локальная модель с пометкой; без LLM-советника.
- [ ] `docs/openapi.yaml` сверяется с приложением; `examples/`: curl, C#/.NET HttpClient, openai-python; контрактный тест: каждый curl-пример проходит против приложения на Mock.
- [ ] pytest, ruff, pyright чисто; ревью ≤ 3 раундов.
## Подзадачи
- [x] План + ворота (2026-11-04)
- [ ] api_gateway (openai, tasks, leases, admin, auth, errors)
- [ ] local_inference + model_router
- [ ] OpenAPI + examples + контрактный тест
## Ревью
<заполняет reviewer: находки, команды, результаты, вердикт>
## Журнал
- 2026-11-04: задача создана координатором при создании репозитория.
- 2026-11-04: analyst — план `tasks/plans/T-369-plan.md` (36 шагов, потоки G, H, R, W, M, I, O; таблица правил router H1–F3, таблица кодов ошибок, эндпоинты; MCP `llmhub_ask` — тонкий stdio-клиент HTTP API). Минимальные правки ядра: `Controller.wake()`, `complete` учитывает `cancel_requested`, `task_queue/local.py`, миграция 0003. Блокирующих вопросов нет; Q6 (реальный облачный InferenceBackend вне T-369/T-370) — координатору. Статус planned; дальше ворота critic + advocate (P1).
- 2026-11-04: analyst — находки ворот critic + advocate внесены в план (§11): MCP вынесен в T-373; `cancelled` → 409; перечитывание клиентов (SIGHUP, `/admin/reload_clients`); разрыв ≠ отмена; private/секрет никогда в облако (F2 + отрицательные тесты); детектор по всем строкам запроса; просрочка в очереди (`expire_overdue`, G3a); общий опросчик ожидания; CAS `complete_local`; страж портов 8090–8092; C# — `dotnet build` со skip. 34 шага, потоки G, H, R, W, I, O. Статус planned.
- 2026-11-04: implementer, поток G (фундамент) готов. Файлы: `configuration/api_models.py` (новый; `Config(ApiSection)` — одна правка `models.py`, плоские поля `local_backends/models/task_types/router/local/clients/api`, перекрёстные проверки `check_api_section`), миграция `0003_api.sql` (jobs.backend, jobs.warnings, индексы `jobs_local_queue`, `clients_key`), `task_queue/local.py`, правки ядра: `Controller.wake()`, `queue.complete` -> `cancelled` при `cancel_requested`, `JobSpec.est_cost_usd` (пишется в `submit`), `queue.requeue` при `restarts>5` и `pool IS NULL` -> `local.fail(local_unavailable, reason=max_restarts)`; `pyproject`: dev `+ openai` (sse-starlette не нужен: ответ stream — буфером). Тесты: `test_g_config/migration/local_queue/expire/core_hooks` (+ обновлены версии схемы 2->3 в `test_a_schema`, `test_review_r2_migrate`). Отклонения: проверка «алиас пула в models» пропускается при пустом `models` (иначе ломаются конфиги ядра T-368 с `pools.models=[m1]`); `task_type_max_tokens` на `default_factory` (RUF012 после смены базы); `requeue_local` берёт лимит из `jobs.max_attempts`, необязательный `max_attempts=` переопределяет (LocalConfig.max_attempts). Результаты: `pytest -m "not slow"` 1158 passed, ruff и pyright чисто.
