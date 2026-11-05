# BOARD — llmhub

> TL;DR: Только активные, todo и blocked задачи, строка ≤ 120 символов. Детали — в файле задачи. Выполненные → `tasks/done/` + строка в `tasks/done/INDEX.md`.

| ID | Задача | Тип | Статус | Зависит |
|---|---|---|---|---|
| T-372 | Этап 4: RunpodAdapter + тест RunPod ≤ $5 (код — review, исправления раунда 2 + L2b (CUDA по одной, 500 no instances = no_capacity), dist 0.3.2 пересобран; живые P0, L0–L8 — координатор) | test | review | T-371 |

## Входящие
<!-- "YYYY-MM-DD от <источник>: <что> (T-XXX)". -->
- 2026-11-04 от reviewer T-368: `integrity._try_open` считает «database is locked» порчей → карантин; отличать при старте (T-371).
- 2026-11-04 от ворот T-369: nginx перед API — `proxy_read_timeout` > 480 с и `proxy_buffering off` для SSE (T-371).
- 2026-11-04 от T-370: развернуть сервер весов по `deploy/README.md` — nginx-юнит, `llmhub-weights-auth` (`llmhub-wauth`, группа `llmhub`), сертификат, `LLMHUB_INSTANCE_SECRET`/`LLMHUB_WEIGHTS_SECRET`, суммы `sha256-T365.txt`, группа `llmhub` у `weights-acct.log`, опрос `/metrics`; tc/QoS — вопрос (T-371).
- 2026-11-04 от T-370: composition root — `Controller(materials=make_material_fns(...))`, `make_cloud_inference(...)` для реальных провайдеров, Mock — `MockInference`; потребитель `on_timings` (EWMA pp/tg, доля MTP) (T-371).
- 2026-11-04 от reviewer T-370 (R3-2/R3-3): admin-ручка/CLI `clear-weights-hold <pool>` (сейчас только `Controller.clear_weights_hold`) + runbook к инциденту `weights_config` critical; нижняя граница `retry.host_blacklist_s` (T-371).
- 2026-11-04 от reviewer T-370 (R3-1, R2-6): на реальных замерах проверить HF 429 (`rate_limited` → чёрный список хоста, ≤ 3 запуска на волну) и пессимизм `_eta` на старте (T-372).
- 2026-11-04 от reviewer T-369: сбой `list_offers` (5xx) метится `gpu_unavailable` вместо `provider_api_error` — в `provisioner.py` записывать `last_error` через `normalize_error` и передавать его в `_reason_of` при истечении `offer_wait_s` (T-372).
- 2026-11-04 от reviewer T-369 (SEC-L2): `/docs`, `/redoc`, `/openapi.json` доступны без ключа — флаг `serve` для их отключения (`docs_url=None` и т. д.) (T-371).
- 2026-11-04 от reviewer T-369 (SEC-L4): лимит одновременных запросов/задач на клиента (429); `waiter._tick` — `IN (...)` пачками по 500 (> 32766 переменных ломает тик) (T-371).
- 2026-11-04 от ворот T-371 (advocate F6/OPEN-3): `waiter._tick` — `IN (...)` пачками по 500 (> 32766 переменных ломает тик) (T-372).
- 2026-11-04 от ворот T-371 (advocate F6/OPEN-3, R3-3 T-370): нижняя граница `retry.host_blacklist_s` (`ge=3600`) (T-372).
- 2026-11-04 от ворот T-372 (critic F12): вынесено из T-372 — desk-spike dstack (0,5 дня), пессимизм `_eta` (R2-6), HF 429 → hold пула (R3-1), `waiter._tick` пачками `IN` по 500, удаление network volumes/шаблонов RunPod, `transport: direct` + `endpoint` в BOOTING, учение обрыва сети iptables (новая задача).
