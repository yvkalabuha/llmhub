# Интеграция клиентов с llmhub

> TL;DR: Рабочее руководство (T-371) для клиентов gameDev (C#/.NET), VoiceBot (T-366) и агентов. Сервер — `http://10.8.0.1:8095`, только через OpenVPN 10.8.0.0/24. Есть OpenAI-совместимый `/v1/chat/completions` (+ SSE), а `/v1/tasks` и `/v1/leases` — по raw HTTP. Описаны коды ошибок (в том числе `429 rate_limited`), таймауты и `data_class`. Для агентов Claude Code есть MCP-сервер `llmhub` (§8). Нормативный контракт — `docs/openapi.yaml`; на сервере `/docs` выключен. Примеры лежат в `examples/`.

## 1. Подключение
- Базовый URL `http://10.8.0.1:8095`. Доступ только из OpenVPN 10.8.0.0/24: правило ufw на `tun0`, служба слушает только 10.8.0.1. OpenAI-клиентам: `OPENAI_BASE_URL=http://10.8.0.1:8095/v1`.
- Контракт — `docs/openapi.yaml` из репозитория. На сервере `/docs`, `/redoc` и `/openapi.json` выключены (`api.docs: false`, флаг `serve --no-docs`) и отвечают `404`.
- Ключи `voicebot`, `gamedev` и `agents` выдаёт оператор (runbook, «Ротация ключей»). Каждый ключ передаётся файлом `0600`, его нельзя класть в переменную окружения, в git или в лог.
- Ключ клиента: `Authorization: Bearer lh_...`. Выдаёт администратор (`python -m llmhub keys new --name <client>` печатает ключ один раз и его sha256; в конфиге и БД — только хеш). Новый клиент подключается без перезапуска (`SIGHUP` или `POST /admin/reload_clients`); перечитывание применяет и `api.admin_key_sha256` (ротация admin-ключа). Клиент, которого удалили или у которого сняли `cloud_allowed`, сразу теряет облако — и при перечитывании, и при перезапуске службы с исправленным конфигом: его облачные задачи в очереди отменяются (`cancelled`, в `error.reason` — `cloud_not_allowed`), явные и prewarm lease закрываются (`revoked`), продление lease и новая облачная задача → `403 cloud_not_allowed` (в том числе запрос, который был в обработке в момент перечитывания). Уже выполняющаяся облачная задача дорабатывает (GPU оплачен); если её прервёт перезапуск или сбой инстанса, в облачную очередь она не вернётся, а будет отменена с тем же `cloud_not_allowed`.
- У клиента в конфиге: `allowed_models` (псевдонимы или `*`), `max_priority`, `default_priority`, `default_policy`, `cloud_allowed` (по умолчанию `false`), `max_active_jobs` (по умолчанию 32).
- **Лимит** (SEC-L4): у клиента не больше `max_active_jobs` задач в очереди и в работе вместе. Следующий запрос (`/v1/chat/completions` или `/v1/tasks`) получает `429 rate_limited` с заголовком `Retry-After` (`retryable=true`, `type=rate_limit_error`). Повтор с существующим `Idempotency-Key` под лимит не попадает. openai-python сам повторяет `429` с паузой из `Retry-After`.
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
- Таймаут HTTP-клиента должен быть **больше 480 с**: openai-python — `timeout=600`, C# — `HttpClient.Timeout = 10 мин`, curl — `--max-time 600`.
- **Прокси перед API.** По умолчанию прокси нет: клиенты ходят напрямую на `10.8.0.1:8095`. Если прокси появится (nginx и т. п.), ему нужны `proxy_read_timeout 600s` (больше 480 с), `proxy_buffering off` (SSE и `: keep-alive` должны доходить сразу) и `proxy_request_buffering off`. Заголовки `Authorization`, `Idempotency-Key` и `X-LLMHub-*` прокси передаёт без изменений.
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
| `rate_limited` | 429 | да | у клиента уже `max_active_jobs` активных задач (`Retry-After`) |
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

## 7a. Клиенты: gameDev (C#/.NET) и VoiceBot (T-366)
**gameDev (C#/.NET)**
- Рабочий образец — `examples/csharp/LlmHubExample`: chat, задача, lease, разбор `error`.
- Нужен один `HttpClient` с `BaseAddress = http://10.8.0.1:8095/` и `Timeout = TimeSpan.FromMinutes(10)` (больше синхронного окна 480 с), а также `DefaultRequestHeaders.Authorization = Bearer <ключ gamedev>`. Ключ читается из файла 0600.
- Длинную генерацию лучше запускать через `/v1/tasks` и опрашивать `GET /v1/tasks/{id}`, а не держать соединение 8 минут.
- Повтор делается только при `error.retryable == true`: паузу брать из `Retry-After` / `retry_after_s`, `Idempotency-Key` оставлять тем же. Это относится и к `429 rate_limited`.
- У клиента `gamedev` в серверном конфиге `cloud_allowed: false`: все запросы идут только на локальные модели. Облако включает заказчик, правкой конфига.

**VoiceBot (T-366)**
- Подходит openai-python (`OPENAI_BASE_URL=http://10.8.0.1:8095/v1`, ключ `voicebot`, `timeout=600`).
- Для коротких реплик: `X-LLMHub-Priority: urgent` (у `voicebot` `max_priority: urgent`) и `X-LLMHub-Wait` меньше 480 с. На ответ `503 queue_wait_exceeded` задача не теряется: её можно забрать позже по `task_id`.
- Данные голоса — `private` (умолчание клиента): они не уходят в облако ни при каком маршруте.
- Если не хватает местной мощности, приходит `503 local_unavailable` (`retryable`). Бот должен показать деградацию, а не повторять запрос в цикле.

## 8. MCP для агентов (`llmhub.mcp_server`, T-373)
Тонкий stdio MCP-сервер: агент Claude Code вызывает llmhub инструментом, не читая большие входы и не выводя большие ответы. Ходит только в `/v1/tasks*` и `/v1/models`; проверки ответа выполняет сам (Python), решения «годится/нет» модель не принимает.

### Инструменты
| Инструмент | Что |
|---|---|
| `llmhub_ask` | `kind`, `input`, `input_files[]`, `schema` (объект или абсолютный путь к `.json`), `checks[]`, `model` (умолчание `auto`), `priority`, `policy`, `max_tokens`, `data_class` (умолчание `private`), `wait_s`, `deadline_s`, `output_file`, `overwrite=false`, `fresh=false` |
| `llmhub_result` | `task_id`, `kind`, `schema`, `checks[]`, `output_file`, `overwrite`, `wait_s=0` — результат задачи (те же проверки) |
| `llmhub_cancel` | `task_id` (облачная задача в работе всё равно оплачена) |
| `llmhub_models` | псевдонимы (`local`/`cloud`/`ctx_tokens`) и виды задач |

Ответ (`structuredContent`): `verdict` (`ok` / `check_failed` / `pending` / `error`), `task_id`, `status`, `eta_s`, `text` (весь текст или начало), `output_file`, `truncated_inline`, `truncated` (`finish_reason=length`), `finish_reason`, `checks[]` (`name`, `ok`, `reason` — только числа и имена, без текста ответа), `backend`, `fallback`, `warnings`, `tokens`, `error{code,reason,retryable,retry_after_s}`, `hint`. Любая ошибка — это `verdict=error` (не ошибка инструмента MCP), локальные отказы (`outside_roots`, `dot_component`, `exists`, `input_too_large`, `invalid_check`, `cloud_not_enabled`, `config_missing`, `key_unavailable`, …) — до отправки задачи.

- **Виды** (`kind`): `extract_json` (нужна `schema`), `classify`, а также `summarize`, `translate`, `review`, `general` — помечены «пилот: не для рабочих задач без согласования». Свои виды и переопределения — YAML-файлом `LLMHUB_MCP_KINDS` (поля `description`, `system`, `max_tokens`, `think`, `task_type`, `checks`, `requires_schema`).
- **Проверки**: `not-truncated` всегда; `json-schema` — неявно при `schema` (допускается обёртка ```` ```json ````); `max-bytes:N`, `sections:A,B` (строки-заголовки по порядку), `item-count:N` (массив / объект с одним массивом / непустые строки), `script:<latin|cyrillic|han>:<доля>`, `regex:<re>`, `no-regex:<re>` (шаблон ≤ 300 символов; текст длиннее 200 000 символов — провал без поиска). Проверки с регулярными выражениями (`regex`, `no-regex`, `json-schema` с `pattern`/`patternProperties`) выполняются в отдельном одноразовом процессе (`python -I -m llmhub.mcp_server.sandbox`, без переменных `LLMHUB_*`), который убивается по таймауту, — катастрофический шаблон не замораживает сервер. Все проверки одного вызова делят бюджет `LLMHUB_MCP_CHECK_TIMEOUT_S` (5 с); не уложилась — `reason=timeout`. В `schema` допускаются только локальные `$ref` (`#`, `#/…`, `#якорь`), которые разрешаются внутри самой схемы; внешние ссылки, вложенные `$id` и неразрешимые ссылки — `invalid_check` до отправки. Причины провалов — только числа, имена и путь в JSON (сегмент ≤ 40, путь ≤ 200 символов). Автоповторов при провале нет.
- **Запись результата**: `ok` + `output_file` → текст в файл, в ответе начало текста; без `output_file` текст больше `LLMHUB_MCP_MAX_INLINE_BYTES` → `<scratch>/<task_id>.txt`, `truncated_inline=true`. `check_failed` → текст только в `<scratch>/<task_id>.rejected.txt` (для разбора), в `output_file` не пишется. Если `output_file` не удалось записать уже после ответа (файл появился за время ожидания → `exists`, и т. п.), — `verdict=error` с кодом, `task_id`, проверками и путём `<scratch>/<task_id>.txt` в `output_file`: оплаченный ответ не теряется. После отправки задачи любая ошибка (в т. ч. `internal_error`) сохраняет `task_id`.
- **Идемпотентность**: `Idempotency-Key = mcp-` + sha256 тела (с **содержимым** файлов, не путями) + `data_class`/`priority`/`policy`. Повтор того же вызова присоединяется к задаче (так `task_id` восстанавливается после обрыва вызова); изменённый файл по тому же пути — новая задача; `fresh=true` — новый запуск (случайный nonce: повтор вызова с `fresh=true` **не** присоединяется к прошлой задаче, её `task_id` — только из прошлого ответа). После `retryable`-провала тот же ключ запускает новую задачу, после неповторяемого возвращает ту же ошибку (§4).

### Регистрация (S12 T-371; копию `.mcp.json` координатор сохраняет в `.bak-<ts>`)
```json
{
  "mcpServers": {
    "llmhub": {
      "type": "stdio",
      "command": "/home/user/.local/bin/uv",
      "args": ["--directory", "/var/www/llmhub", "run", "--quiet", "--extra", "mcp", "python", "-m", "llmhub.mcp_server"],
      "timeout": 360000,
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
gameDev — то же с `"LLMHUB_MCP_ROOTS": "/var/www/gameDev:/tmp/claude-1000"`. После установки пакета доступна и команда `llmhub-mcp` (то же, что `python -m llmhub.mcp_server`).
- **Ключ — только файлом** (`LLMHUB_KEY_FILE`: обычный файл, не симлинк, владелец — текущий пользователь, права `600`; первая непустая строка). Переменную с самим ключом в `env` файла `.mcp.json` не писать никогда: такие файлы попадают в git. Ключ нигде не выводится (ни в ответах, ни в stderr).
- На сервере llmhub для агентов заведён отдельный клиент `agents`: `cloud_allowed=false`, `max_priority=normal`, `default_priority: low`. Ключ создаётся на шаге S4 (`keys new --out`) и попадает в `/home/user/.config/llmhub/agents.key` (0600, на экран не выводится). `LLMHUB_MCP_ALLOW_CLOUD` не ставится.
- `data_class`: всегда шлётся заголовок `X-LLMHub-Data-Class: private`; `cloud_allowed` — только параметром вызова **и** при `LLMHUB_MCP_ALLOW_CLOUD=1` в `env` MCP-сервера (без решения заказчика не ставить); сервер llmhub всё равно сужает по клиенту.
- Пути — только абсолютные (cwd MCP-сервера — `/var/www/llmhub`, не проект агента), только внутри `LLMHUB_MCP_ROOTS` (+ scratch); путь нормализуется, `realpath` проверяется повторно (симлинки наружу запрещены), `..` запрещено, любой файл или каталог с точкой ниже корня (`.env`, `.git`, `.ssh`, `.claude`, …) недоступен; входы — обычные UTF-8 файлы; запись атомарная (`0600`), существующий файл — только с `overwrite=true`. Корень `/` и корни на `/proc`, `/sys`, `/dev` пропускаются (warning в stderr), пути в этих ФС отклоняются всегда (`/proc/self/environ` содержит окружение процесса). Scratch-каталог — свой (владелец — текущий пользователь, иначе `scratch_unavailable`), права приводятся к `0700`; при старте из него удаляются свои временные `.llmhub-mcp-*.tmp` старше часа (остаются после SIGKILL во время записи). Известное ограничение: жёсткая ссылка внутри корня на свой файл снаружи читается (`realpath` её не видит; создать её можно только на файл, который агент и так читает).
- Разрешения инструментов (`mcp__llmhub__llmhub_ask` и др. в `settings.json`) — на усмотрение владельца проекта.

### Переменные окружения
| Переменная | Умолчание | Что |
|---|---|---|
| `LLMHUB_URL` | — (обязательна) | базовый URL API |
| `LLMHUB_KEY_FILE` | `~/.config/llmhub/key` | файл клиентского ключа (`600`) |
| `LLMHUB_MCP_ROOTS` | — | разрешённые корни через `:` (несуществующие пропускаются) |
| `LLMHUB_MCP_SCRATCH` | `~/.local/state/llmhub-mcp/out` | каталог больших и отклонённых ответов (`0700`, неявно корень) |
| `LLMHUB_MCP_KINDS` | — | YAML со своими видами (обычный файл, владелец — текущий пользователь, без записи для группы и других: после создания `chmod 644` — при `umask 002` файл получается `664` и отклоняется) |
| `LLMHUB_MCP_ALLOW_CLOUD` | — | `1` — разрешить `data_class=cloud_allowed` |
| `LLMHUB_MCP_MAX_INPUT_BYTES` | `400000` | сумма `input` + файлов |
| `LLMHUB_MCP_MAX_INLINE_BYTES` | `16000` | больше — текст в файл |
| `LLMHUB_MCP_PREVIEW_BYTES` | `2000` | начало текста при записи в файл |
| `LLMHUB_MCP_DEFAULT_WAIT_S` / `LLMHUB_MCP_MAX_WAIT_S` | `90` / `300` | ожидание результата в одном вызове |
| `LLMHUB_MCP_HTTP_TIMEOUT_S` | `30` | предел одного HTTP-запроса (внутри вызова — не дольше остатка срока) |
| `LLMHUB_MCP_CHECK_TIMEOUT_S` | `5` | общий бюджет проверок одного вызова |
| `LLMHUB_MCP_MODEL` | `auto` | псевдоним по умолчанию |

### Таймауты и `pending`
- Вызов ждёт до `wait_s` (опрос `GET /v1/tasks/{id}` с паузой `eta_s/4` в пределах 0,5–5 с), затем `verdict=pending` + `task_id`; задача на сервере живёт дальше и не отменяется ни при `pending`, ни при обрыве вызова.
- Длительность одного вызова `llmhub_ask`/`llmhub_result` ограничена общим сроком: отправка — до `max(wait_s, 5)` с, опрос — до `wait_s` (+ 5 с на опрос, начатый до срока; таймаут каждого HTTP-запроса урезается до остатка, не дождались → `pending`), `GET /result` — до 10 с, проверки — до `LLMHUB_MCP_CHECK_TIMEOUT_S`. Итого вызов ≤ `max(wait_s, 5) + 15 + LLMHUB_MCP_CHECK_TIMEOUT_S` с (с умолчаниями — `max(wait_s, 5) + 20` с). Ответ сервера медленнее срока → `pending` (или `error` `deadline`, `retryable=true`, если задача ещё не создана или не успел `/result`; `task_id` сохраняется).
- Claude Code ограничивает время вызова MCP-инструмента переменной `MCP_TOOL_TIMEOUT` (мс, в окружении процесса Claude Code). Правило: `max(wait_s, 5)` + 20 с + запас < `MCP_TOOL_TIMEOUT`/1000. **Рекомендация**: `MCP_TOOL_TIMEOUT=360000` (6 мин) при `LLMHUB_MCP_MAX_WAIT_S=300`. Умолчание `MCP_TOOL_TIMEOUT` в текущей версии Claude Code здесь не зафиксировано (по документации не сверено) — не полагаться на него, задавать явно. Наблюдение (не из документации): в схеме stdio-конфига локальной сборки Claude Code 2.1.318 есть поле сервера `"timeout"` (мс; перекрывает `MCP_TOOL_TIMEOUT` для этого сервера, жёсткий предел на вызов, progress-уведомления его не продлевают, значения < 1000 игнорируются) — можно указать `"timeout": 360000` рядом с `"env"`, если версия Claude Code его поддерживает.
- Если клиент оборвал вызов раньше, `task_id` можно вернуть повтором того же `llmhub_ask` (тот же ключ идемпотентности → та же задача; кроме вызова с `fresh=true`) или узнать из прошлого `pending`.
- `llmhub_cancel`: `status=cancelled` — отменена; `warnings=["cancel_pending"]` — отмена запрошена, задача ещё работает (облачная останавливается в безопасной точке и оплачивается); `warnings=["already_finished"]` — задача уже завершилась, ничего не отменено. Долгие задачи: `wait_s=0..30` → `pending` → позже `llmhub_result(task_id, wait_s=…)`.
- Размер ответа: `LLMHUB_MCP_MAX_INLINE_BYTES` держать ниже лимита вывода MCP Claude Code (`MAX_MCP_OUTPUT_TOKENS`); 16 000 байт ≈ 4–8 тыс. токенов.

### Что делегировать (до нового пилота на llmhub)
- **Можно** (с `schema`/`checks`, результат обязательно просматривает Claude): извлечение структуры, сверяемой с источником (`extract_json` + `schema` + `item-count`); классификация по явному полю (`classify` + `schema` с `enum`). Выигрыш — на входах от ~8 тыс. токенов; ожидание — минуты.
- **Нельзя**: заметки к релизу и changelog, переводы, свободная классификация, числовые агрегаты, ревью кода как вердикт, всё с секретами и ключами во входе, всё, где ошибку не поймает проверка и не увидит Claude.
- **Эскалация**: `verdict ≠ ok` → подзадачу делает Claude целиком; ответ не «чинится», `fresh=true` ради другого ответа не повторяется (кроме `error.retryable=true` после `retry_after_s`). `ok` → Claude просматривает; не годится — делает сам.
- Локальная LLM одна (CPU, ~9 ток/с): параллельные агенты стоят в общей очереди; `priority=urgent` не использовать.
