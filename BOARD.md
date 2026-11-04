# BOARD — llmhub

> TL;DR: Только активные, todo и blocked задачи, строка ≤ 120 символов. Детали — в файле задачи. Выполненные → `tasks/done/` + строка в `tasks/done/INDEX.md`.

| ID | Задача | Тип | Статус | Зависит |
|---|---|---|---|---|
| T-369 | 3b: API (OpenAI+tasks+leases+admin), router, адаптер llama-server | impl | in-progress | T-368 |
| T-371 | 3d: деплой systemd на 65.109.95.35, e2e на Mock, integration.md | impl | todo | T-369, T-370 |
| T-372 | Этап 4: spike dstack + тест RunPod ≤ $5 | test | blocked | T-371 |
| T-373 | MCP-сервер llmhub_ask для агентов (тонкий клиент /v1/tasks) | impl | todo | T-369 |

## Входящие
<!-- "YYYY-MM-DD от <источник>: <что> (T-XXX)". -->
- 2026-11-04 от reviewer T-368: `integrity._try_open` считает «database is locked» порчей → карантин; отличать при старте (T-371).
- 2026-11-04 от ворот T-369: nginx перед API — `proxy_read_timeout` > 480 с и `proxy_buffering off` для SSE (T-371).
- 2026-11-04 от T-370: развернуть сервер весов по `deploy/README.md` — nginx-юнит, `llmhub-weights-auth` (`llmhub-wauth`, группа `llmhub`), сертификат, `LLMHUB_INSTANCE_SECRET`/`LLMHUB_WEIGHTS_SECRET`, суммы `sha256-T365.txt`, группа `llmhub` у `weights-acct.log`, опрос `/metrics`; tc/QoS — вопрос (T-371).
- 2026-11-04 от T-370: composition root — `Controller(materials=make_material_fns(...))`, `make_cloud_inference(...)` для реальных провайдеров, Mock — `MockInference`; потребитель `on_timings` (EWMA pp/tg, доля MTP) (T-371).
- 2026-11-04 от reviewer T-370 (R3-2/R3-3): admin-ручка/CLI `clear-weights-hold <pool>` (сейчас только `Controller.clear_weights_hold`) + runbook к инциденту `weights_config` critical; нижняя граница `retry.host_blacklist_s` (T-371).
- 2026-11-04 от reviewer T-370 (R3-1, R2-6): на реальных замерах проверить HF 429 (`rate_limited` → чёрный список хоста, ≤ 3 запуска на волну) и пессимизм `_eta` на старте (T-372).
