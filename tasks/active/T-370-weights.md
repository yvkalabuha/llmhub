# T-370: 3c: веса

> TL;DR: WeightSource цепочка self_hosted→hf, сервер весов на 65.109.95.35 (отдельный HTTPS, одноразовые токены), образ llmhub-infer без весов.

- Проект: llmhub
- Тип: impl
- Агент: analyst → implementer → reviewer
- Статус: in-progress
- План: tasks/plans/T-370-plan.md
- Приоритет: P1
- Зависит от: T-368
- Документы: decisions/ADR-0138-cloud-gpu-manager.md; /var/www/projectVoiceBot/platform/docs/research/T-367-cloud-gpu-manager.md (§6.12, «Ворота 2026-11-04» п. 2); T-367-tz-v2.md (ТЗ)

## Цель
Быстрый старт GPU (цель этапа 4: медиана ≤ 5 мин) за счёт загрузки весов с нашего сервера с запасным путём через HF. Реализация и тесты без реальной сети и без установки на сервер (установка — T-371).
## Критерии приёмки
- [ ] `weights.sources: [self_hosted, hf]` (R2 — опция, не реализуется); манифест с sha256; проверка контрольной суммы; докачка Range; переход к следующему источнику при ошибке **и** при скорости < `min_speed_mb_s` после 30 с — отдельные тесты на фейковых HTTP-источниках.
- [ ] Сервер весов (`deploy/nginx-weights.conf` + выдача токенов): отдельный read-only HTTPS endpoint только для каталога GGUF; токен одноразовый, на инстанс, привязан к `op_id`, живёт до READY или таймаута LOADING_MODEL, затем отозван; `limit_rate`; ≤ 2 одновременных загрузок; VPN и локальные llama-server не затрагиваются. Тесты токенов: повтор, истёкший, чужой op_id.
- [ ] Образ `llmhub-infer` (`infer-image/Dockerfile`): llama.cpp server-cuda (CUDA 12.8) собран внутрь, TLS-прокси + проверка ключа API, загрузчик весов; **веса не запекаются**. Сборка на платных ресурсах не выполняется; проверяются юнит-тесты загрузчика и lint Dockerfile.
- [ ] Режимы TLS `direct` (pin отпечатка) и `proxy` (ключ на инстанс) отражены в конфиге и спецификации инстанса; какой доступен — проверка этапа 4 (T-372).
- [ ] Ничего не открывается на сервере и не публикуется; pytest, ruff, pyright чисто; ревью ≤ 3 раундов.
## Подзадачи
- [ ] План + ворота (план готов, ворота critic + advocate — впереди)
- [ ] weights: цепочка, sha256, Range, failover
- [ ] выдача токенов + nginx-конфиг
- [ ] infer-image
## Ревью
<заполняет reviewer: находки, команды, результаты, вердикт>
## Журнал
- 2026-11-04: задача создана координатором при создании репозитория.
- 2026-11-04: analyst — план `tasks/plans/T-370-plan.md`: 8 потоков (0 → A/B/C/D → E, G → F); по решению координатора (Q6 из T-369) облачный `HttpInferenceBackend` — поток G, протокол `InferenceBackend` заморожен (прогресс — отдельный `LoadStatusSource`). Загрузка весов в поде (entrypoint на Python), прогресс через `/health` пода, отдельный nginx с `auth_request`, токен HMAC с отзывом по состоянию инстанса, TLS сервера весов — self-signed + pin. Образ FROM `server-cuda12-b12063@sha256:d5ffba7d…` (CUDA 12.8.1, проверено), публикация — GHCR public (push — с разрешения). Блокирующих вопросов нет; Q1–Q5 в §10. Статус planned.
- 2026-11-04: ворота ADR-0139 (critic + advocate) — решения внесены в план, раздел 2a:
  - одна загрузка с нашего сервера (`limit_rate 80m`, `w_all` = `segments` = 1), без tc;
  - токен один на инстанс и многоразовый до READY или таймаута (уточнение формулировки ADR-0138 п. 4 «одноразовый»), подпись без nonce, лимит объёма 1,2 × Σsize по acct-логу nginx, привязка к первому IP;
  - сегменты Range (`segments`: self_hosted 1, hf 4);
  - HF_TOKEN по умолчанию не передаётся;
  - права валидатора и WAL для T-371;
  - находки critic medium/low — все приняты; R3 исправлен по advocate.

  Статус planned.
- 2026-11-04: implementer, потоки 0 и A выполнены (B, C, D можно параллельно). Файлы: `pyproject.toml` (+`cryptography`, маркер `docker`, `pythonpath`/pyright `infer-image`), `infer-image/llmhub_infer/__init__.py`, `tests/tls_certs.py` (`make_self_signed`), `tests/test_w_tls_certs.py`, `tests/test_w_isolation.py`, `src/llmhub/configuration/weights_models.py` (новый, отдельный модуль — аддитивно для T-369), `models.py` (`PoolConfig.weights/infer`, `Config.weights` + проверки уровня Config), `loader.py` (`load_instance_secret`, `load_weights_secret`, `load_weights_ca`), `__init__.py` (ленивые экспорты), `examples/config.cloud.example.yaml`, `tests/test_w_config.py`. Отклонения: sha256 в примере — 64 нуля (валидатор требует hex), заглушки заполняются в T-371; `test_s` по умолчанию 30 константой. Проверка `direct` ⇒ `need_public_ip` — в потоке E. Итог: `pytest -m "not slow"` 629 passed, ruff check чисто, pyright 0 errors.
