# T-371: 3d: деплой

> TL;DR: systemd-служба на 65.109.95.35, конфиг, health/metrics, порт весов, e2e на Mock, docs/integration.md для gameDev и VoiceBot T-366.

- Проект: llmhub
- Тип: chore
- Агент: analyst → implementer → reviewer
- Статус: todo
- План: tasks/plans/T-371-plan.md (создаёт analyst)
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
- [ ] План + ворота
- [ ] юниты и конфиг
- [ ] установка на сервер (момент согласует координатор)
- [ ] порт весов
- [ ] e2e и integration.md
## Ревью
<заполняет reviewer: находки, команды, результаты, вердикт>
## Журнал
- 2026-11-04: задача создана координатором при создании репозитория.
