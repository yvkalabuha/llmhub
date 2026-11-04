# Интеграция клиентов с llmhub

> TL;DR: **Черновик.** Каркас руководства для клиентов (gameDev на C#/.NET, VoiceBot T-366): OpenAI-совместимый шлюз, `tasks`/`leases` по raw HTTP, таймауты. Нормативный контракт — OpenAPI и curl-примеры (появятся в T-369/T-371).

## 1. Подключение
- Базовый URL `http://10.8.0.1:8095/v1` (только VPN), заголовок `Authorization: Bearer <ключ клиента>`.
- `data_class` клиента по умолчанию `private` (только локальные модели); облако включается в конфиге (`cloud_allowed`).

## 2. OpenAI-совместимый шлюз
- `POST /v1/chat/completions` (+ `stream`), `GET /v1/models` (псевдонимы моделей).
- Заголовки: `Idempotency-Key` (хранится 7 суток), ответ `X-LLMHub-Truncated`, `X-LLMHub-Wait`.
- Ошибки: объект OpenAI `error` с `code`, `reason` (6 причин недоступности облака), `retryable`, `retry_after_s`.

## 3. Задачи (`/v1/tasks`)
- Создание, статус, результат, отмена; приоритеты urgent/normal/low; `low` копится пакетами (5 задач / 30 мин, ожидание ≤ 90 мин).
- Для запросов дольше синхронного окна (≤ 8 мин): `503` + `Retry-After` + `task_id`.

## 4. Leases (`/v1/leases`)
- Явный lease с пульсом (TTL 10 мин) удерживает GPU; неявные — на задачу.

## 5. Таймауты клиента
- Таймаут HTTP-клиента > 8 мин (openai-python — 600 с); долгие задачи — через `/v1/tasks`.

## 6. Примеры
- curl, C#/.NET `HttpClient`, openai-python — каталог `examples/` (TODO T-369/T-371).
