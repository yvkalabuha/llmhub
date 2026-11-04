# Runbook llmhub (сервер 65.109.95.35)

> TL;DR: Как эксплуатировать llmhub в Docker на 65.109.95.35 (релиз 0.2.2, конфиг в каталоге `/etc/llmhub/conf/`). Работают четыре контейнера `llmhub-*` (все `--network host`); журналы лежат в journald, CLI вызывается через `docker exec -i … --admin-key-file /dev/stdin`. Здесь собраны инциденты (`weights_config`, `db_corrupt`, код 75, KILL, `tls_pin_mismatch`, `low_memory`), бэкап и восстановление, ротация ключей и сертификата, обновление и откат (S14). Шаги установки — `deploy/README.md` и план T-371 §6.

## 1. Что где
| Что | Где |
|---|---|
| API (контроллер, локальный воркер, шлюз) | контейнер `llmhub-api`, `10.8.0.1:8095` (только VPN, правило ufw на `tun0`) |
| reaper (сторож облака) | `llmhub-reaper`; heartbeat `/var/lib/llmhub/reaper.hb` |
| валидатор токенов весов | `llmhub-weights-auth`, `127.0.0.1:8096` |
| nginx весов | `llmhub-weights-nginx`, `65.109.95.35:9443` (HTTPS, только `.gguf`, 80m, 1 загрузка) |
| конфиг | `/etc/llmhub/conf/config.yaml` (0640 `root:llmhub`, каталог `conf/` 0750 `root:llmhub`); шаблон — `deploy/config.example.yaml`. Контейнеры монтируют **каталог** `conf/` (SEC-D2): после `mv` внутри него работающий контейнер видит новый файл. Копии прежних версий — `/etc/llmhub/config.yaml.bak-<ts>` (вне монтирования) |
| секреты | `/etc/llmhub/secrets/{label,instance,weights}` (0640), `providers` (0600, пустой); `/etc/llmhub/admin.key` (0600) |
| данные | `/var/lib/llmhub` (`state.db*` 0640, `backup/` — единственный каталог копий, §4), `/var/lib/llmhub-weights-auth` |
| учёт весов | `/var/log/llmhub/weights-acct.log` (0640 `llmhub-wnginx:llmhub`), `/etc/logrotate.d/llmhub-acct` |
| релизы | `/root/llmhub-dist/<ver>/` (`image.id`, `nginx.id`, tar-образы, `llmhub-deploy.sh`, `run-args/`) |
| маркеры шагов | `/var/lib/llmhub-deploy/` (`hashes.txt` — только sha256 ключей, `preflight/baseline`) |

Пользователи без входа: `llmhub` 64100, `llmhub-wnginx` 64101, `llmhub-wauth` 64102 (фиксированные id). llama-server, dockerd, `daemon.json`, xray, OpenVPN и чужие контейнеры llmhub не трогает.

## 2. Состояние
- `docker ps --filter name=llmhub- --format '{{.Names}} {{.Status}}'` показывает `healthy` у всех четырёх.
- Перед шагами с `daemon-reload` (`s6`, `s6 rollback`, `s14`) скрипт сам проверяет `systemctl show llama-server* -p NeedDaemonReload` и останавливается при `yes` (I1): правки юнитов llama, не применённые их владельцем, не активируются побочно.
- Журналы: `journalctl CONTAINER_NAME=llmhub-api -n 100` (так же для `llmhub-reaper`, `llmhub-weights-auth`, `llmhub-weights-nginx`). `docker logs` не используется: у контейнеров log driver journald, ротацию делает journald.
- CLI (ключ идёт через stdin, в argv и env не попадает):
  `docker exec -i llmhub-api python -m llmhub status --admin-key-file /dev/stdin < /etc/llmhub/admin.key`
  Команды: `status`, `budget`, `client list`, `reload-clients`, `hold --pool P --ttl S`, `release --pool P`, `kill [--reason R]`, `kill --file`, `resume [--provider X]`, `clear-weights-hold --pool P`, `config-check`, `backup --to DIR --keep N`, `healthcheck --url U`.
- `/metrics` (Prometheus) отдаётся только с admin-ключом: `curl -s -H @<файл с "Authorization: Bearer …"> http://10.8.0.1:8095/metrics`. Ключ на экран не выводить. Валидатор: `curl -s 127.0.0.1:8096/metrics`, счётчики `llmhub_weights_auth_total{verdict}`.
- Алерты (вручную или через внешний мониторинг): контейнер не `healthy`; `halted: true` в `status`; инциденты `critical`/`high`; рост `verdict="low_memory"` или `db_busy`.
- Проверка, что ничего чужого не изменилось: `llmhub-deploy.sh s11 check`. Он сравнивает текущее состояние со снимком S0: юниты и файлы llama, PID dockerd, `Id/StartedAt` чужих контейнеров, чужие правила ufw.

## 3. Инциденты
| Признак | Что делать |
|---|---|
| `weights_hold:<pool>` в status, причина `weights_config` | Исправить источник весов или конфиг (`render.py`, S8b), затем `clear-weights-hold --pool <pool>` (ответ `removed: true`) |
| `db_corrupt`, `safe_mode: true` (HALTED) | Файл переименован в `state.db.corrupt-<ts>`, служба создала пустой и стоит в safe mode. Разобрать копию; если нужно, восстановить из бэкапа (§4), затем `resume` |
| контейнер `llmhub-api` перезапускается, в журнале `state.db busy … exiting 75` | Код 75 (`StateDbBusy`) означает, что `state.db` 30 с был заблокирован другим процессом. Порчи нет, файл не трогается. Найти, кто держит блокировку (ручной `sqlite3`, восстановление); Docker перезапустит api сам |
| нужно остановить облако | `kill` (через API, ждёт до 60 с) или `kill --file` (пишет `/var/lib/llmhub/KILL`, работает и без API; reaper исполнит в течение 60 с). `resume` не сработает, пока файл существует: удалить `KILL`, затем `resume` |
| `kill_incomplete` | Часть ресурсов не подтвердила удаление. reaper продолжает попытки; смотреть `status.incidents` и журнал reaper |
| `tls_pin_mismatch` (high) | Сертификат пода не совпал с ожидаемым. Под не используется, инстанс останавливается. Это возможная атака или ошибка образа; разбирать до `resume` |
| `verdict="low_memory"` у валидатора | `MemAvailable` хоста ниже `weights.min_mem_available_mib` (8 ГиБ). Поды берут веса с HF. Проверить память llama-server; порог не снижать без решения заказчика |
| reaper `unhealthy` / выход 70 | Самосторож: проход не завершился за `max(3×loop, 45+2×loop)` с. Docker перезапускает контейнер. Если повторяется — смотреть журнал (зависший вызов провайдера, блокировка БД) |

## 4. Бэкап и восстановление
- Все копии лежат в одном каталоге `/var/lib/llmhub/backup/` (REV2-7), у каждого источника свой префикс и своя ротация:
  - `state-<UTC>.db` — `llmhub-backup.timer` (ежедневно) → `llmhub-backup.service` → `docker exec llmhub-api python -m llmhub backup … --keep 14`. Online backup в `*.tmp`, `integrity_check`, права 0640. Вручную: `systemctl start llmhub-backup.service`.
  - `controller-<UTC>.db` — собственная суточная копия контроллера (keep 7), делается без таймера.
  - Обе копии в режиме `journal_mode=DELETE` (REV2-5): если открыть копию для осмотра, рядом не появятся `-wal`/`-shm`.
- Восстановление. Строка в БД — единственное доказательство, что облачный ресурс наш. Если после времени копии создавались ресурсы, у них верная HMAC-метка, но строки в копии нет. Контроллер такой ресурс **не** удаляет: он записывает инцидент `unknown_owned_resource` (critical) и ставит **HALT**. reaper удалит ресурс примерно через 30 мин (REV2-6). Поэтому:
  1. Перед восстановлением выполнить `kill` (через API, ждёт до 60 с) и убедиться, что в `status` `teardown` пуст, а `pools.*.instance` равен `null`. Тогда облачных ресурсов нет. Если API недоступен — `kill --file`, затем дождаться reaper.
  2. `docker stop llmhub-api llmhub-reaper` (только эти два).
  3. Сохранить текущие `state.db*` рядом (`mv state.db state.db.before-restore-<ts>`, так же `-wal`/`-shm`).
  4. `install -m 0640 -o llmhub -g llmhub backup/state-<ts>.db /var/lib/llmhub/state.db` (или `controller-<ts>.db`).
  5. `docker start llmhub-api llmhub-reaper`; проверить `status` и `healthy`.
  6. Если `kill` в шаге 1 не выполнялся (или ресурс всё же нашёлся), ждать HALT с `unknown_owned_resource`. Разобрать инцидент: ресурс наш и создан после копии → дождаться удаления reaper'ом или удалить через `kill`. Только после этого — `resume`. Если перед восстановлением был `kill --file`, сначала удалить `/var/lib/llmhub/KILL`.

## 5. Ротация ключей и секретов
Конфиг лежит в смонтированном каталоге `/etc/llmhub/conf/` (SEC-D2). Если `s5 apply` (а также `s8b`, `s10x`) изменил `config.yaml`, скрипт сам перезапускает `llmhub-api`, `llmhub-reaper` и `llmhub-weights-auth` (те, что существуют) и ждёт `healthy`. `reload-clients` нужен только при правке конфига вручную.
- **admin**: `shred -u /etc/llmhub/admin.key`, затем `llmhub-deploy.sh s4 apply` (создаёт недостающий ключ и пишет его sha256 в `/var/lib/llmhub-deploy/hashes.txt`), затем `s5 apply` (новый `admin_key_sha256`, перезапуск api). Проверка: `admin_call status` новым ключом проходит, старым — 401.
- **клиенты** (`voicebot`, `gamedev`, `agents`): сохранить старый ключ для проверки (`install -m 0600 /etc/llmhub/keys/<n>.key /root/<n>.old`), удалить `/etc/llmhub/keys/<n>.key`, выполнить `llmhub-deploy.sh s4 apply` (новый ключ и хеши), затем `s5 apply` (конфиг меняется → api перезапускается). **Проверить, что работающий API отверг старый ключ**:
  `docker exec -i llmhub-api python -c "import sys, httpx; k = sys.stdin.readline().strip(); print(httpx.get('http://10.8.0.1:8095/v1/models', headers={'Authorization': 'Bearer ' + k}).status_code)" </root/<n>.old` → `401`, с новым ключом → `200`. Затем `shred -u /root/<n>.old`. Новый ключ передать клиенту файлом 0600 (способ — Q6) и на сервере `shred -u`.
- `s10x` (удаление `e2e`) делает такую проверку сам: до `shred` ключа он требует `401` от работающего API, а `s10x check` спрашивает `client list` у API, а не только файл.
- **секреты `label`/`instance`/`weights`** без отдельного решения не ротировать: `label` подписывает метки облачных ресурсов (старые ресурсы перестанут распознаваться), `instance` выводит ключи и TLS подов.

## 6. Сертификат весов и порт 9443
- Перевыпуск: `llmhub-deploy.sh s8a rollback && s8a apply` (новый fingerprint в `/var/lib/llmhub-deploy/weights.fp`), затем `docker restart llmhub-api llmhub-weights-nginx`. Путь `ca_pem_file` в конфиге не меняется, поэтому `s8b apply` конфиг не перепишет и api сам не перезапустит (I3); api читает `ca_pem_file` только при старте.
- Закрыть порт: `llmhub-deploy.sh s9 rollback` (удаляет только правило `llmhub weights`). Поды перейдут на HF.
- Проверка (F9): `/w/000000000000/qwen/x.gguf` без токена должен отвечать `401` (путь совпадает с regex location). Путь вне regex отвечает `404`, TLS 1.2 не принимается. `s9 check` проверяет это **с самого сервера**, то есть через `lo`, и правило ufw на `enp9s0` при этом не участвует (L7).
- **S9 с внешнего хоста — обязательно** (рабочая станция без VPN или любой внешний хост; нужен только публичный `weights-cert.pem`):
  ```
  curl -s -o /dev/null -w '%{http_code}\n' --cacert weights-cert.pem https://65.109.95.35:9443/w/000000000000/qwen/x.gguf   # 401
  curl -s -o /dev/null -w '%{http_code}\n' --cacert weights-cert.pem https://65.109.95.35:9443/w/000000000000/bench/run.py  # 404
  curl -s -o /dev/null --cacert weights-cert.pem --tls-max 1.2 https://65.109.95.35:9443/ ; echo "exit $?"                   # exit != 0
  timeout 5 bash -c '</dev/tcp/65.109.95.35/8095' ; echo "exit $?"                                                            # != 0 (API не виден снаружи)
  ```

## 7. Обновление релиза и откат
1. На рабочей станции: `nice -n 10 deploy/docker/build.sh` → `dist/<ver>/`. Скопировать каталог в `/root/llmhub-dist/<ver>/` и выполнить `llmhub-deploy.sh s1 check` (SHA256SUMS).
2. `s3 apply` нового релиза (`docker load`, сверка Id тегов `llmhub:<ver>` и `llmhub-nginx:<ver>` с `image.id`/`nginx.id` или `image.config-id`/`nginx.config-id`). Хранилище containerd показывает digest индекса, классическое (overlay2/vfs) — digest конфига (R3-5); фактический Id записывается в `/var/lib/llmhub-deploy/<image|nginx>-<ver>.local-id`, по нему запускаются контейнеры. Тип хранилища — `preflight/baseline/docker-store.txt` (s0). `s3 rollback` снимает только эти теги и только если их Id наш (SEC-D5, R3-2): Id нашего nginx совпадает с upstream `nginx:1.27-alpine`, и образ под чужим тегом остаётся.
3. Контейнеры по новому Id: `s6 rollback` → `s6 apply` из нового каталога (данные остаются), для окна 2 так же `s8c`.
4. Откат: те же `s6`/`s8c`, но скриптом из `/root/llmhub-dist/<prev>/` (в его `image.id` старый Id).
- **Полный откат (S14)**: `llmhub-deploy.sh s14 apply`. Удаляются только `llmhub-*` контейнеры, правила ufw, которые добавил сам скрипт (`/var/lib/llmhub-deploy/ufw-*.added`, L1), таймер бэкапа и logrotate. `/etc/llmhub`, `/var/lib/llmhub*` и `/var/log/llmhub` архивируются в `/root/llmhub-rollback-<ts>.tgz` (0600) и **удаляются**. Затем снимаются наши теги образов и удаляются пользователи и группы, записанные в `s2.created` (L2). В конце выполняется `s11`: новые слушатели на 0.0.0.0/`[::]` и чужие адреса на 8095/8096/9443 считаются ошибкой (L3).
- **Переустановка после S14** (F13): `s2 apply` создаёт пользователей с теми же фиксированными uid/gid 64100–64102, поэтому владельцы файлов из архива остаются верными. Восстановление: `tar xzf … -C /`, затем `s2`…`s9 apply`.

## 8. Перед T-372 (реальный провайдер)
- Клиента `e2e` в конфиге быть не должно. `s10x apply` выполняется в конце T-371, после пробы весов S10w (ей нужен клиент с `cloud_allowed` для prewarm), и проверяется через `client list`. Конфиг с `mock_only` при реальном провайдере не пройдёт `config-check`.
- Сбросить Mock-ledger (Q7) по решению заказчика: суммы Mock не должны занимать реальный бюджет.
- `providers.enabled` расширяется только вместе с адаптером T-372 и разрешением заказчика. Ключи провайдера кладутся файлом в `/etc/llmhub/secrets/providers` (0600).
