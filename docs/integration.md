# Интеграция клиентов с llmhub

> TL;DR: Руководство для клиентов (gameDev на C#/.NET, VoiceBot, агенты): OpenAI-совместимый `/v1/chat/completions` (+ SSE), `/v1/tasks` и `/v1/leases` по raw HTTP, коды ошибок, заголовки, таймауты, правила `data_class`. Нормативный контракт — `docs/openapi.yaml` (генерируется `scripts/export_openapi.py`, тест сверяет с приложением) и примеры `examples/` (каждый исполняется в тестах против живого приложения).

## 1. Подключение
- Базовый URL `http://10.8.0.1:8095` (только VPN); OpenAI-клиентам — `OPENAI_BASE_URL=http://10.8.0.1:8095/v1`.
- Ключ клиента: `Authorization: Bearer lh_...`. Выдаёт администратор (`python -m llmhub keys new --name <client>` печатает ключ один раз и его sha256; в конфиге и БД — только хеш). Новый клиент подключается без перезапуска (`SIGHUP` или `POST /admin/reload_clients`); перечитывание применяет и `api.admin_key_sha256` (ротация admin-ключа). Клиент, которого удалили или у которого сняли `cloud_allowed`, сразу теряет облако — и при перечитывании, и при перезапуске службы с исправленным конфигом: его облачные задачи в очереди отменяются (`cancelled`, в `error.reason` — `cloud_not_allowed`), явные и prewarm lease закрываются (`revoked`), продление lease и новая облачная задача → `403 cloud_not_allowed` (в том числе запрос, который был в обработке в момент перечитывания). Уже выполняющаяся облачная задача дорабатывает (GPU оплачен); если её прервёт перезапуск или сбой инстанса, в облачную очередь она не вернётся, а будет отменена с тем же `cloud_not_allowed`.
- У клиента в конфиге: `allowed_models` (псевдонимы или `*`), `max_priority`, `default_priority`, `default_policy`, `cloud_allowed` (по умолчанию `false`).
- `GET /v1/models` — псевдонимы, доступные клиенту, и `auto` (поле `llmhub`: `local`, `cloud`, `ctx_tokens`).
- `/healthz` (без ключа) — жив ли процесс; `/readyz` — жив ли цикл контроллера.

## 2. Конфиденциальность: `data_class`
- Эффективный класс = клиент ∧ заголовок `X-LLMHub-Data-Class` ∧ поле тела `llmhub.data_class`. Любой источник может только **сузить** до `private` (повторённый заголовок: хватает одного `private`); расширить до `cloud_allowed` можно лишь в конфиге клиента.
- `private` (по умолчанию) — **только локальные модели**: ни правилом политики, ни запасным путём, ни перезапуском такая задача в облако не уходит. Нет локальной модели у псевдонима → `403 forbidden` (`reason=private_requires_local`); локальная недоступна → `503 local_unavailable`.
- Детектор секретов сканирует все строки запроса (сообщения, content-parts, `tool_calls.arguments`, ответы tools, `tools`, `response_format`): ключи API, пароли в `KEY=…`/JSON/YAML, JWT, `https://user:pass@host`, приватные ключи OpenSSH/PGP. Найден секрет → задача только локальная, заголовок ответа `X-LLMHub-Warning: secret_detected`; у облачного псевдонима без локальной модели → `403`. Детектор эвристический (страховка, гарантию даёт только `private`): секрет в base64, разбитый на несколько сообщений или с zero-width-символами он не найдёт.

## 3. OpenAI-совместимый шлюз
- `POST /v1/chat/completions` — тело OpenAI (`model`, `messages`, `tools`, `response_format`, `max_tokens`/`max_completion_tokens`, `stream`, `stream_options.include_usage`; прочие поля уходят модели как есть). `model` — псевдоним или `auto`.
- Параметры llmhub — заголовками или объектом тела `"llmhub": {priority, policy, deadline_s, task_type, data_class}` (поле главнее заголовка).

### Заголовки запроса
| Заголовок | Значение |
|---|---|
| `Idempotency-Key` | 1..255 печатных ASCII; хранится 7 суток; тот же ключ с другим телом → `409 idempotency_conflict` (+ `task_id`). Без ключа одинаковое тело того же клиента ~15 мин присоединяется к той же задаче |
| `X-LLMHub-Priority` | `urgent` / `normal` / `low` (не выше `max_priority` клиента, иначе `403`) |
| `X-LLMHub-Policy` | `ECONOMY` / `BALANCED` (умолчание) / `QUALITY` |
| `X-LLMHub-Deadline` | секунды от сейчас; задача, не начавшаяся к сроку, → `failed/deadline_exceeded` (`504`) |
| `X-LLMHub-Wait` | синхронное окно 1..480 с (умолчание 480) |
| `X-LLMHub-Task-Type` | тип задачи из конфига сервера (`max_tokens`, «размышления») |
| `X-LLMHub-Data-Class` | `private` / `cloud_allowed` (только сужает) |
| `X-LLMHub-Lease` | id явного lease, под которым идёт запрос |

### Заголовки ответа
| Заголовок | Значение |
|---|---|
| `X-LLMHub-Task` | id задачи (по нему — `/v1/tasks/{id}`) |
| `X-LLMHub-Backend` | `local:<бэкенд>` или `cloud:<пул>` |
| `X-LLMHub-Fallback` | ответ получен запасным путём. Запрос, которому облако разрешено (`cloud_allowed`, без секретов, у псевдонима есть пул), выполнен локально, потому что облако недоступно: значение — причина (одна из шести, §6, или `controller_halted`); ставится и тогда, когда политика и так выбрала бы локальный путь. Облачный ответ с `local_unavailable` — локальная модель была недоступна, задачу взяло облако. У `private`/секретных запросов заголовка нет: облако для них не вариант |
| `X-LLMHub-Truncated` | `1`, если `finish_reason=length` |
| `X-LLMHub-Cost-Estimate` | оценка стоимости облака, USD |
| `X-LLMHub-Warning` | через запятую: `secret_detected`, `economy_stage`, … |

### Синхронное окно и таймауты клиента
- Сервер ждёт результат до 480 с (`X-LLMHub-Wait` — меньше). Не успел → `503 queue_wait_exceeded` + `Retry-After` + `task_id`; **задача остаётся в очереди**. Повтор того же запроса (тот же `Idempotency-Key` или то же тело) присоединяется к ней.
- Таймаут HTTP-клиента должен быть **больше 480 с**: openai-python — `timeout=600`, C# — `HttpClient.Timeout = 10 мин`, curl — `--max-time 600`. Прокси между клиентом и llmhub — `proxy_read_timeout` > 480 с и `proxy_buffering off` для SSE (T-371).
- openai-python сам повторяет `409` и `5xx` (`max_retries`, по умолчанию 2), если сервер не запретил: llmhub в каждом ответе об ошибке ставит `x-should-retry: true|false` (= `error.retryable`), поэтому `409 cancelled`, `504 deadline_exceeded`, `503 controller_halted` и прочие неповторяемые ошибки SDK не повторяет. Повтор `503 queue_wait_exceeded` присоединяется к той же задаче (общее ожидание до ~24 мин). `Retry-After` до 120 с SDK выдерживает; **если `Retry-After` больше 120 с, SDK не повторяет вовсе** и сразу поднимает исключение. Сервер ставит `Retry-After` по ETA в пределах `api.retry_after_min_s`..`api.retry_after_max_s` (по умолчанию до 900 с), так что при длинной очереди повтора от SDK не будет — для долгих задач `/v1/tasks`.
- **Разрыв соединения не отменяет задачу**: результат остаётся доступен по `task_id` (`X-LLMHub-Task` или `error.task_id`). Повтор после сетевой ошибки — с тем же `Idempotency-Key`.

### Stream (SSE) в v1
- Заголовки ответа приходят сразу, затем комментарии `: keep-alive` каждые 15 с, пока задача ждёт и идёт. **Первый чанк с текстом приходит только в конце** (ответ отдаётся буфером), затем `finish_reason`, `usage` (при `include_usage`) и `data: [DONE]`. `tool_calls` (с `index`) и `reasoning_content` передаются в дельтах.
- `X-LLMHub-Fallback` в stream-ответе ставится по решению, принятому при постановке (заголовки уходят до завершения задачи); запасной путь, выбранный позже ядром (облако отказало уже в работе), виден только в `GET /v1/tasks/{id}` (`fallback`).
- Ошибка после отправки заголовков приходит событием `data: {"error": {...}}` без `[DONE]` (openai-python поднимает `APIError`).

## 4. Задачи (`/v1/tasks`)
1. `POST /v1/tasks` (тело как у chat) → `202` + `Location` + `{"id","status","route","backend"}`; дубликат → `200` с тем же `id`. Исключение: если задача с этим `Idempotency-Key` упала с `retryable=true`, ключ переходит к новой задаче (`202`, новый `id`); упавшая с `retryable=false` или отменённая возвращается как есть.
2. `GET /v1/tasks/{id}` → статус, `eta_s`, маршрут, `est_cost_usd`/`cost_usd`, `fallback`, `warnings`, `error`. Опрашивайте с паузой ≈ `eta_s/4` (не чаще раза в 0,5 с, не реже раза в 30 с).
3. `GET /v1/tasks/{id}/result` → `chat.completion` с заголовками ответа; не готово → `409 task_not_ready` (`retry_after_s`); упала → объект ошибки задачи; отменена → `409 cancelled`.
4. `POST /v1/tasks/{id}/cancel`: в очереди → `cancelled`; локальная в работе → вызов рвётся, `cancelled`; **облачная в работе → `cancel_requested`, итог `cancelled`, уже потраченное время GPU оплачено**; завершённая — без изменений.
- Чужая задача неотличима от несуществующей (`404`).
- Приоритеты: `urgent` > `normal` > `low`; `low` для облака копится пакетами. Срок жизни задачи в очереди задаёт только `deadline_s`/`X-LLMHub-Deadline`: отдельного `max_queue_wait_s` в API v1 нет (одноимённое поле ядра — триггер запуска GPU, а не срок задачи).
- Облачная задача, которую отверг сам llama-server (4xx, например контекст длиннее окна), завершается `completed` с `finish_reason="error"` и телом ответа llama-server; повторять её без изменений бессмысленно. Потеря пода или сети — повтор на стороне llmhub, клиент видит только задержку.

## 5. Leases (`/v1/leases`)
- Нужен `cloud_allowed` у клиента и псевдоним с облачным пулом (иначе `403`/`404`).
- `POST /v1/leases {"model", "ttl_s": 30..3600, "prewarm": bool}` → `201 {"id","pool","kind","ttl_s","expires_at"}`; `prewarm=true` сразу поднимает GPU.
- **Пульс**: `POST /v1/leases/{id}/renew` чаще, чем `ttl_s` (например, каждые `ttl_s/3`); пропуск → lease истекает (`410 lease_expired`), GPU отпускается по общим правилам простоя.
- `POST /v1/leases/{id}/close {"final": true}` — закрыть (идемпотентно); `final` — GPU больше не нужна.
- Запросы под lease — заголовок `X-LLMHub-Lease: <id>`; неявные lease на задачу llmhub ставит сам.

## 6. Ошибки
Объект OpenAI: `{"error": {"message", "type", "code", "reason", "retryable", "retry_after_s", "task_id", "fallback"}}`. Сообщения не содержат текста запроса, ключей и путей.

| `code` | HTTP | `retryable` | Когда |
|---|---|---|---|
| `unauthorized` | 401 | нет | нет/неверный ключ |
| `forbidden` | 403 | нет | модель вне `allowed_models`, приоритет выше `max_priority`, private/секрет без локальной модели, lease без `cloud_allowed`, клиентский ключ на `/admin` |
| `invalid_request` | 400 | нет | тело/заголовки не прошли проверку |
| `request_too_large` | 413 | нет | тело больше лимита (4 МБ) |
| `context_too_large` | 400 | нет | запрос не помещается в контекст модели |
| `model_not_available` | 404 | нет | неизвестный псевдоним |
| `not_found` | 404 | нет | задача/lease не найдены или чужие |
| `idempotency_conflict` | 409 | нет | тот же ключ, другое тело (`task_id` существующей) |
| `task_not_ready` | 409 | да | результат ещё не готов |
| `lease_expired` | 410 | нет | lease истёк |
| `lease_closed` | 409 | нет | lease закрыт |
| `conflict` | 409 | нет | admin: hold отклонён, resume запрещён |
| `queue_wait_exceeded` | 503 | да | синхронное окно истекло (`Retry-After`, `task_id`, задача остаётся) |
| `cloud_unavailable` | 503 | по `reason` | облако недоступно, а локально нельзя (нет локальной модели) |
| `local_unavailable` | 503 | да | локальный бэкенд недоступен / попытки исчерпаны |
| `controller_halted` | 503 | нет | служба остановлена (HALTED/KILL), локально нельзя |
| `busy` | 503 | да (1 с) | БД занята |
| `deadline_exceeded` | 504 | нет | дедлайн прошёл до старта |
| `upstream_timeout` | 504 | да | таймаут модели после попыток |
| `cancelled` | 409 | нет | результат отменённой задачи |
| `internal_error` | 500 | да | прочее |

Шесть причин недоступности облака (`reason` у `cloud_unavailable`, значение `X-LLMHub-Fallback`): `provider_api_error`, `gpu_unavailable`, `deployment_error` — временные (`retryable=true`); `insufficient_balance`, `quota_exceeded`, `budget_limit_reached` — нет (нужен оператор). Если у псевдонима есть локальная модель, запрос с разрешённым облаком выполняется локально (`200` + `X-LLMHub-Fallback`).

Повторять стоит только `retryable=true`, с паузой `retry_after_s`/`Retry-After` и тем же `Idempotency-Key`: после итоговой повторяемой ошибки тот же ключ запускает новую задачу (§4), после неповторяемой возвращает ту же ошибку — нужен новый ключ и, как правило, оператор.

## 7. Примеры (`examples/`)
Все используют только `$LLMHUB_URL`, `$LLMHUB_KEY` (`$LLMHUB_ADMIN_KEY` — admin), openai-python — `OPENAI_BASE_URL`/`OPENAI_API_KEY`; необязательно `$LLMHUB_MODEL` (умолчание `auto`). Каждый исполняется в `tests/test_o_examples.py` против живого приложения на Mock.
- curl (`bash`, без `jq`): `examples/curl/01_models.sh`, `examples/curl/02_chat.sh`, `examples/curl/03_chat_stream.sh`, `examples/curl/04_task_submit_poll_result.sh`, `examples/curl/05_task_cancel.sh`, `examples/curl/06_lease_lifecycle.sh`, `examples/curl/07_idempotency.sh`, `examples/curl/08_errors.sh`, `examples/curl/09_admin_status.sh`.
- Python: `examples/python/openai_chat.py` (openai-python: chat, stream, заголовки, ошибки, `timeout=600`), `examples/python/raw_tasks.py` (httpx: задача с `Idempotency-Key`, опрос, результат, lease с пульсом).
- C#/.NET: `examples/csharp/LlmHubExample` (`HttpClient`: chat, задача, lease, разбор `error`; `Timeout` 10 мин). На машине разработки llmhub нет `dotnet` — тест сборки пропускается; **gameDev**: `cd examples/csharp/LlmHubExample && LLMHUB_URL=… LLMHUB_KEY=… dotnet run`. Пути и заголовки `.cs` сверяются с OpenAPI всегда.
