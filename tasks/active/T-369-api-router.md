# T-369: 3b: API

> TL;DR: OpenAI-совместимый шлюз, tasks, leases, admin, ошибки, идемпотентность, OpenAPI и curl-примеры, адаптер llama-server, router ECONOMY/BALANCED/QUALITY, клиенты key+allowed_models+max_priority.

- Проект: llmhub
- Тип: impl
- Агент: analyst → implementer → reviewer
- Статус: todo
- План: tasks/plans/T-369-plan.md (создаёт analyst)
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
- [ ] План + ворота
- [ ] api_gateway (openai, tasks, leases, admin, auth, errors)
- [ ] local_inference + model_router
- [ ] OpenAPI + examples + контрактный тест
## Ревью
<заполняет reviewer: находки, команды, результаты, вердикт>
## Журнал
- 2026-11-04: задача создана координатором при создании репозитория.
