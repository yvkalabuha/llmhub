# llmhub

> TL;DR: Служба-диспетчер LLM: один OpenAI-совместимый API для локальных llama-server и арендуемой по требованию облачной GPU (RunPod/dstack/Vast через адаптеры). Минимизирует совокупную стоимость, бюджет $50/мес, облако всегда уничтожается после работы. Статус: ядро (T-368), веса и образ (T-370), API и router (T-369) — на Mock; деплой — T-371.

## Зачем
Постоянный сервер без GPU держит control plane и локальные модели; тяжёлые задачи уходят на облачную 3090 только когда это выгодно и разрешено политикой. Клиенты — VoiceBot, gameDev (C#/.NET) и другие.

## Как подключить клиента
1. Администратор заводит клиента (ключ хранится хешем): `allowed_models`, `max_priority`, `data_class` (по умолчанию `private` — только локально; облако — явный `cloud_allowed`).
2. Клиент задаёт окружение:
   ```
   OPENAI_BASE_URL=http://10.8.0.1:8095/v1
   OPENAI_API_KEY=<клиентский ключ llmhub>
   ```
3. Обычные запросы `/v1/chat/completions` (в т.ч. `stream`) работают как у OpenAI. Синхронное ожидание ≤ 8 мин, затем `503` + `Retry-After` + `task_id`; таймаут клиента должен быть больше (openai-python — 600 с). Долгие задачи и явные GPU-сеансы — `/v1/tasks`, `/v1/leases`. Руководство — `docs/integration.md`, контракт — `docs/openapi.yaml`, примеры (curl, openai-python, httpx, C#/.NET) — `examples/`.

## Запуск
- `python -m llmhub serve --config /etc/llmhub/conf/config.yaml` — процесс API: контроллер, локальный воркер и шлюз в одном event loop и на одном SQLite-соединении; окружение: `LLMHUB_LABEL_SECRET` (+ `LLMHUB_INSTANCE_SECRET`/`LLMHUB_WEIGHTS_SECRET` при пулах с весами), ключи llama-server — в переменных `local_backends.*.api_key_env`. `SIGHUP` перечитывает клиентов, `SIGTERM` — штатная остановка.
- `python -m llmhub.reaper --config …` — сторож, отдельный процесс.
- `python -m llmhub keys new --name <client>` — новый ключ клиента (печатается один раз) и его sha256 для конфига.
- `uv run python scripts/export_openapi.py` — пересобрать `docs/openapi.yaml` после изменения маршрутов (тест сверяет).

## Облачный под
Образ `llmhub-infer` (`infer-image/`, см. его README): llama.cpp + фронт с ключом API на инстанс и загрузчиком весов (наш сервер весов → HF). Сервер весов и заметки установки — `deploy/README.md`. Весов и секретов в образе нет; push — только с разрешения заказчика.

## Разработка
`uv sync`, `uv run pytest`, `uv run ruff check`, `uv run pyright`. Процесс и правила безопасности — `CLAUDE.md`; решения — `DECISIONS.md`; задачи — `BOARD.md`.
