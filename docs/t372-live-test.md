# T-372 — живой тест RunPod: чек-лист координатора

> TL;DR: Пошаговый чек-лист живого теста RunPod ≤ $5 (план `tasks/plans/T-372-plan.md` §6): P0 без трат, L0 — сторож на рабочей станции, L1 — ключ только для чтения, L2 — одно разрешение заказчика и lifecycle-под, L3–L6 — три холодных старта и учения, L7 — сверка денег, L8 — откат и снятие сторожа.
> Выполняет только координатор. После каждого платного шага проверяется: 0 подов `llmhub-*` и `currentSpendPerHr == 0` дважды с интервалом ≥ 60 с. Сессия не закрывается, пока это не так.
> Ключи нигде не печатаются: ни в терминале, ни в журнале, ни в отчёте. Значения передаются только файлом или через stdin.

Обозначения: `S=root@65.109.95.35`, `D=/root/llmhub-dist/0.3.1`, `G=scripts/runpod_guard.py` (запуск `python3 -I`), `ST=~/.local/state/llmhub-guard`.
CLI на сервере: `docker exec -i llmhub-api python -m llmhub <cmd> ...` (команды API — с `--admin-key-file /dev/stdin < /etc/llmhub/admin.key`).

## Условия остановки (сразу `kill`, затем `G destroy-leftovers --yes`, затем разбор)
1. Ошибка разбора ответа RunPod: `RunpodParseError` в журнале или код 4 сторожа (в том числе `STOP: guard state reset`). Код 7 сторожа (ошибка API или неудачный DELETE). Код 8 сторожа: нет читаемого `before.json`, сторож не работает.
2. Есть под без строки в БД или вне шага. Под жив > 5 мин после шага. `currentSpendPerHr ≠ 0` после шага. Сработал порог сторожа (код 6).
3. Найден network volume или шаблон `llmhub-*`, либо в diff появился любой новый том, шаблон, registry auth или endpoint (код 5).
4. Под RUNNING > 15 мин без прогресса к READY.
5. Инцидент critical (`unknown_owned_resource`, `kill_incomplete`, `ownership_mismatch`, `hard_cap`, `created_resource_rejected`, `created_resource_destroy_failed`) или high (`create_unknown_unresolved`, `tls_pin_mismatch`). При `created_resource_rejected` ref пода — в `details_json` и в `instances.provider_ref`; если есть `created_resource_destroy_failed`, удалить этот под в консоли.
6. `cost_per_hr > 0,30` или `currentSpendPerHr > 0,30`.
7. Баланс упал от снимка «до» на ≥ $3,00. Мягкие стопы: Δ ≥ $1,50 или ledger ≥ $2,00 — продолжать только после «да» заказчика.
8. Шаг длится дольше 2× оценки. Баланс < $15. rw-ключ получил 401/403. Ключ или токен виден в выводе. reaper `unhealthy`.

## Проверка «ноль» (после каждого платного шага)
```
python3 -I $G snapshot --key-file ~/.config/runpod/api-key --out $ST/zero-1.json
sleep 65
python3 -I $G snapshot --key-file ~/.config/runpod/api-key --out $ST/zero-2.json
python3 -I $G diff $ST/before.json $ST/zero-2.json   # код 0; spend_per_hr=0.000 в обоих
```

### P0 — без трат
1. Схема RunPod (ключ не нужен):
   `curl -fsS https://rest.runpod.io/v1/openapi.json -o tests/fixtures/runpod/openapi.json`
   `uv run python scripts/runpod_schema_check.py tests/fixtures/runpod/openapi.json` → `schema: ok`;
   `uv run pytest tests/test_t372_runpod_schema.py -q` → тест реального файла зелёный, не skipped.
   Если есть расхождения (поля, enum, пагинация `GET /pods`), не тратить: исправить адаптер.
2. Образ: `nice -n 10 deploy/docker/publish-infer.sh scan` → `dist/0.3.1/infer.scan.txt` = `verdict: clean` и ровно одна строка `allowed: layer 0 …: usr/lib/x86_64-linux-gnu/libgnutls.so.30.37.1: private_key (GnuTLS FIPS 140 self-test vectors …)` (решение заказчика 2026-11-04: точный путь и digest базового слоя, REV-S6). Любая другая строка `allowed:` или находка — стоп.
   `deploy/docker/publish-infer.sh push` (токен `~/.config/llmhub/ghcr-token`, 0600, `--password-stdin`, временный `DOCKER_CONFIG`) → `dist/0.3.1/infer.digest`.
   Заказчик ставит пакету видимость public. Проверка анонимного pull: `DOCKER_CONFIG=$(mktemp -d) docker pull $(cat dist/0.3.1/infer.digest)` и размер образа (F7c).
3. Консоль RunPod: auto-pay выключен, Low balance alert включён, баланс ≤ $25.

### L0 — сторож до первого пода (D3)
```
install -d -m 0700 ~/.local/lib/llmhub-guard ~/.config/llmhub-guard $ST
install -m 0600 scripts/runpod_guard.py ~/.local/lib/llmhub-guard/runpod_guard.py
install -m 0600 deploy/workstation/guard.env.example ~/.config/llmhub-guard/guard.env   # затем вписать пути и GUARD_UNTIL
install -m 0644 deploy/workstation/llmhub-runpod-guard.{service,timer} ~/.config/systemd/user/
python3 -I $G snapshot --key-file ~/.config/runpod/api-key --out $ST/before.json   # код 0; подов llmhub-* в «до» — 0
systemctl --user daemon-reload
systemctl --user enable --now llmhub-runpod-guard.timer
journalctl --user -u llmhub-runpod-guard -n 5      # одна строка llmhub_pods=0 spend/h=0.000
```
`GUARD_UNTIL` в `guard.env` — плановый конец теста + 6 ч, UTC (`date -u -d '+14 hours' +%Y-%m-%dT%H:%M:%SZ` для теста на 8 ч). После этого срока сторож только сообщает и ничего не удаляет (REV-S9). Без `--until` срок — `taken_at` снимка «до» + 24 ч.
Сторож хранит время первого обнаружения каждого пода в `$ST/first_seen.json` (0600). Возраст пода = сейчас − min(первое обнаружение, `lastStartedAt`): под, который ещё тянет образ, уже тарифицируется, а `lastStartedAt` у него нет (REV-S2). Без `--state-dir` автоудаление не запускается (код 2). Если файл испорчен, он сбрасывается и проход завершается кодом 4 (стоп) — уже после проверок и удалений.
Если `before.json` потерян или испорчен, юнит завершается кодом 8 и печатает `STOP: no usable before snapshot` — это условие остановки, а не тихое отключение.
rw-ключа на рабочей станции ещё нет. Пока его нет, юнит падает на `--rw-key-file` (код 2); это видно в журнале. До L2 достаточно ручного `watch --once` без `--auto-destroy`.

### L1 — ключ только для чтения, релиз 0.3.1, конфиг теста
1. Ключ: `ssh $S 'install -m 0400 -o 64100 -g 0 /dev/stdin /etc/llmhub/secrets/runpod' < ~/.config/runpod/api-key`.
2. Релиз 0.3.1: s1, s3; затем `llmhub-deploy.sh s6 apply` и `llmhub-deploy.sh s6k apply` (s6k пересоздаёт api и reaper с монтированием ключа; у weights-auth и nginx его нет).
3. Клиент `t372` (на рабочей станции): `uv run python -m llmhub keys new --name t372 --out ~/.config/llmhub/t372.key` — на экран выводится только sha256, он идёт в `clients.t372.key_sha256`. Файл ключа (0600) хранит координатор.
4. Источник HF (без ключа, только чтение). Репозиторий — `unsloth/Qwen3.8-27B-GGUF` (T-367-gpu-engines, T-364, T-365 §2); пути шаблона уже верные: `Qwen3.8-27B-UD-Q4_K_M.gguf` и `MTP/mtp-Qwen3.8-27B-Q4_0.gguf` (голова MTP в подпапке `MTP/`). Revision — 40-hex SHA коммита, не `main`:
   ```
   REV=$(curl -fsS https://huggingface.co/api/models/unsloth/Qwen3.8-27B-GGUF | python3 -I -c 'import json,sys;print(json.load(sys.stdin)["sha"])')
   for f in Qwen3.8-27B-UD-Q4_K_M.gguf MTP/mtp-Qwen3.8-27B-Q4_0.gguf; do
     curl -fsSI "https://huggingface.co/unsloth/Qwen3.8-27B-GGUF/resolve/$REV/$f" | grep -i '^x-linked-etag'
   done
   cat /opt/models/sha256-T365.txt   # на сервере: ssh $S cat /opt/models/sha256-T365.txt
   ```
   Значение `x-linked-etag` (LFS-oid, без кавычек) каждого файла должно совпасть с его sha256 из `/opt/models/sha256-T365.txt`. Не совпало — не продолжать: веса не те, что проверены в T-365. Итог: `LLMHUB_HF_BASE_URL=https://huggingface.co/unsloth/Qwen3.8-27B-GGUF/resolve/$REV/`.
   Конфиг теста: `LLMHUB_CONFIG_TEMPLATE=config.t372.example.yaml LLMHUB_RENDER_SET="pools.cloud-27b.image=$(cat infer.digest);clients.t372.key_sha256=<hash>" LLMHUB_WEIGHTS_CHAIN=hf LLMHUB_HF_BASE_URL=https://huggingface.co/unsloth/Qwen3.8-27B-GGUF/resolve/$REV/ llmhub-deploy.sh s5 apply`. Затем s8b apply с теми же переменными (веса из T-365). `config-check` должен вывести `ok`.
5. Mock-ledger (Q5, только после конфига с `enabled: [runpod]`):
   `docker exec llmhub-api python -m llmhub ledger-exclude --provider mock --config /etc/llmhub/conf/config.yaml` — dry run показывает число строк и сумму;
   затем то же с `--yes` → `backup` создан, строки помечены `excluded_at`.
6. GraphQL (R2-10). Вызовы GraphQL (`gpuTypes … lowestPrice { uninterruptablePrice stockStatus }`, `myself { clientBalance currentSpendPerHr spendLimit }`) в openapi нет: они **не сверены** со схемой и проверяются только здесь, ключом только для чтения, до любой траты. Ключ не попадает в argv: `printf` — встроенная команда bash, заголовок идёт через `-H @<(…)`:
   ```
   curl -fsS https://api.runpod.io/graphql -H 'Content-Type: application/json' \
     -H @<(printf 'Authorization: Bearer %s\n' "$(head -n1 ~/.config/runpod/api-key)") \
     -d '{"query":"query { myself { clientBalance currentSpendPerHr spendLimit } gpuTypes(input:{id:\"NVIDIA GeForce RTX 3090\"}) { id displayName memoryInGb lowestPrice(input:{gpuCount:1, secureCloud:false}) { uninterruptablePrice stockStatus } } }"}'
   ```
   Ожидается: нет `errors`; `uninterruptablePrice` ≈ 0,22 (это Community, не Secure); `stockStatus` не null. Если ошибка только про `spendLimit` (поля нет), повторить запрос без него: код это переносит сам (`myself` повторяет запрос без `spendLimit`, `spend_limit=None`); записать вывод в журнал. Любая другая ошибка — стоп до L2. Тем же ключом: `GET /v1/pods` → `[]` (массив, не обёртка):
   `curl -fsS https://rest.runpod.io/v1/pods -H @<(printf 'Authorization: Bearer %s\n' "$(head -n1 ~/.config/runpod/api-key)")`.
7. `docker exec llmhub-api python -m llmhub provider-check --config /etc/llmhub/conf/config.yaml` показывает: оффер 3090 с ценой, `owned: []`, баланс, биллинг за месяц.
8. Запись ответов: `python3 -I $G record --key-file ~/.config/runpod/api-key --dir tests/fixtures/runpod/recorded`;
   `uv run pytest tests/test_t372_runpod_api.py tests/test_t372_fixture_scan.py -q` (A2b, F-scan) зелёные.

### L2 — единственное разрешение заказчика (D1), lifecycle-под
1. AskUserQuestion: ≤ $5, RunPod, срок — сессия теста. Ответ записать в журнал задачи.
2. rw-ключ: `ssh $S 'install -m 0400 -o 64100 -g 0 /dev/stdin /etc/llmhub/secrets/runpod' < ~/.config/runpod/api-key-rw`, затем `llmhub-deploy.sh s6k apply`: новый inode, контейнеры пересоздаются. На рабочей станции `api-key-rw` (0600); в журнале таймера идут проходы без ошибок.
3. L2b: `docker exec llmhub-api python -m llmhub lifecycle-probe --config /etc/llmhub/conf/config.yaml --image nvidia/cuda:12.8.1-base-ubuntu24.04@sha256:<digest> --max-s 480` (`--hold-s 150` по умолчанию).
   `desiredStatus` — желаемое состояние: RUNNING сразу после POST (R2-6). Поэтому проба держит под `--hold-s` секунд после ответа create. **Во время удержания** на рабочей станции:
   ```
   python3 -I $G watch --key-file ~/.config/runpod/api-key --before $ST/before.json --once   # под виден, код 0
   python3 -I $G record --key-file ~/.config/runpod/api-key --dir tests/fixtures/runpod/recorded   # настоящий Pod (A2b)
   ```
   и запись таймера сторожа в `journalctl --user -u llmhub-runpod-guard` с `llmhub_pods=1`.
   В выводе пробы: `name_ok: true`, `cost_per_hr`, `visible_s`, `desired_running_s` (желаемое, не фактическое), `held_s` ≥ 150, `destroy_gone_s`, `zero: true`. Значение `destroy_gone_s` с запасом записать в `providers.runpod.list_staleness_s`.
   Входит ли диск в `costPerHr`, видно по сумме: цена 3090 + диск. Строка пула `_probe` уходит в ERROR → STOPPING → STOPPED сама. Затем проверка «ноль».
   После «ноль» (R2-7): повторить `DELETE` и `GET` по старому id пода (ref из вывода пробы) и записать коды ответа. Ожидается 204/400 на DELETE и 404 (или 400) на GET; адаптер и сторож считают 400/404 на DELETE «удалён», если `GET` даёт 404 (или пода нет в списке):
   ```
   R=<ref>
   curl -sS -o /dev/null -w 'DELETE %{http_code}\n' -X DELETE "https://rest.runpod.io/v1/pods/$R" -H @<(printf 'Authorization: Bearer %s\n' "$(head -n1 ~/.config/runpod/api-key-rw)")
   curl -sS -o /dev/null -w 'GET %{http_code}\n' "https://rest.runpod.io/v1/pods/$R" -H @<(printf 'Authorization: Bearer %s\n' "$(head -n1 ~/.config/runpod/api-key)")
   ```

### L3 — старт 1: только HF + учение «контроллер недоступен» (D2)
`LLMHUB_WEIGHTS_CHAIN=hf`. Prewarm пула `cloud-27b` клиентом `t372`. Записать время pull образа, скорость HF и фазы.
**Контрольная точка:** первый READY и один ответ. Дальше — только после «да» координатора.
Два запроса (5k/300 и 1,3k/4k), замер ток/с.
Учение: `docker stop llmhub-api` на 5 мин → `docker exec llmhub-reaper python -m llmhub kill --file --config /etc/llmhub/conf/config.yaml` (API лежит, файл `KILL` пишет контейнер reaper) → reaper удаляет под ≤ 60 с без API (журнал reaper, сторож, консоль) → `docker start llmhub-api` → recovery → удалить `KILL` → `resume`. Затем проверка «ноль».

### L4 — старт 2: `self_hosted` → HF, proxy
`LLMHUB_WEIGHTS_CHAIN=self_hosted,hf`, s5 apply. Замер Hetzner→RunPod.
После READY: `docker exec -i llmhub-api python -m llmhub weights-token-check --config /etc/llmhub/conf/config.yaml --op <op_id>`. Ожидается `http_status: 403` и `expected_verdict: revoked`. Но 403 сам по себе ничего не доказывает: так же отвечают `ip_mismatch` (IP уже привязан к поду, а запрос идёт с сервера), `path_not_allowed` и `volume_cap` (R2-8). **Доказательство отзыва** — строка журнала weights-auth после `checked_at` из вывода (команда есть в поле `proof`):
   `docker logs llmhub-weights-auth --since <checked_at> 2>&1 | grep 'op=<op_id> verdict='` → последняя строка `verdict=revoked`. Её записать в журнал. Другой вердикт — отзыв не доказан. (401 бывает только без токена.)
Логи пода из консоли: `docker exec -i llmhub-api python -m llmhub scan-secrets --op <op_id> < pod-logs.txt` → `matches: 0`.
Три запроса, из них один с ответом > 100 с. Idle 5 мин → destroy. Затем проверка «ноль».

### L5 — старт 3: только `self_hosted`
`LLMHUB_WEIGHTS_CHAIN=self_hosted`. Один запрос; idle → destroy. Затем проверка «ноль».

### L6 — учение kill
Старт, затем в LOADING_MODEL `kill` через API → 0 подов ≤ 60 с (`provider-check`, сторож, консоль) → `resume`. Затем проверка «ноль».

### L7 — сверка денег (через ≥ 2 ч)
Главная сверка (F11): Δ баланса RunPod между `before.json` и новым снимком, в центах, против суммы ledger (`budget`). Допуск max(5¢; 5 %).
Биллинг по каждому podId (`provider-check` → `billing_refs`): каждый podId есть в `instances.provider_ref`, сумма ≈ ledger. `get(<старые ref>)` → None.

### L8 — откат и снятие сторожа
1. Конфиг: `llmhub-deploy.sh s5 apply` без `LLMHUB_CONFIG_TEMPLATE` (`providers.enabled: [mock]`, клиента `t372` нет).
2. Ключ: `llmhub-deploy.sh s6k rollback` (`shred -u`, api и reaper без монтирования).
3. Итоговая проверка «ноль» (дважды ≥ 60 с), снимок «после», `G diff before after` = 0 новых ресурсов.
4. Только после этого снять сторож (D3):
```
systemctl --user disable --now llmhub-runpod-guard.timer
rm -f ~/.config/systemd/user/llmhub-runpod-guard.service ~/.config/systemd/user/llmhub-runpod-guard.timer
rm -f ~/.config/llmhub-guard/guard.env && systemctl --user daemon-reload
```
5. Заказчик отзывает rw-ключ RunPod и GitHub-токен. Отчёт в журнал задачи. Цифры из «Наблюдений» плана §6 (READY ≤ 10 мин, медиана, прогноз ±30 %, ток/с, скорости, pull) — это наблюдения, а не критерии.
