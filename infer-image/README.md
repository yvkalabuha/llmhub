# llmhub-infer — образ пода

> TL;DR: Образ облачной GPU: `llama.cpp server-cuda` (CUDA 12.8.1, b12063 по digest) + фронт на Python (TLS/ключ API, `/health` с прогрессом) + загрузчик весов (цепочка `self_hosted` → `hf`, sha256, Range, переход по ошибке и скорости). Весов, секретов и адресов в образе нет — всё приходит через env от контроллера. **Публикация (push) — только с разрешения заказчика.**

## Контракт env (контроллер → под)
Собирает `llmhub.cloud_manager.instance_env.build_material`, разбирает `llmhub_infer.supervisor.read_env`; согласованность проверяет `tests/test_w_contract.py`.

| Переменная | Секрет | Значение |
|---|---|---|
| `LLMHUB_OP_ID` | нет | 12 hex |
| `LLMHUB_TRANSPORT` | нет | `direct` (TLS в поде, pin) или `proxy` (HTTP за прокси провайдера) |
| `LLMHUB_PORT` | нет | 8443 (direct) / 8080 (proxy) |
| `LLMHUB_WEIGHTS_PLAN` | нет | JSON v1: файлы (size, sha256), источники, `deadline_unix` |
| `LLMHUB_LLAMA_ARGS` | нет | JSON-список; `-m -md --host --port --api-key* --ssl-*` запрещены |
| `LLMHUB_MAX_LIFETIME_S`, `LLMHUB_WEIGHTS_DIR` | нет | информационно; каталог весов (`/models`) |
| `LLMHUB_API_KEY` | да | `HMAC(instance_secret, "api:"+op_id)` |
| `LLMHUB_WEIGHTS_TOKEN` | да | токен сервера весов (есть источник `self_hosted`) |
| `LLMHUB_TLS_CERT_PEM`, `LLMHUB_TLS_KEY_PEM` | да | только `direct`; Ed25519 из `HMAC(instance_secret, "tls:"+op_id)` |
| `HF_TOKEN` | да | по умолчанию **не передаётся** (репозиторий публичный); только при `weights.hf_token_env`, только fine-grained read-only на этот репозиторий |

Секреты уходят провайдеру в `InstanceSpec.secret_env` (`repr=False`), в БД, логи и `transitions` не попадают. llama-server получает env без `LLMHUB_*` и без имён с TOKEN/SECRET/KEY.

## Семантика фронта
- `GET /ping` → 204 без ключа (BOOTING → LOADING_MODEL).
- `GET /health` (ключ): 503 `{"status":"loading", phase, file, source, done, total, mb_s, failovers, eta_s, sources_left}`; 503 `phase=llama_load`; 200 `{"status":"ok"}` только когда llama-server ответил 200; 500 `{"status":"failed","reason"}` (`weights`, `deadline`, `llama_exited`, `internal`) — процесс жив, перезапусков нет.
- `/v1/*`, `/props` (ключ) → потоковый прокси на `127.0.0.1:8081`; до READY — 503 `{"error":"not ready"}`; llama недоступна — 502 `{"error":"upstream unavailable"}`.

Контроллер (`HttpInferenceBackend`): `failed` → ранний E7 `weights_failed:<reason>`; нет запасного источника и прогноз ETA не укладывается в `loading_model_s` на двух опросах (не раньше 60 с) → E7 `weights_too_slow`; `eta_s = null` — «неизвестно», раннего E7 нет.

## Ручные проверки (C5, без push)
```
docker run --rm -i hadolint/hadolint@sha256:30a8fd2e785ab6176eed53f74769e04f125afb2f74a6c52aef7d463583b6d45e < infer-image/Dockerfile
nice -n 10 docker build -t llmhub-infer:dev infer-image/
docker run --rm --entrypoint /app/llama-server llmhub-infer:dev --help | grep -E 'draft-mtp|spec-draft-n-max'
docker run --rm llmhub-infer:dev   # без env: код 2, «bad environment: LLMHUB_OP_ID: required»
```
GPU локально нет: запуск llama-server в контейнере и замер загрузки — этап 4 (T-372).

## Публикация
GHCR public `ghcr.io/<owner>/llmhub-infer:<ver>`, в конфиге пула — по digest. Push, владелец GHCR и тег — только с разрешения заказчика (T-371/T-372). Пин `python3.12=3.12.3-1ubuntu0.17` в Dockerfile при пересборке через месяцы обновить (архив Ubuntu удаляет старые версии).
