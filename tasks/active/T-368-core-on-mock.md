# T-368: 3a: ядро на Mock

> TL;DR: Ядро llmhub без сети и денег: SQLite-схема, автомат GPU, leases, очередь, бюджет и ledger, сторож, MockProvider со сбоями, контрактные тесты адаптеров.

- Проект: llmhub
- Тип: impl
- Агент: analyst → implementer → reviewer (ворота critic + advocate на план, P1)
- Статус: todo
- План: tasks/plans/T-368-plan.md (создаёт analyst)
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
- [ ] Ворота critic + advocate на план
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
