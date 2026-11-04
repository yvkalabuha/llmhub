# T-373: MCP-сервер llmhub_ask для агентов

> TL;DR: stdio MCP-сервер `llmhub_ask` — тонкий клиент HTTP API llmhub (`/v1/tasks`), чтобы агенты Claude Code вызывали llmhub как инструмент (запрос заказчика). Файлы читает и пишет только внутри разрешённых корней, по умолчанию `private`, проверки результата на своей стороне. Вынесен из T-369 по воротам 2026-11-04.

- Проект: llmhub
- Тип: impl
- Агент: analyst → implementer → reviewer
- Статус: todo
- План: tasks/plans/T-373-plan.md (создаёт analyst)
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
- [ ] План
- [ ] checks + kinds
- [ ] client + безопасность файлов
- [ ] server + живой тест + документация

## Ревью
<заполняет reviewer: находки, команды, результаты, вердикт>

## Журнал
- 2026-11-04: задача создана по решению координатора на воротах плана T-369 (MCP вынесен из T-369; находки critic по MCP — в критериях).
