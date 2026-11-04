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

## Ревью (цикл 1: безопасность)
> TL;DR: 1 major (ReDoS-страж не работает — `re` держит GIL, сервер замерзает), 2 minor (права существующего scratch; корень `/` открывает `/proc/self/environ` с `LLMHUB_KEY`), 4 low. Пути (traversal, симлинки, TOCTOU, hard link при записи), ключ, двойной допуск облака, Idempotency-Key, rejected-текст, эхо ошибок — в порядке. Вердикт: **доработка** (SEC-1).

Коммит cb8d314, ревьюер безопасности; `src/` не менялся. Тесты: `tests/test_review_t373_sec_paths.py` (21), `tests/test_review_t373_sec_service.py` (25); подтверждённые дефекты — `xfail(strict=True)`.

Находки:
- **SEC-1 major** — `checks.py:231-242` `run_checks_async`: `asyncio.to_thread` + `wait_for(5 s)` не ограничивает катастрофический regex: движок `re` не отпускает GIL, цикл событий стоит до конца поиска, поток не убить. Замер: `regex:^(a+)+$` на `"a"*28+"b"` с `timeout_s=0.5` → ответ через 9.7 с; при длине 40+ stdio-сервер висит бесконечно (все инструменты, ping). Тот же путь — `pattern`/`patternProperties` в `schema` (jsonschema → `re`). Текст — ответ модели, т. е. управляем промпт-инъекцией во входном файле. Тест `test_regex_timeout_bounds_wall_time` (xfail). Патч: регэксп-проверки (и `json-schema` при наличии `pattern`/`patternProperties`/`format: regex`) выполнять в отдельном процессе с жёстким kill по таймауту, напр. `multiprocessing.get_context("spawn")`/`ProcessPoolExecutor(max_workers=1)` с пересозданием пула при таймауте; дешёвый минимум дополнительно — лимит длины текста для regex (напр. 200 000 байт) и отказ `CheckSpecError` для вложенных квантификаторов `(\w+)+`/`(a|a)*` (эвристика, не замена).
- **SEC-2 minor** — `paths.py:217-221` `_make_private_dir`: если каталог уже есть, `return` без проверки — режим 0777/чужой владелец сохраняются (`LLMHUB_MCP_SCRATCH` в общем каталоге: другой пользователь подменяет/удаляет `*.txt` после записи, агент читает чужой текст; симлинк-ловушка → отказ `symlink`, DoS). Тест `test_existing_scratch_dir_is_made_private` (xfail). Патч:
  ```python
  def _make_private_dir(path: Path) -> None:
      path.mkdir(parents=True, mode=0o700, exist_ok=True)
      st = os.lstat(path)
      if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
          raise OSError(errno.EPERM, "scratch not private")
      if stat.S_IMODE(st.st_mode) & 0o077:
          os.chmod(path, 0o700)
  ```
- **SEC-3 minor** — `paths.py:89-109`/`settings.py:332-342`: псевдо-ФС не запрещены; при `LLMHUB_MCP_ROOTS=/` (или корне, содержащем `/proc`) `input_files=["/proc/self/environ"]` читается (S_ISREG, размер 0) и уходит в llmhub вместе с `LLMHUB_KEY`. Тест `test_proc_environ_refused_even_with_root_slash` (xfail). Патч: в `_check_real` отказ `outside_roots` для realpath под `/proc`, `/sys`, `/dev`; в `_roots` пропускать (с warning) корень `/` и корни под этими ФС.
- low — `paths.py:98-109`: hard link внутри корня на файл снаружи (в т. ч. `~/.ssh/*`) читается — realpath его не видит. Создать ссылку можно только на свой файл (protected_hardlinks), который агент и так читает; при желании — отказ при `st_nlink > 1` в `read_text`. Зафиксировано тестом `test_hard_link_to_outside_file_is_readable_documented`. Запись через hard link безопасна (`os.replace` меняет запись каталога) — тест.
- low — `checks.py:95`: причина `json-schema` — `absolute_path` из данных ответа модели (ключи `patternProperties`/`additionalProperties`-объектов), т. е. фрагмент проверяемого текста неограниченной длины, вопреки docstring `checks.py:3-4`. Патч: обрезать каждый сегмент до 40 символов и путь до 200, либо выводить только индексы/ключи из `schema.properties`.
- low — `kinds.py:420-429`: `LLMHUB_MCP_KINDS` читается без проверки владельца/прав; чужой файл с правом записи меняет `system` и описания инструментов (инъекция в промпт агента). Патч: та же проверка `st_uid == getuid()` и `mode & 0o022 == 0`, что у ключа.
- low — `paths.py:170-204`: после SIGKILL между `open(tmp)` и `unlink` в каталоге пользователя остаётся `.llmhub-mcp-*.tmp` (0600, полный ответ). Допустимо; можно отметить в docs.
- info — Idempotency-Key (`request.py:55-68`) не включает `deadline_s`; сервер его тоже не хеширует (`submission.py` meta = data_class/policy/priority) — согласовано, коллизии опций нет (тесты на data_class/priority/policy/nonce/schema/model). Путь в ключ и тело не попадает (только basename) — тест.

Проверено и в порядке (с тестами): `..`, точки, симлинки наружу (файл, каталог, `/proc`, `/dev/zero`), подмена промежуточного каталога и конечного компонента между `resolve_*` и `read_text`/`write_text` (повторная проверка `/proc/self/fd`), симлинк, подсаженный после `resolve_output` (с overwrite → `symlink`, без → `exists`, наружу ничего не пишется), 0600 при `umask 0`, FIFO как ключ (не блокирует), 0640/0644 ключа → `bad_mode`, ошибки ключа без пути/содержимого; ключ не попадает в ответы ok/check_failed/error/models и логи (caplog DEBUG); облако — только `data_class="cloud_allowed"` и `LLMHUB_MCP_ALLOW_CLOUD` ровно `"1"` (`true`/` 1`/`01` — нет), kinds.yaml не может задать `data_class`/`llmhub`, в теле `llmhub` только `task_type`, сервер сужает класс (`options.py:107-116`) и сам запрещает облако для private (`submission.py:181`); по умолчанию `private` и при включённом облаке; rejected-текст не пишется в `output_file` даже с `overwrite=true` (существующий файл цел), pending/error ничего не пишут; ошибки не эхо-ят вход (спецификации проверок, пути, kind, schema-путь). Описания инструментов содержат правило делегирования (`server.py:24-52`).

Команды и результаты:
- `uv run ruff check src tests scripts` → All checks passed!
- `uv run pyright` → 0 errors, 0 warnings
- `uv run pytest -q -p no:cacheprovider tests/test_m_*.py tests/test_review_t373_sec_*.py` → 287 passed, 3 xfailed (23.8 s; с slow `test_m_live.py`)
- `uv run pytest -q -p no:cacheprovider -m "not slow and not docker"` → 2806 passed, 110 deselected, 7 xfailed (107 s)

Вердикт (безопасность): **доработка** — закрыть SEC-1 (major); SEC-2, SEC-3 — желательно в том же раунде (патчи выше, снять xfail). Low — на усмотрение. Статус не менял.

## Ревью (цикл 2: корректность)
> TL;DR: 2 major (после submit теряется `task_id` при любой не-HTTP ошибке — гонка `exists` при записи, исключение проверки; неразрешимая `$ref` в схеме обнаруживается только после работы LLM → `internal_error`), 2 minor (глубоко вложенный JSON-ответ → `internal_error`; длительность вызова не ограничена `wait_s + 30 с`, как обещает §8), 3 low. Протокол MCP, контракт API (Idempotency-Key, `x-should-retry`, `Retry-After`, 409/503/504, повторное присоединение), семантика `result`/`cancel`, kinds.yaml, флаги усечения, регистрация из §8 — в порядке. Вердикт: **доработка** (C2-F1, C2-F2).

Коммит cb8d314, ревьюер корректности; `src/` не менялся. Тесты: `tests/test_review_t373_fn_correctness.py` (7: 3 passed, 4 `xfail(strict=True)` — при исправлении снять маркер).

Находки:
- **C2-F1 major** `src/llmhub/mcp_server/service.py:249-251` (и `:273-275` в `_result`): после `submit` ловится только `HubError`; `PathDenied` из `_finish` (`write_text` → `exists`, `scratch_unavailable`), `OSError` (диск), исключения проверок уходят в `_guard` (`:98-114`) без `task_id` → оплаченный ответ недостижим (без id нет `llmhub_result`; повтор `ask` с тем же `output_file`, который теперь существует, отказывает `exists` до submit). Тест `test_output_race_after_submit_keeps_task_id`. Патч (в `_ask`; в `_result` так же с `task_id`):
  ```python
  except HubError as exc:
      exc.task_id = exc.task_id or sub.id
      raise
  except PathDenied as exc:
      return _error(exc.code, task_id=sub.id)
  except Exception as exc:  # noqa: BLE001
      log.error("llmhub-mcp: internal error %s, task %s", type(exc).__name__, sub.id)
      return _error("internal_error", task_id=sub.id)
  ```
  Желательно при `exists` на записи `output_file` ещё сбросить текст в scratch `<task_id>.txt` и вернуть путь.
- **C2-F2 major** `src/llmhub/mcp_server/checks.py:81-96`: `check_schema` пропускает неразрешимую `$ref` (`{"$ref": "#/$defs/missing"}`, удалённые URL); `iter_errors` бросает `_WrappedReferencingError` (`referencing.exceptions.Unresolvable`) только в `run`, после ответа LLM → `internal_error` без `task_id` (C2-F1). Тест `test_unresolvable_ref_schema_refused_before_submit`. Патч: в `_json_schema` до submit обойти дерево схемы, отклонить `$ref`, не начинающуюся с `#`, локальные разрешить JSON-pointer по самой схеме, иначе `CheckSpecError` (→ `invalid_check` до сети); в `run` — страховка `except Exception: return CheckResult("json-schema", False, "schema_error")`.
- **C2-F3 minor** `src/llmhub/mcp_server/checks.py:51-55` (`_load_json`; им же пользуется `_count_items` `:115`): `json.loads` глубоко вложенного ответа (`"["*100000…`) бросает `RecursionError`, не `ValueError` → `internal_error` вместо `check_failed`. Тест `test_deeply_nested_answer_is_check_failed_with_task_id`. Патч: `except (ValueError, RecursionError): return False, None`.
- **C2-F4 minor** `src/llmhub/mcp_server/client.py:147-170`, `service.py:242-248`: бюджет повторов ограничивает только паузы, но не длительность HTTP-запросов (до `http_timeout_s=30` каждый); после последнего опроса у дедлайна ещё `result` (бюджет 5 с + до 30 с) и проверки (до 5 с каждая, последовательно). Вызов `wait_s=90` при ответах сервера по 25 с длится 130 с > `wait_s + 30` (правило `docs/integration.md:168`). Тест `test_call_duration_bounded_by_wait_plus_margin`. Патч: общий дедлайн вызова `start + wait_s` — timeout каждого запроса `min(http_timeout_s, остаток + запас)`, не начинать новый опрос, если остаток меньше запаса на `result`; либо честное правило в §8: `wait_s + 2·LLMHUB_MCP_HTTP_TIMEOUT_S + 5 с·N_checks < MCP_TOOL_TIMEOUT/1000`.
- C2-F5 low `service.py:277-282` `_cancel`: облачная задача в работе → `verdict=ok`, `status=running`, а `cancel_requested` из `CancelResult` не передаётся; для уже завершённой — `ok` с `completed`; агенту неясно, отменилось ли. Предложение: `hint`/`warnings` по статусу.
- C2-F6 low `service.py:137-159` `models()`: нет общего `except Exception`; нестандартное поле `llmhub` (не объект) → `AttributeError` → `isError` MCP (сервер живёт). OpenAPI так не отвечает — только робастность.
- C2-F7 low `docs/integration.md:169`: «`task_id` можно вернуть повтором того же `llmhub_ask`» не работает для вызова с `fresh=true` (случайный nonce) — дописать.

Проверено и в порядке: схемы инструментов (`data_class` enum/умолчание, `ge/gt`), ошибки → `verdict=error` с `isError=false`; неверные типы аргументов → `isError=true`, сервер продолжает обслуживать (тест `test_server_survives_bad_args_and_keeps_serving`: 6 плохих вызовов, 5 «мягких» отказов с кодами, затем `ok`); `x-should-retry` главнее тела, ≤ 2 повтора, `Retry-After`/`retry_after_s` больше бюджета → без повтора, submit повторяется тем же ключом; `task_not_ready` не повторяется; TERMINAL совпадает с ядром (`api_gateway/waiter.py:15`), `running`+`cancel_requested` → опрос дальше; повтор `ask` после `pending` → тот же `task_id`, затем `result` → `ok` (тест `test_retryable_status_error_keeps_task_id_and_rejoins`); `cancel` → `result` = `error cancelled`, `task_id` сохранён (тест); Idempotency-Key включает `data_class/priority/policy`, как каноническое тело сервера (`api_gateway/submission.py:158`), `deadline_s` в нём нет ни там, ни там; `X-LLMHub-Deadline` в формате `:g` сервер разбирает (`options.py:86`); `truncated` = `length` или `X-LLMHub-Truncated`; `check_failed` → только `*.rejected.txt`; kinds.yaml — 6 видов, `task_type` нет, неизвестные поля → `KindsError`. Регистрация из §8 работает как написано: `env -i HOME=… LLMHUB_URL=… LLMHUB_KEY_FILE=… LLMHUB_MCP_ROOTS=… /home/user/.local/bin/uv --directory /var/www/llmhub run --quiet --extra mcp python -m llmhub.mcp_server` по stdio → `initialize` OK, `tools/list` = 4 инструмента, `llmhub_models` без ключа → `structuredContent.error.code=key_unavailable`, stderr пуст.

Команды и результаты:
- `uv run pytest -q -p no:cacheprovider tests/test_review_t373_fn_correctness.py -rxX` → 3 passed, 4 xfailed; с `--runxfail` все 4 падают по описанной причине (`task_id=None`, `internal_error` ×2, «call took 130 s»).
- `uv run pytest -q -p no:cacheprovider -m "not slow and not docker"` → exit 0 (регрессии после последней правки docstring нет).
- `uv run pytest -q -p no:cacheprovider` (полный, slow + docker) → **2891 passed, 1 skipped, 6 xfailed** (4 этого цикла + 2 цикла безопасности), 327 с.
- `uv run ruff check src tests scripts` → All checks passed; `uv run pyright` → 0 errors.

Критерии приёмки (часть корректности): поля ответа, `wait_s` vs `MCP_TOOL_TIMEOUT` в §8 (с оговоркой C2-F4), лимиты входа/inline, Idempotency-Key по содержимому, живой slow-тест, пример регистрации — выполнены; блокируют C2-F1 и C2-F2.

Вердикт цикла 2: **доработка** — исправить C2-F1, C2-F2 (major), желательно C2-F3, C2-F4 в том же раунде; снять `xfail` в `tests/test_review_t373_fn_correctness.py`. Статус не менял.

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

## Исправления (раунд 1)
> TL;DR: закрыты SEC-1..3, все low цикла безопасности (кроме hard link — задокументирован), C2-F1..F7. Регулярные выражения — в убиваемом одноразовом процессе, общий срок вызова, `task_id` не теряется после submit. Снято 6 `xfail(strict)`; тест C2-F4 переписан (см. отклонения). Статус — review.

| Находка | Исправление | Тест |
|---|---|---|
| SEC-1 major | `sandbox.py`: `regex`/`no-regex` и `json-schema` с `pattern`/`patternProperties` — в одноразовом дочернем процессе `python -I -m llmhub.mcp_server.sandbox` (JSON по stdin/stdout, без env `LLMHUB_*`/`PYTHON*`), `kill` + `wait` по таймауту; в ребёнке `SIGALRM` (сирота умирает сам); текст для regex ≤ 200 000 символов; один бюджет `LLMHUB_MCP_CHECK_TIMEOUT_S` (5 с) на все проверки вызова | `test_regex_timeout_bounds_wall_time` (xfail снят); `test_m_fix_round1.py`: `test_catastrophic_regex_is_killed_and_loop_stays_free` (цикл тикает, дочерний процесс снят), `test_server_stays_responsive_while_catastrophic_regex_runs` (MCP `tools/list`, `llmhub_models`, второй `llmhub_ask` < 1 с, пока идёт regex; `ping` в mcp 2.2.0 удалён), `test_one_budget_for_all_checks`, `test_regex_text_is_capped`, `test_child_dies_on_its_own_alarm`, `test_child_gets_no_llmhub_env`, `test_schema_with_pattern_is_isolated_and_bounded`, `test_schema_without_pattern_runs_inline`, `test_checks_module_has_no_inline_regex_search` |
| SEC-2 minor | `_make_private_dir`: `mkdir` → `lstat` (каталог, владелец = `getuid`, иначе `EPERM` → `scratch_unavailable`) → `chmod 0700`; проверяется `realpath` scratch (симлинк на свой каталог допустим, проверки — к цели) | `test_existing_scratch_dir_is_made_private` (xfail снят); `test_scratch_foreign_owner_refused`, `test_scratch_symlink_uses_checked_target`, `test_new_scratch_is_0700` |
| SEC-3 minor | `paths.py`: `/proc`, `/sys`, `/dev` отклоняются в лексической и в `realpath`-проверке; `Roots.build` пропускает (warning) корень `/` и корни на этих ФС (в т. ч. симлинк на `/proc/self`) | `test_proc_environ_refused_even_with_root_slash` (xfail снят); `test_pseudo_fs_refused_with_root_slash` (5), `test_forbidden_roots_are_skipped` (5), `test_symlink_root_to_proc_skipped` |
| low: причина json-schema | сегмент пути ≤ 40, путь ≤ 200, имя валидатора ≤ 40 | `test_schema_reason_is_length_capped` |
| low: `LLMHUB_MCP_KINDS` | обычный файл, владелец — текущий пользователь, без записи для группы/других (`mode & 0o022`), чтение через `fstat` открытого fd | `test_m_kinds.py`: `test_user_file_writable_by_others_refused` (3), `test_user_file_foreign_owner_refused`, `test_user_file_not_regular_refused` |
| low: `.llmhub-mcp-*.tmp` | при старте удаляются свои (`uid`) временные файлы scratch старше 1 ч (моложе — могут принадлежать другому процессу) | `test_stale_tmp_files_removed_on_start` |
| low: hard link | не менялось (как предложил ревьюер — допустимо), описано в docs §8 | `test_hard_link_to_outside_file_is_readable_documented` |
| C2-F1 major | `service._after_submit` (общий для `ask`/`result`): `HubError` с `task_id`, `PathDenied` → `_error(code, task_id)`, прочее → `internal_error` с `task_id`; отказ записи `output_file` после ответа → текст в `<scratch>/<task_id>.txt`, `verdict=error` с кодом, проверками и путём | `test_output_race_after_submit_keeps_task_id` (xfail снят); `test_exists_on_output_spills_to_scratch`, `test_unexpected_error_after_submit_keeps_task_id`, `test_result_scratch_unavailable_keeps_task_id` |
| C2-F2 major | `_json_schema`: обход схемы до submit — только локальные `$ref`/`$dynamicRef` (`#`, `#/pointer` с `~0`/`~1`/%-декодированием, `#якорь` по `$anchor`/`$dynamicAnchor`), вложенный `$id` — отказ; `schema_outcome` никогда не бросает (`schema_error`) | `test_unresolvable_ref_schema_refused_before_submit` (xfail снят); `test_bad_references_refused` (6), `test_local_references_accepted` (4), `test_schema_check_never_raises` |
| C2-F3 minor | `load_json`: `except (ValueError, RecursionError)`; схема: `RecursionError` → `invalid_check`, файл схемы — `invalid_request` | `test_deeply_nested_answer_is_check_failed_with_task_id` (xfail снят); `test_deep_schema_refused` |
| C2-F4 minor | `HubClient._request(deadline=)`: таймаут попытки = `min(http_timeout, остаток)`, таймаут из-за урезания → `HubError("deadline", retryable)`, паузы повторов — в пределах срока; `wait(deadline=, initial=)`: срезанный опрос → последний статус (`pending`); `service`: submit до `max(wait_s,5)`, опрос до `wait_s + 5`, `/result` 10 с, проверки — общий бюджет 5 с ⇒ вызов ≤ `max(wait_s,5) + 20` с; §8 и описание `llmhub_ask` исправлены | `test_call_duration_bounded_by_wait_plus_margin` (переписан, xfail снят), `test_call_with_fast_requests_still_ok`, `test_wait_zero_with_hung_server_is_bounded`, `test_request_timeout_is_clamped_to_the_deadline`, `test_check_timeout_setting` |
| C2-F5 low | `cancel`: `cancelled` → «Cancelled.»; завершённая → `warnings=["already_finished"]`; в работе → `warnings=["cancel_pending"]` + подсказка (облако оплачено); описано в §8 | `test_cancel_says_what_happened` (3) |
| C2-F6 low | `models()`: `llmhub` не объект → пустые поля, `id` не строка → `null`; общий `except Exception` → `error.code=internal_error` | `test_models_tolerates_odd_meta_and_never_raises` |
| C2-F7 low | §8: повтор с `fresh=true` к прошлой задаче не присоединяется | `test_m_docs.py` |

Файлы: `src/llmhub/mcp_server/{sandbox.py (новый), checks.py, client.py, service.py, paths.py, kinds.py, settings.py, server.py}`; `docs/integration.md` §8; тесты `tests/test_m_fix_round1.py` (новый, 47), `tests/test_m_kinds.py`, `tests/test_review_t373_{sec_paths,sec_service,fn_correctness}.py` (сняты маркеры; в `sec_service` kinds-файлу задан `644`).

Отклонения:
1. `test_call_duration_bounded_by_wait_plus_margin` в исходном виде (`ok` и ≤ 120 с при 25 с на запрос) недостижим ни при каком ограниченном расписании: `completed` виден только опросу, начатому ≥ 64 с, затем `/result` ещё 25 с; к тому же `MockTransport` не соблюдает таймауты httpx. Тест переписан: транспорт соблюдает `read`-таймаут запроса; ожидается `pending` с `task_id` за ≤ `wait_s + 20` с; добавлены быстрый вариант (`ok`) и «висящий сервер» при `wait_s=0`.
2. Новая настройка `LLMHUB_MCP_CHECK_TIMEOUT_S` (бюджет проверок; нужна и для теста отзывчивости).
3. Права kinds-файла: при `umask 002` файл `664` теперь отклоняется (строго по предложению ревьюера, `0o022`); в docs — «например `644`».
4. Проверки без regex по-прежнему в рабочем потоке (`to_thread` + общий срок); в `run_checks` (синхронно, без лимита) — только для доверенного текста (тесты).

Команды и результаты (раунд 1):
- `uv run ruff check src tests scripts` → All checks passed!
- `uv run pyright` → 0 errors, 0 warnings, 0 informations
- `uv run pytest -q -p no:cacheprovider tests/test_m_*.py tests/test_review_t373_*.py` → 353 passed (29 с, с slow `test_m_live.py`), xfail не осталось
- `uv run pytest -q -p no:cacheprovider -m "not slow and not docker"` → 2869 passed, 110 deselected (93 с)
- `uv run pytest -q -p no:cacheprovider` (полный, slow + docker) → 2978 passed, 1 skipped (321 с)
- 2026-11-04: implementer — раунд исправлений 1 выполнен, статус review (ждёт повторного ревью).
