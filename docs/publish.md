# Публикация: Gitea (git.myapphub.tech), сайт и бот на своём сервере

Схема: код живёт в Gitea, на сервере приложений лежит клон репозитория, nginx раздаёт `web/` как сайт, бот крутится как systemd-сервис. Обновление одной командой или автоматически через Gitea Actions.

## 1. Репозиторий в Gitea

1. Войти в https://git.myapphub.tech → «+» → «Новый репозиторий». Имя `car-import-bot`, приватный или публичный на ваш выбор, без инициализации README.
2. На этой машине добавить remote и запушить:

```bash
git remote add gitea https://git.myapphub.tech/<ваш_логин>/car-import-bot.git
git push -u gitea main
```

При первом пуше Git спросит логин и пароль. Вместо пароля вставьте токен: в Gitea → Настройки → Приложения → «Создать токен» с правами `repository: read and write`. Git Credential Manager запомнит его.

Если хотите, чтобы GitHub-копия тоже обновлялась: `git push origin main` (origin уже указывает на github.com/slava130913/car-import-bot). Можно удалить её: `git remote remove origin`.

## 2. Сервер для бота и сайта

Подойдёт любой VPS с Ubuntu 22.04+. Выполнить от root:

```bash
apt-get update && apt-get install -y git nginx python3 python3-venv certbot python3-certbot-nginx
git clone https://git.myapphub.tech/<ваш_логин>/car-import-bot.git /opt/car-import-bot
cd /opt/car-import-bot
cp .env.example .env
nano .env        # BOT_TOKEN, ADMIN_IDS, BOT_USERNAME, при желании VIN_PRICE_STARS и ANTHROPIC_API_KEY
sudo bash deploy/install.sh
```

Скрипт создаст виртуальное окружение, соберёт `web/`, поставит и запустит сервис `car-import-bot`. Проверка: `journalctl -u car-import-bot -f`, в Telegram написать боту `/start`.

Для приватного репозитория клонируйте по SSH: на сервере `ssh-keygen -t ed25519`, публичный ключ добавить в Gitea → Настройки → SSH/GPG ключи, затем `git clone git@git.myapphub.tech:<логин>/car-import-bot.git`.

## 3. Сайт калькулятора

```bash
cp /opt/car-import-bot/deploy/nginx-web.conf /etc/nginx/sites-available/car-calc
nano /etc/nginx/sites-available/car-calc      # server_name → ваш домен, например calc.myapphub.tech
ln -s /etc/nginx/sites-available/car-calc /etc/nginx/sites-enabled/
nginx -t && systemctl reload nginx
certbot --nginx -d calc.myapphub.tech          # HTTPS, обязателен для Telegram Mini App
```

DNS: A-запись домена на IP сервера. После этого сайт открывается по https://calc.myapphub.tech, кнопки ведут на бота из `BOT_USERNAME`.

Mini App: @BotFather → `/mybots` → бот → Bot Settings → Menu Button → URL сайта.

## 4. Обновление после изменений

На своей машине: закоммитить и `git push gitea main`. На сервере:

```bash
bash /opt/car-import-bot/deploy/deploy.sh
```

Скрипт делает `git pull`, обновляет зависимости, пересобирает `web/` и перезапускает бота. Сайт обновляется сразу, потому что nginx читает файлы из той же папки.

## 5. Автодеплой через Gitea Actions (необязательно)

Файл `.gitea/workflows/ci.yml` уже в репозитории: на каждый пуш гоняет тесты, при пуше в main деплоит по SSH.

1. В Gitea включить Actions (в `app.ini`: `[actions] ENABLED = true`) и подключить runner по инструкции Gitea (act_runner), либо убедиться, что он уже есть у вас в Site Administration → Actions → Runners.
2. На сервере приложений создать ключ для деплоя: `ssh-keygen -t ed25519 -f ~/.ssh/deploy_car -N ""`, публичный добавить в `~/.ssh/authorized_keys` пользователя, который имеет право запускать `systemctl restart car-import-bot` (root или sudo без пароля для этой команды).
3. В репозитории Gitea → Settings → Actions → Secrets добавить `DEPLOY_HOST`, `DEPLOY_USER`, `DEPLOY_KEY` (содержимое приватного ключа).

Без секретов workflow просто гоняет тесты.

## 6. Если бот и Gitea на одном сервере

Это нормально: бот не слушает портов, nginx добавляет ещё один `server`-блок рядом с блоком Gitea. Следите только, чтобы `server_name` отличались.

## 7. Что проверить после публикации

- `/start` в боте отвечает, расчёт проходит, заказ VIN приходит админу.
- Сайт открывается по HTTPS, расчёт работает, кнопки ведут на бота.
- `systemctl status car-import-bot` показывает `active (running)` после перезагрузки сервера.
