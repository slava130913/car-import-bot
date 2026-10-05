# Публикация: Gitea myapphub + сервер studio-myapphub-1

Схема та же, что у остальных проектов myapphub (см. `D:\Claude\git\docs\CI-DEPLOY.md`): push в `main` → Gitea Actions → тесты → деплой по SSH на `deploy@188.166.91.146` в `/home/deploy/car-import-bot` → `docker compose up -d --build` → проверка → при неудаче откат.

## Что уже настроено

- Репозиторий https://git.myapphub.tech/myapphub/car-import-bot (приватный), remote `gitea` на этой машине. Пуш идёт от пользователя codex-agent через Git Credential Manager.
- Actions включены, workflow `.gitea/workflows/deploy.yml` и скрипт `.gitea/scripts/ci-ssh.sh` из шаблона myapphub, тесты в `ci/test.sh`.
- Переменные репозитория (Settings → Actions → Variables): `DEPLOY_TARGETS`, `DEPLOY_PATH=/home/deploy/car-import-bot`, `DEPLOY_EXCLUDES`, `DEPLOY_BUILD` (подключает override общей сети Caddy), `DEPLOY_ACTIVATE=docker compose up -d --build`, `DEPLOY_HEALTHCHECK`, `DEPLOY_HEALTH_RETRIES`.
- Секрет `DEPLOY_ENV_FILE`: серверный `.env` с токеном бота, ADMIN_IDS и BOT_USERNAME. Попадает в `/home/deploy/car-import-bot/shared/.env`.
- Контейнеры: `bot` (Python, aiogram) и `web` (nginx со статикой калькулятора, в сети Caddy под именем `car-web`). Данные бота в томе `car-import-bot_bot-data`, релизы его не трогают.
- Токен владельца `ci-car-import` создан в Gitea → Настройки → Приложения для `ci.py`. Если больше не нужен, удалите его там же.

## Что осталось сделать руками

### 1. Бот (сделано)

Бот @china_car_calc_bot создан, токен и ADMIN_IDS лежат в секрете `DEPLOY_ENV_FILE` и в локальном `.env`. Команды, описание и кнопка меню (Mini App на GitHub Pages) заданы в BotFather. Чтобы поменять настройки на сервере:

```powershell
$env:GITEA_TOKEN = '<токен ci-car-import или новый>'
python D:\Claude\git\scripts\ci.py set-secret DEPLOY_ENV_FILE --repo myapphub/car-import-bot --file D:\Claude\car\.env
```

затем Actions → deploy → Run workflow.

### 2. Домен сайта

Основной вариант: сайт и форма заявок на хостинге reg.ru, заявки хранятся в РФ. Шаги в [regru.md](regru.md).

Запасной вариант: отдавать статику со своего сервера (без формы заявок, PHP там нет). Сейчас сайт работает на
GitHub Pages: https://slava130913.github.io/car-import-bot/.

1. reg.ru → DNS `myapphub.tech` → A-запись `car` → `188.166.91.146`.
2. В Caddyfile Студии (`/opt/bytoprompt-studio/deploy/Caddyfile`, эталон `D:\Codex\mygit\deploy\Caddyfile.git`) добавить блок:

```
car.myapphub.tech {
    reverse_proxy car-web:80
}
```

3. На сервере: `docker exec bytoprompt-studio-caddy-1 caddy reload --config /etc/caddy/Caddyfile`.

Caddy сам выпустит сертификат. После этого Mini App: @BotFather → Bot Settings → Menu Button → https://car.myapphub.tech/.

### 3. Проверка

- Ход деплоя: https://git.myapphub.tech/myapphub/car-import-bot/actions
- На сервере: `cd /home/deploy/car-import-bot/current && docker compose ps && docker compose logs --tail 50 bot`
- В Telegram: `/start` боту, расчёт, заказ VIN приходит админу.

## Обновление

```bash
git add -A && git commit -m "Что изменилось" && git push gitea main
```

Копия на GitHub обновляется отдельно: `git push origin main`. Если GitHub больше не нужен, `git remote remove origin` и удалите репозиторий там.

## Запуск на своём компьютере

Без Docker: `powershell -ExecutionPolicy Bypass -File run.ps1`. С Docker: `docker compose up -d --build`, сайт на http://localhost:8080.
