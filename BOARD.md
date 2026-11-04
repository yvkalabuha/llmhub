# BOARD — llmhub

> TL;DR: Только активные, todo и blocked задачи, строка ≤ 120 символов. Детали — в файле задачи. Выполненные → `tasks/done/` + строка в `tasks/done/INDEX.md`.

| ID | Задача | Тип | Статус | Зависит |
|---|---|---|---|---|
| T-368 | 3a: ядро на Mock (SQLite, автомат, leases, очередь, бюджет, сторож) | impl | todo | — |
| T-369 | 3b: API (OpenAI+tasks+leases+admin), router, адаптер llama-server | impl | todo | T-368 |
| T-370 | 3c: веса (WeightSource, сервер весов) и образ llmhub-infer | impl | todo | T-368 |
| T-371 | 3d: деплой systemd на 65.109.95.35, e2e на Mock, integration.md | impl | todo | T-369, T-370 |
| T-372 | Этап 4: spike dstack + тест RunPod ≤ $5 | test | blocked | T-371 |

## Входящие
<!-- "YYYY-MM-DD от <источник>: <что> (T-XXX)". -->
