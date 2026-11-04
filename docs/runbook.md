# Runbook llmhub (сервер 65.109.95.35)

> TL;DR: Как эксплуатировать llmhub в Docker на 65.109.95.35. Работают четыре контейнера `llmhub-*` (все `--network host`); журналы лежат в journald, CLI вызывается через `docker exec -i … --admin-key-file /dev/stdin`. Здесь собраны инциденты (`weights_config`, `db_corrupt`, код 75, KILL, `tls_pin_mismatch`, `low_memory`), бэкап и восстановление, ротация ключей и сертификата, обновление и откат (S14). Шаги установки — `deploy/README.md` и план T-371 §6.

## 1. Что где
| Что | Где |
|---|---|
| API (контроллер, локальный воркер, шлюз) | контейнер `llmhub-api`, `10.8.0.1:8095` (только VPN, правило ufw на `tun0`) |
| reaper (сторож облака) | `llmhub-reaper`; heartbeat `/var/lib/llmhub/reaper.hb` |
| валидатор токенов весов | `llmhub-weights-auth`, `127.0.0.1:8096` |
| nginx весов | `llmhub-weights-nginx`, `65.109.95.35:9443` (HTTPS, только `.gguf`, 80m, 1 загрузка) |
| конфиг | `/etc/llmhub/config.yaml` (0640 `root:llmhub`); шаблон — `deploy/config.example.yaml` |
| секреты | `/etc/llmhub/secrets/{label,instance,weights}` (0640), `providers` (0600, пустой); `/etc/llmhub/admin.key` (0600) |
| данные | `/var/lib/llmhub` (`state.db*` 0640, `backup/`), `/var/lib/llmhub-weights-auth` |
| учёт весов | `/var/log/llmhub/weights-acct.log` (0640 `llmhub-wnginx:llmhub`), `/etc/logrotate.d/llmhub-acct` |
| релизы | `/root/llmhub-dist/<ver>/` (`image.id`, `nginx.id`, tar-образы, `llmhub-deploy.sh`, `run-args/`) |
| маркеры шагов | `/var/lib/llmhub-deploy/` (`hashes.txt` — только sha256 ключей, `preflight/baseline`) |

Пользователи без входа: `llmhub` 64100, `llmhub-wnginx` 64101, `llmhub-wauth` 64102 (фиксированные id). llama-server, dockerd, `daemon.json`, xray, OpenVPN и чужие контейнеры llmhub не трогает.

## 2. Состояние
- `docker ps --filter name=llmhub- --format '{{.Names}} {{.Status}}'` показывает `healthy` у всех четырёх.
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
- Ежедневно `llmhub-backup.timer` запускает `llmhub-backup.service`, а тот выполняет `docker exec llmhub-api python -m llmhub backup … --keep 14`. Копия делается через SQLite online backup в `*.tmp`, проверяется `integrity_check`, файлы получают права 0640. Результат: `/var/lib/llmhub/backup/state-<UTC>.db`. Вручную: `systemctl start llmhub-backup.service`.
- Восстановление:
  1. `docker stop llmhub-api llmhub-reaper` (только эти два).
  2. Сохранить текущие `state.db*` рядом (`mv state.db state.db.before-restore-<ts>`, так же `-wal`/`-shm`).
  3. `install -m 0640 -o llmhub -g llmhub backup/state-<ts>.db /var/lib/llmhub/state.db`.
  4. `docker start llmhub-api llmhub-reaper`; проверить `status` и `healthy`.
- Облачные ресурсы, созданные после времени копии, контроллер найдёт как orphan по HMAC-метке и удалит (reconcile при старте).

## 5. Ротация ключей и секретов
- **admin**: `python -m llmhub keys new --name admin --out <новый файл>` через `docker run --rm --network none --user 0 -v /etc/llmhub/keys:/out <image id> …`. Новый sha256 записать в `/var/lib/llmhub-deploy/hashes.txt` (строка `admin`), затем `llmhub-deploy.sh s5 apply`. Новый admin-ключ начинает действовать после `reload-clients` (он применяет и `admin_key_sha256`) или `docker restart llmhub-api`. Старый файл удалить `shred -u`.
- **клиенты** (`voicebot`, `gamedev`, `agents`): удалить `/etc/llmhub/keys/<n>.key`, выполнить `llmhub-deploy.sh s4 apply` (создаёт недостающие ключи и обновляет хеши), затем `s5 apply` и `reload-clients`. Ключ передать клиенту файлом 0600 (способ — Q6) и на сервере `shred -u`.
- **секреты `label`/`instance`/`weights`** без отдельного решения не ротировать: `label` подписывает метки облачных ресурсов (старые ресурсы перестанут распознаваться), `instance` выводит ключи и TLS подов.

## 6. Сертификат весов и порт 9443
- Перевыпуск: `llmhub-deploy.sh s8a rollback && s8a apply` (новый fingerprint в `/var/lib/llmhub-deploy/weights.fp`), затем `s8b apply` (api перечитает `ca_pem_file`) и `docker restart llmhub-weights-nginx`.
- Закрыть порт: `llmhub-deploy.sh s9 rollback` (удаляет только правило `llmhub weights`). Поды перейдут на HF.
- Проверка снаружи (F9): `/w/000000000000/qwen/x.gguf` без токена должен отвечать `401` (путь совпадает с regex location). Путь вне regex отвечает `404`, TLS 1.2 не принимается. Это делает `s9 check`.

## 7. Обновление релиза и откат
1. На рабочей станции: `nice -n 10 deploy/docker/build.sh` → `dist/<ver>/`. Скопировать каталог в `/root/llmhub-dist/<ver>/` и выполнить `llmhub-deploy.sh s1 check` (SHA256SUMS).
2. `s3 apply` нового релиза (`docker load`, сверка Id с `image.id`).
3. Контейнеры по новому Id: `s6 rollback` → `s6 apply` из нового каталога (данные остаются), для окна 2 так же `s8c`.
4. Откат: те же `s6`/`s8c`, но скриптом из `/root/llmhub-dist/<prev>/` (в его `image.id` старый Id).
- **Полный откат (S14)**: `llmhub-deploy.sh s14 apply`. Удаляются только `llmhub-*` контейнеры, оба правила ufw, таймер бэкапа и logrotate. `/etc/llmhub`, `/var/lib/llmhub*` и `/var/log/llmhub` архивируются в `/root/llmhub-rollback-<ts>.tgz` (0600) и **удаляются**. Затем удаляются образы и пользователи. В конце выполняется `s11`.
- **Переустановка после S14** (F13): `s2 apply` создаёт пользователей с теми же фиксированными uid/gid 64100–64102, поэтому владельцы файлов из архива остаются верными. Восстановление: `tar xzf … -C /`, затем `s2`…`s9 apply`.

## 8. Перед T-372 (реальный провайдер)
- Клиента `e2e` в конфиге быть не должно. `s10x apply` выполняется в конце T-371, после пробы весов S10w (ей нужен клиент с `cloud_allowed` для prewarm), и проверяется через `client list`. Конфиг с `mock_only` при реальном провайдере не пройдёт `config-check`.
- Сбросить Mock-ledger (Q7) по решению заказчика: суммы Mock не должны занимать реальный бюджет.
- `providers.enabled` расширяется только вместе с адаптером T-372 и разрешением заказчика. Ключи провайдера кладутся файлом в `/etc/llmhub/secrets/providers` (0600).
