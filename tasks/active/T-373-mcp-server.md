# T-373: MCP-сервер llmhub_ask для агентов

> TL;DR: stdio MCP-сервер `llmhub_ask` — тонкий клиент HTTP API llmhub (`/v1/tasks`), чтобы агенты Claude Code вызывали llmhub как инструмент (запрос заказчика). Файлы читает и пишет только внутри разрешённых корней, по умолчанию `private`, проверки результата на своей стороне. Вынесен из T-369 по воротам 2026-11-04.

- Проект: llmhub
- Тип: impl
- Агент: analyst → implementer → reviewer
- Статус: review
- План: tasks/plans/T-373-plan.md
- Приоритет: P2
- Зависит от: T-369
- Документы: tasks/plans/T-369-plan.md (§4 эндпоинты и коды ошибок, §11 ворота); decisions/ADR-0138-cloud-gpu-manager.md; образец проверок (только для синтаксиса, не импортировать) — /var/www/projectVoiceBot/tools/llm/extllm_checks.py

## Цель
Агенты Claude Code вызывают llmhub через MCP-инструмент, не тратя токены на чтение больших входов и вывод больших результатов. Сервер не импортирует ничего из `llmhub.*` вне `llmhub.mcp_server` и ничего из projectVoiceBot (`tools/llm`).

## Набросок (из плана T-369 до ворот, уточняет analyst)
- `src/llmhub/mcp_server/`: `__main__.py` (stdio), `server.py` (`mcp==2.2.0`, `mcp.server.mcpserver.MCPServer`), `client.py` (httpx, `LLMHUB_URL`, ключ из `LLMHUB_KEY` или `LLMHUB_KEY_FILE` с правами 600), `checks.py`, `kinds.py` + `kinds.yaml`; зависимости `mcp==2.2.0`, `jsonschema` — extra `mcp` и dev-группа.
- Инструменты: `llmhub_ask(kind, input, input_files, schema, checks, model="auto", priority, policy, max_tokens, data_class="private", wait_s, output_file, overwrite=False, fresh=False)`, `llmhub_result(task_id, schema, checks, output_file, overwrite)`, `llmhub_cancel(task_id)`, `llmhub_models()`.
- Поток: `POST /v1/tasks` → опрос `GET /v1/tasks/{id}` до `wait_s` → `GET /result` → проверки; не дождались → `pending` + `task_id`. Автоповторов при провале проверок нет.
- `kind` → системный промпт, `task_type`, `max_tokens`, `think`, проверки по умолчанию; виды общего назначения (`general`, `summarize`, `classify`, `extract_json`, `translate`, `review`); свои — файлом `LLMHUB_MCP_KINDS`.
- Проверки (синтаксис как у `extllm_checks`, код свой): `max-bytes:N`, `not-truncated`, `json-schema` (неявно при `schema`), `sections:A,B`, `item-count:N`, `script:<latin|cyrillic|han>:<ratio>`, `regex:<re>`, `no-regex:<re>`.

## Критерии приёмки
- [ ] Файлы: `input_files` и `output_file` только внутри корней `LLMHUB_MCP_ROOTS` (список через `:`; путь нормализуется `realpath`, симлинки наружу запрещены); каталоги и файлы с точкой в начале имени (`.env`, `.git`, `.ssh`, …) запрещены; запись только внутри корней; существующий файл не перезаписывается без `overwrite=true`. Тесты на каждый отказ.
- [ ] `X-LLMHub-Data-Class: private` по умолчанию; облако — только явным параметром `data_class="cloud_allowed"` (и если клиент это разрешает на сервере).
- [ ] `Idempotency-Key` = sha256 канонического тела запроса **с содержимым файлов** (не путей); параметр `fresh=true` добавляет nonce (новый запуск того же запроса). Тест: тот же путь с другим содержимым → другой ключ.
- [ ] Лимит размера результата в ответе `LLMHUB_MCP_MAX_INLINE_BYTES`: больше → текст пишется в файл в scratch-каталоге (внутри корней), в ответе путь + `truncated_inline: true` и начало текста.
- [ ] Лимит входа `LLMHUB_MCP_MAX_INPUT_BYTES` (по умолчанию 400 000).
- [ ] Документировано соотношение `wait_s` и таймаута MCP-инструмента клиента (`MCP_TOOL_TIMEOUT` Claude Code): `wait_s` должен быть меньше; иначе — `pending` + `llmhub_result`.
- [ ] Ответ: `verdict` (`ok`/`check_failed`/`pending`/`error`), `text` или `output_file`, `checks[]`, `task_id`, `backend`, `fallback`, `truncated`, `tokens`, `error{code,reason,retryable,retry_after_s}`; ключ нигде не выводится.
- [ ] Тест импортов: только `httpx`, `mcp`, `jsonschema`, `yaml`, stdlib и `llmhub.mcp_server.*`.
- [ ] Живой тест (`slow`): MCP-подпроцесс по stdio против живого приложения T-369 (фикстура `tests/i_fixtures.py`): `ok`, `check_failed`, `pending` → `llmhub_result`.
- [ ] Пример регистрации в `docs/integration.md` (не в projectVoiceBot); pytest, ruff, pyright чисто; ревью ≤ 3 раундов.

## Подзадачи
- [x] План
- [x] checks + kinds
- [x] client + безопасность файлов
- [x] server + живой тест + документация

## Ревью
<заполняет reviewer: находки, команды, результаты, вердикт>

## Журнал
- 2026-11-04: задача создана по решению координатора на воротах плана T-369 (MCP вынесен из T-369; находки critic по MCP — в критериях).
- 2026-11-04: analyst — план `tasks/plans/T-373-plan.md`: 16 шагов, потоки 0 → A, B, C (параллельно) → D → E; пакет `llmhub.mcp_server` (settings, secret, paths, checks, kinds, request, client, service, server), тесты in-process на `ApiEnv` + slow stdio на `LiveStack` (`tests/int_live.py`, не `i_fixtures.py`); двойной допуск облака (`data_class` + `LLMHUB_MCP_ALLOW_CLOUD`); правило делегирования — в описаниях инструментов. P2 — ворота не обязательны. Блокирующих вопросов нет. Статус planned.
- 2026-11-04: implementer — потоки 0, A, B, C, D, E выполнены последовательно (TDD), статус review.
  - Файлы: `pyproject.toml` (extra `mcp`, dev `mcp==2.2.0`+`jsonschema>=4.20`, script `llmhub-mcp`), `uv.lock`; пакет `src/llmhub/mcp_server/` (`__init__`, `__main__`, `settings`, `secret`, `paths`, `models`, `checks`, `kinds` + `kinds.yaml`, `request`, `client`, `service`, `server`); тесты `tests/m_fixtures.py`, `tests/test_m_{settings,secret,paths,models,checks,kinds,request,client,service,service_api,server,imports,live,docs}.py`; `docs/integration.md` (TL;DR + новый §8 «MCP для агентов»).
  - Проверки: `uv run pytest tests/test_m_*.py` → 244 passed (с slow `test_m_live.py`, ~16 с); быстрый набор `-m "not slow and not docker"` → 2758 passed + smoke после правки; полный `uv run pytest -q -p no:cacheprovider` (slow + docker) → 2868 passed, 1 skipped, 314 с (до последней правки service: +1 тест, `test_m_*` перепрогнаны — 244 passed); `ruff check src tests scripts` — чисто; `pyright` — 0 errors. kinds.yaml и `llmhub-mcp` есть в wheel (`uv build --wheel --offline` в scratchpad). Документированная команда `uv --directory /var/www/llmhub run --quiet --extra mcp python -m llmhub.mcp_server` запускается (проверено с неверным env → exit 2, stderr).
  - Отклонения от плана:
    1. Q1: `uv sync --offline` не нашёл `python-multipart==0.0.33` → выполнен обычный `uv sync` (бесплатно, PyPI).
    2. `paths`: любой компонент `..` в сыром пути — отказ `dot_component` (строже плана: после `normpath` `root/.git/../a` иначе не ловится); добавлены коды `not_found`, `invalid_path` (NUL), `input_too_large`, `scratch_unavailable`. Запись без `overwrite` — `os.link` временного файла (атомарный отказ `exists`, если файл появился), с `overwrite` — `os.replace`; оба через `dir_fd`, повторная проверка `realpath` каталога и входного файла через `/proc/self/fd`.
    3. `HubClient.wait` возвращает последний статус всегда (а не `None` по сроку) — вызывающему нужен `eta_s` для `pending`; добавлены `budget_s` у методов и свойство `monotonic`.
    4. `Check` — dataclass (`name`, `fn`), а не голый Callable: имя нужно для таймаута; `run_checks_async` — каждая проверка в `asyncio.to_thread` с таймаутом 5 с (R5).
    5. `service.ask`: схема (в т. ч. путь к файлу) разбирается до проверок — `build_checks` нужна схема; пустой вход → `invalid_request`; при `output_file` в ответе только начало текста (`preview_bytes`), полный текст — в файле; ошибка HTTP после submit сохраняет `task_id`.
    6. `server.py` без `from __future__ import annotations` (MCPServer вычисляет аннотации с замыканиями); `build_server(..., monotonic=)` — для тестов на FakeClock; lifespan не используется.
    7. `HubClient` ключ кешируется после первой успешной загрузки (`_key_loader`).
  - Q2 (`MCP_TOOL_TIMEOUT`): по документации не сверено (сети к докам нет). В §8 — как рекомендация `360000`; наблюдение из бинаря локального Claude Code 2.1.318: в схеме stdio-конфига есть поле сервера `timeout` (мс, перекрывает `MCP_TOOL_TIMEOUT`, < 1000 игнорируется) — описано как наблюдение, не факт из документации. Умолчание не найдено.
  - Известные ограничения: опрос с паузой до 5 с — короткая задача отвечает через ~5 с после готовности; текстовый `content` ответа MCP дублирует `structuredContent` (JSON с отступами, ~+20 % к размеру); поток с зависшим regex продолжает работать после таймаута (R5); `.mcp.json` нигде не правился.
