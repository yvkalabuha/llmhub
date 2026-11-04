# T-368: 3a: ядро на Mock

> TL;DR: Ядро llmhub без сети и денег: SQLite-схема, автомат GPU, leases, очередь, бюджет и ledger, сторож, MockProvider со сбоями, контрактные тесты адаптеров.

- Проект: llmhub
- Тип: impl
- Агент: analyst → implementer → reviewer (ворота critic + advocate на план, P1)
- Статус: in-progress
- План: tasks/plans/T-368-plan.md
- Приоритет: P1
- Зависит от: —
- Документы: decisions/ADR-0138-cloud-gpu-manager.md; /var/www/projectVoiceBot/platform/docs/research/T-367-cloud-gpu-manager.md (§6.3, §6.4, §6.5, §6.6, §6.7, §6.8, §6.9, §9); T-367-tz-v2.md (ТЗ)

## Цель
Реализовать ядро (этап 3a и очередь/деньги прежнего 3b исследования) целиком на MockProvider: ни одного реального вызова провайдера и ни одного ключа. Модули: configuration, persistence, lifecycle_manager, task_queue, scheduler, budget_manager, cost_tracker, reaper, cloud_manager, provider_adapters/{base,mock}.
## Критерии приёмки
- [ ] Схема SQLite WAL по §6.6 (clients, jobs, leases, instances, transitions, ledger, reservations, provider_accounts, provider_snapshots, incidents, controller_state); миграции, `integrity_check`, бэкап; порча БД -> безопасный режим HALTED.
- [ ] Частичный UNIQUE(pool) WHERE state NOT IN ('STOPPED','PENDING_DELETE','LEAKED'); тест: новый цикл пула при PENDING_DELETE без второго живого инстанса в индексе.
- [ ] Автомат STOPPED→PROVISIONING→BOOTING→LOADING_MODEL→READY⇄BUSY⇄IDLE→STOPPING→STOPPED, ERROR→STOPPING; PENDING_DELETE (destroy принят) и LEAKED (нет нуля за 15 мин -> инцидент, блок провайдера, HALTED); таймауты состояний, `max_lifetime` 3 ч; решение IDLE→STOPPING в одной транзакции с проверкой очереди и leases; каждый переход и строка таблицы §6.3 покрыты тестом.
- [ ] Leases implicit/explicit (пульс, TTL 10 мин)/hold (≤ 8 ч)/prewarm; тест: агент молчит 20 мин с живым lease — GPU жива; drain после пачки; `idle_timeout` 0–15, по умолчанию 5.
- [ ] Очередь urgent/normal/low, пакеты low (5 задач / 30 мин, ожидание ≤ 90 мин), round-robin по клиентам, Idempotency-Key 7 суток, неявная дедупликация sha256(client+тело) ~15 мин, 409 при конфликте; триггеры запуска пула по §6.7.
- [ ] Бюджет: оценка P·(T_cold_p90+ΣT_work+T_idle)+трафик, резервы, лимиты (сторож $50, менеджер $45/мес, $3/сут, $1/сеанс, $0,25/задание, подбюджет exception $10), предупреждения 50/80/95 % и по темпу, ступени 80 % ECONOMY / 95 % только urgent / 100 % нет запусков; нехватка баланса — блокировка провайдера без повторов и автопополнения, resume только вручную; распределение простоя по клиентам.
- [ ] Ledger (gpu/disk/traffic/storage), сверка с балансом/биллингом Mock; расхождение ≤ max($0,05; 5 %).
- [ ] Сторож (reaper) — отдельный процесс; kill-switch: kill -> 0 ресурсов ≤ 60 с (на Mock); удаление только при совпадении HMAC-метки `llmhub-<op_id>-<hmac8>` и записи в БД; восстановление сверкой с `list_owned`.
- [ ] `ProviderAdapter` Protocol (list_offers, create(spec,label), get, list_owned, идемпотентный destroy, balance, billing_since, normalize_error) + Capability; метода stop нет. MockProvider с инъекцией: задержки старта/загрузки, ошибки API, 429, неизвестный исход create, нехватка баланса, нет GPU, «остановлен, но платит», orphan. Контрактные тесты адаптеров (`contract.py`): новый провайдер проходит их без изменения ядра.
- [ ] Сценарии ТЗ §15 отдельными тестами: 10 запросов -> 1 create; urgent не ждёт пакета; один Idempotency-Key -> одна задача; перезапуск в PROVISIONING и BUSY без потерь и лишних ресурсов; прерывание GPU -> повторная постановка; задача во время STOPPING -> новый цикл; исчерпание бюджета/баланса; неизвестный исход create -> один ресурс; порча БД.
- [ ] Прогон S1–S6/RF2–RF6 симулятора через Mock с ускоренными часами: число запусков и $ совпадают ±5 %.
- [ ] `uv run pytest`, `ruff check`, `pyright` чисто; ревью ≤ 3 раундов; автомат/leases/бюджет/create-reconcile — 2 независимых цикла ревью.
## Подзадачи
- [x] Ворота critic + advocate на план (2026-11-04)
- [ ] persistence + configuration
- [ ] provider_adapters base/mock + contract
- [ ] lifecycle_manager + leases
- [ ] task_queue + scheduler
- [ ] budget_manager + cost_tracker
- [ ] reaper + kill-switch
- [ ] сценарии §15 и S1–S6
## Ревью
<заполняет reviewer: находки, команды, результаты, вердикт>
## Журнал
- 2026-11-04: задача создана координатором при создании репозитория.
- 2026-11-04: analyst — план `tasks/plans/T-368-plan.md` (41 шаг, потоки A–F, DDL, таблица переходов E1–E22, тесты §15 и 19 ситуаций); статус planned; следующий шаг — ворота critic + advocate.
- 2026-11-04: analyst — правки по воротам critic + advocate внесены в план (§14: 1 high, 11 medium, low, advocate a–e, решения Q1/Q3/Q4/hold); S0 добавлен, 42 шага; Capability.SPEND_RATE — расширение списка §6.5; статус planned.
- 2026-11-04: implementer — A1–A5 и S0 выполнены (A6 — следующий). Пакеты: `clock.py`; `configuration/{models,loader}.py`; `persistence/{db.py, migrations/0001_initial.sql}` (13 таблиц, DDL из §4 дословно); `provider_adapters/{base,labels,mock,mock_store,mock_inference}.py`; `cloud_manager/{inference,pools,offers,provisioner}.py`; `lifecycle_manager/{states,machine,leases,teardown,recovery}.py`; `task_queue/{models,bodies,queue}.py`; `budget_manager/limits.py` (только `Gate`); `killswitch.py` (KillReport, check_kill_file); `controller.py`; `tests/harness.py`. Тесты: `test_a_clock`, `test_a_config`, `test_a_schema`, `test_a_unique_pool` (A4 + гонка 10 потоков), `test_a_tx` (A5: контроллер 300 мс -> `Busy`, сторож 5000 мс ждёт), `test_s0_skeleton` (happy path PROVISIONING→...→STOPPED, детерминизм, повторное использование инстанса, CAS, таблица рёбер). Итог: pytest 66 passed, ruff и pyright чисто.
- 2026-11-04: отклонения и уточнения интерфейсов: (1) `trigger` в `transition`/`check_edge` — id ребра `E1`..`E22` (у E15 шесть рёбер, у E17 два; всего 28 рёбер); подробность причины — `reason=` в `**fields` (по умолчанию = id), в `transitions.reason`; (2) `transition` и `set_provider_ref` присоединяются к открытой транзакции или открывают свою; `insert_provisioning` и `try_idle_to_stopping` всегда одна своя; (3) E1 пишет строку `transitions` (STOPPED→PROVISIONING); (4) `connect(path, busy_timeout_ms=300)`; `tx` при занятой БД бросает `persistence.db.Busy`; вложенный `tx` — AssertionError; (5) `queue.complete(..., res_path=None)` — добавлен необязательный аргумент; (6) функции очереди лежат в `task_queue/queue.py`, типы — `task_queue/models.py`; (7) `JobRunSpec(job_id, model_alias, tokens_in, max_tokens, body)` и `JobRunResult(tokens_in, tokens_out, finish_reason, body)` — поля план не задавал; (8) заглушки с `NotImplementedError`: `MockProvider.inject/stopped_but_billing`, `queue.{requeue,fallback_local,cancel,requeue_orphaned_running,purge_expired}`, `find_duplicates`, `kill_all`, `Controller.{kill,resume,hold,release}`; (9) в S0 нет резерва `session` в `insert_provisioning`, KILL-файла в `step()`, таймаутов состояний, GuardedAdapter; (10) `pyproject.toml`: marker `slow`, `asyncio_default_fixture_loop_scope`, pyright `extraPaths=["tests"]`; `tests/conftest.py` не создавался (не понадобился).
