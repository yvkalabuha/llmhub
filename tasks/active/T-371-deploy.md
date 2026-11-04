# T-371: 3d: деплой

> TL;DR: systemd-служба на 65.109.95.35, конфиг, health/metrics, порт весов, e2e на Mock, docs/integration.md для gameDev и VoiceBot T-366.

- Проект: llmhub
- Тип: chore
- Агент: analyst → implementer → reviewer
- Статус: review (код; шаги сервера S0–S14 не выполнены — их выполняет координатор)
- План: tasks/plans/T-371-plan.md
- Приоритет: P1
- Зависит от: T-369, T-370
- Документы: decisions/ADR-0138-cloud-gpu-manager.md; /var/www/projectVoiceBot/platform/docs/research/T-367-cloud-gpu-manager.md («Ворота 2026-11-04», ADR-0138 п. 1, 12–13); T-367-tz-v2.md (ТЗ)

## Цель
Поставить llmhub рядом с llama-server на 65.109.95.35 (API 10.8.0.1:8095, только VPN) в режиме Mock, проверить e2e и выдать клиентам руководство. Заказчик разрешил открыть порт весов 2026-11-04; ключей провайдеров и трат нет.
## Критерии приёмки
- [ ] `deploy/llmhub.service`, `llmhub-reaper.service/.timer`, `config.example.yaml`; `/etc/llmhub/` (providers.env 600, пустой/Mock); служба не от root; локальные юниты llama-server не изменены (статус и конфиги до/после совпадают).
- [ ] `/healthz` и `/metrics` (Prometheus) доступны по VPN; CLI `llmhub status|hold|release|kill|resume|budget|client`.
- [ ] Порт весов открыт по разрешению заказчика (2026-11-04): только HTTPS, только каталог GGUF, limit_rate и ≤ 2 загрузок; VPN и llama-server работают как прежде. Тариф Hetzner на трафик 8–12 ТБ/мес проверен или вопрос заказчику.
- [ ] e2e на Mock на сервере: chat, stream, task, lease, kill (0 ресурсов ≤ 60 с), перезапуск службы без потерь.
- [ ] `docs/integration.md` доведён из черновика до рабочего: OpenAI-совместимость, tasks/leases по raw HTTP, таймауты клиента; ссылки на OpenAPI и `examples/`; пригоден для gameDev (C#/.NET) и VoiceBot T-366.
- [ ] Резервные копии БД и откат установки описаны. Реальных ключей провайдеров на сервере нет.
## Подзадачи
- [x] План + ворота
- [x] юниты и конфиг (Docker: run-args, config.example.yaml, скрипт шагов; локально)
- [ ] установка на сервер (момент согласует координатор)
- [ ] порт весов
- [ ] e2e и integration.md
## Ревью
<заполняет reviewer: находки, команды, результаты, вердикт>
## Журнал
- 2026-11-04: задача создана координатором при создании репозитория.
- 2026-11-04: analyst — план `tasks/plans/T-371-plan.md`: код — потоки A (integrity busy), C (конфиг, 429 `rate_limited`, docs-флаг, `host_blacklist_s≥3600`), D (waiter пачки, `api_key_file`), B (админ-CLI, `keys new --out`), E (юниты, конфиг, render, скрипт шагов), F (e2e/проба весов), G (runbook, integration.md); сервер — S0–S14 с check/apply/rollback (координатор). Осмотр сервера (только чтение): Ubuntu 22.04, Python 3.10 без uv, nginx нет (:80 — apache2, :443 — xray), 9443/8095/8096 свободны, llama-server слушают 10.8.0.1, uplink 1 Гбит/с. Блокирующих для implementer нет; Q1–Q10 — заказчику/координатору. P1: готов к воротам critic + advocate. Статус planned.
- 2026-11-04: analyst — план v2 после ворот (critic: accept with changes; advocate: needs-decision) и решений заказчика: деплой в Docker (`llmhub-api`, `llmhub-reaper`, `llmhub-weights-auth`, `llmhub-weights-nginx`, все `--network host`, образ из wheelhouse по Id/digest, не-root, read-only, секреты файлами `*_FILE`), лимит весов 80m, регистрация MCP после деплоя (S12). Два окна: 1 — API+reaper+e2e на Mock, 2 — веса+ufw 9443+проба. Добавлены F1 (ленивый ключ llama), F4/F7 (самосторож reaper + healthcheck вместо timer), F5 (тест скрипта в одноразовом dind), F6 (directio, порог MemAvailable), F7 (`mock_only`, удаление `e2e` в конце), F8 (только GET /health|/v1/models), F11 (`llmhub backup`). `IN`-пачки и `host_blacklist_s≥3600` → T-372; `nginx-api.example.conf` убран. Тексты F9/F12/F13 analyst не получил — сверить координатору. Статус planned.
- 2026-11-04: implementer — **код готов, статус review**. Шаги сервера S0–S14 не выполнялись: ssh не использовался, сервер не трогался. Их выполняет координатор по `deploy/README.md` («Порядок») и плану §6.
  - Потоки A, C, D, R, B, F, E, G, I выполнены по TDD. Решения координатора (Q3 — GET `/slots` в фоне; F9, F12, F13) внесены в план §13, чек-лист — в §14.
  - Изменены: `src/llmhub/{__init__ (0.2.0), __main__, server}.py`, `persistence/{integrity,backup,__init__}.py`, `configuration/{loader,api_models,models,weights_models}.py`, `api_gateway/{errors,submission,app}.py`, `local_inference/client.py`, `reaper/{core,__main__}.py`, `weights/auth_app.py`, `deploy/{nginx-weights.conf,README.md}`, `docs/{integration.md,openapi.yaml}`, `scripts/export_openapi.py`, `.gitignore` (`dist/`); тесты `test_w_nginx_conf.py` (образ nginx по digest, имена `llmhub-test-w-*`, `__MAX_DL__`), `test_api_errors.py` (22 кода), `test_int_live.py` (`probe_interval_s` 10), `test_int_runtime.py` (разные секреты), `test_smoke.py` (версия), `fake_llama.py` (`/v1/models`).
  - Созданы: `src/llmhub/admin_cli.py`, `src/llmhub/tools/{__init__,weights_probe}.py`, `scripts/e2e_mock.py`, `deploy/{config.example.yaml,render.py,logrotate-llmhub-acct}`, `deploy/docker/{Dockerfile,.dockerignore,images.env,build.sh,run-args/*.args}`, `deploy/systemd/llmhub-backup.{service,timer}`, `deploy/server/llmhub-deploy.sh`, `docs/runbook.md`, 16 файлов `tests/test_t371_*.py`.
  - Проверки: `ruff check src tests scripts deploy` — чисто; `pyright` — 0 ошибок. Быстрый набор: 3092 passed (114 deselected). Полный набор со slow и docker, включая F5: 3205 passed, 1 skipped (dotnet), 11 мин 38 с. Тесты задачи: 177 быстрых + e2e (slow) + F5 (docker, около 6 мин).
  - Сборка: `dist/0.2.0/` (в `.gitignore`); `image.id` = `sha256:7ccc7dfd48feb3764cbf37080ca82e1782df8726367d7727b07242994f61e7e2`, `llmhub-0.2.0.tar.gz` 65 МБ (образ 263 МБ на диске / 68 МБ контента); nginx `sha256:65645c7b…` (`nginx:1.27-alpine`) — `nginx-weights-0.2.0.tar.gz` 21 МБ. Локальные теги после сборки удалены, артефакт — tar.
  - Отклонения от плана:
    - (1) `probe` по умолчанию остался `slots` (решение Q3), в `config.example.yaml` стоит `probe: slots`. В фоне только GET, интервал ≥ 10 с.
    - (2) Порог сторожа reaper — `max(3×loop, 45 + 2×loop)` = 75 с при loop 15, чтобы не оборвать блокирующий `kill_all` (45 с). Healthcheck `--max-age 90`.
    - (3) F12: `/var/log/llmhub` = `root:llmhub 0750`, nginx получает `--group-add 64100`, `su` в logrotate не используется. Со `su` скрипт `postrotate` шёл бы от пользователя без доступа к docker, и учёт объёма был бы занижен. Новые файлы создаются `create 0640 llmhub-wnginx llmhub`.
    - (4) admin `hold` GPU не запускает (только держит живую). Поэтому фаза `kill` в e2e и проба S10w используют client prewarm lease (`e2e_mock.py prewarm` / `close-lease`), а `s10x` (удаление `e2e`) перенесён в конец окна 2, после S10w («в конце T-371»).
    - (5) Публичный сертификат весов лежит отдельно: `/etc/llmhub/weights-pub/` (api и валидатор монтируют только его, ключ остаётся в `/etc/llmhub/weights` у nginx).
    - (6) Секреты создаются через `head -c 48 /dev/urandom | base64` (без openssl на хосте). CLI и проверки работают из образа, на хосте нужны только bash, coreutils, docker и ufw.
    - (7) Образ nginx сохраняется в dist, и сервер его не скачивает; контейнеры запускаются по Id.
    - (8) F5: «сервер» — привилегированный ubuntu:22.04 со своим dockerd (статические бинарники из `docker:29-dind`, `--iptables=false`, без bridge). ufw, systemctl и logrotate в нём — заглушки (`LLMHUB_UFW`/`LLMHUB_SYSTEMCTL`/`LLMHUB_LOGROTATE`); в копии dist `journald` заменён на `local`, потому что во вложенном демоне нет journald. Заглушка llama не нужна: бэкенды указывают на `127.0.0.1:1`.
    - (9) Добавлены `render.py --drop-client`, `weights_probe --op auto`, healthcheck `--health-start-interval 2s`.
  - Инцидент при отладке: временный контейнер `llmhub-test-api-dbg` с `config.example.yaml` в bridge-сети через VPN рабочей станции сделал фоновые `GET /slots` к настоящим `10.8.0.1:8090–8092` без ключа (ответ 401, только чтение; контейнер жил ≈ 20–30 с — 1–2 GET на бэкенд, затем удалён). Других обращений к серверу не было. В F5 и прочих тестах бэкенды указывают на `127.0.0.1:1` или фейки, это проверяет `test_h_no_real_units.py`.
  - Известные ограничения:
    - `reload-clients` в CLI против LiveStack без config_path проверяется только на ответ API.
    - Тест сборки C# по-прежнему пропускается: нет dotnet.
    - На сервере не проверены: journald-драйвер, настоящий ufw/logrotate, tun0, `--group-add 998` с реальным ключом llama, чтение `state.db` валидатором с ro-монтирования под нагрузкой. Всё это проверяют S6/S8c/S10w.
