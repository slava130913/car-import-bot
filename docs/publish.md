# Публикация: Gitea myapphub + сервер studio-myapphub-1

Схема та же, что у остальных проектов myapphub (см. `D:\Claude\git\docs\CI-DEPLOY.md`): push в `main` → Gitea Actions → тесты → деплой по SSH на `deploy@188.166.91.146` в `/home/deploy/car-import-bot` → `docker compose up -d --build` → проверка → при неудаче откат.

## Что уже настроено

- Репозиторий https://git.myapphub.tech/myapphub/car-import-bot (приватный), remote `gitea` на этой машине. Пуш идёт от пользователя codex-agent через Git Credential Manager.
- Actions включены, workflow `.gitea/workflows/deploy.yml` и скрипт `.gitea/scripts/ci-ssh.sh` из шаблона myapphub, тесты в `ci/test.sh`.
- Переменные репозитория (Settings → Actions → Variables): `DEPLOY_TARGETS`, `DEPLOY_PATH=/home/deploy/car-import-bot`, `DEPLOY_EXCLUDES`, `DEPLOY_BUILD` (подключает override общей сети Caddy), `DEPLOY_ACTIVATE=docker compose up -d --build`, `DEPLOY_HEALTHCHECK`, `DEPLOY_HEALTH_RETRIES`.
- Секрет `DEPLOY_ENV_FILE`: серверный `.env` с пустым `BOT_TOKEN`. Попадает в `/home/deploy/car-import-bot/shared/.env`.
- Контейнеры: `bot` (Python, aiogram) и `web` (nginx со статикой калькулятора, в сети Caddy под именем `car-web`). Данные бота в томе `car-import-bot_bot-data`, релизы его не трогают.
- Токен владельца `ci-car-import` создан в Gitea → Настройки → Приложения для `ci.py`. Если больше не нужен, удалите его там же.

## Что осталось сделать руками

### 1. Токен бота (без него бот ждёт и ничего не делает)

1. @BotFather → `/newbot` → токен. @userinfobot → ваш id.
2. Обновить серверный `.env` одной командой (файл не попадает в git):

```powershell
$env:GITEA_TOKEN = '<токен ci-car-import или новый>'
python D:\Claude\git\scripts\ci.py set-secret DEPLOY_ENV_FILE --repo myapphub/car-import-bot --file D:\secure\car-import-bot.env
```

Содержимое файла:

```
BOT_TOKEN=123456:AA...
ADMIN_IDS=ваш_id
BOT_USERNAME=username_бота_без_@
VIN_PRICE_STARS=0
PAYMENT_INSTRUCTIONS=Мы свяжемся с вами для оплаты и пришлём отчёт в течение 24 часов.
ANTHROPIC_API_KEY=
EDGE_NETWORK=bytoprompt-studio_default
```

3. Перезапустить деплой: Actions → deploy → Run workflow (или любой push в main). Бот подхватит токен.

### 2. Домен сайта

Сейчас сайт работает на GitHub Pages: https://slava130913.github.io/car-import-bot/. Чтобы отдавать его со своего сервера:

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
