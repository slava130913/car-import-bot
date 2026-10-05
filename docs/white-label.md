# Боты клиентов (white label)

## Основной способ: клиент запускает бот сам за 5 минут

Все боты клиентов работают в том же процессе, что и основной бот (`bot/tenants.py`). Отдельный сервер или контейнер на клиента не нужен.

1. Клиент открывает `t.me/<основной бот>?start=partner` и нажимает «Запустить свой бот за 5 минут» (или команда `/mybot`).
2. Создаёт бота у @BotFather (`/newbot`) и присылает токен. Бот проверяет токен через `getMe`, удаляет сообщение с ним из чата.
3. Вводит название компании, контакт менеджера и свою комиссию в рублях. Комиссия добавляется в расчёт отдельной строкой.
4. Бот сохраняет клиента в таблицу `tenants` (пробный период `TRIAL_DAYS`, по умолчанию 7 дней), ставит описание, команды и кнопку меню с веб-калькулятором (`WEB_URL?bot=&brand=&fee=`) и сразу начинает опрашивать Telegram за нового бота.
5. Клиент открывает своего бота и нажимает «Старт», иначе Telegram не даст присылать ему заявки.

Данные клиентов разделены полем `tenant_id` во всех таблицах. Заявки бота клиента видят только его админы (`/addadmin`).

Команды клиента в его боте: `/leads`, `/report`, `/settings`, `/setfee`, `/setmanager`, `/setbrand`, `/addadmin`, `/stats`, `/export`, `/broadcast`.
Команды владельца платформы в основном боте: `/tenants` (список и счётчики), `/tenant_on <id> <дней>` (оплачен), `/tenant_off <id>` (пауза).
Когда пробный период закончился, бот раз в час проверяет это и пишет клиенту и владельцу платформы. Бот клиента при этом не отключается.
Если клиент отозвал токен у @BotFather, его бот ставится на паузу, владелец платформы получает уведомление.

## Запасной способ: отдельный экземпляр на клиента

Нужен, если клиент хочет свой сервер или отдельную базу. Каждый клиент получает отдельный экземпляр: свой токен, своё название, свои заявки и своя база. Код общий, обновления из `main` доходят до всех.

## Что взять у клиента

- Токен бота из @BotFather (или создать бота за клиента и передать ему права позже).
- Название компании для приветствия и контакт менеджера.
- Telegram id менеджера (узнать в @userinfobot), туда пойдут заявки.
- Логотип для аватара: @BotFather → `/setuserpic`.

## Запуск на сервере studio-myapphub-1 (15–30 минут)

1. В Gitea создать репозиторий-зеркало `myapphub/car-bot-<клиент>` из `myapphub/car-import-bot` (Новая миграция → Gitea → зеркало) или форк. Workflow деплоя приедет вместе с кодом.
2. Включить Actions и задать переменные так же, как в основном репозитории, но со своим путём:

```powershell
$env:PYTHONUTF8 = '1'
$R = 'myapphub/car-bot-client1'
python D:\Claude\git\scripts\ci.py enable-actions $R
python D:\Claude\git\scripts\ci.py set-var DEPLOY_TARGETS "deploy@188.166.91.146" --repo $R
python D:\Claude\git\scripts\ci.py set-var DEPLOY_PATH "/home/deploy/car-bot-client1" --repo $R
python D:\Claude\git\scripts\ci.py set-var DEPLOY_EXCLUDES "./data/bot.sqlite3 ./reports ./.venv ./.venv-ci ./.pytest_cache ./.git" --repo $R
python D:\Claude\git\scripts\ci.py set-var DEPLOY_BUILD "cp deploy/compose.override.shared-network.yml compose.override.yml" --repo $R
python D:\Claude\git\scripts\ci.py set-var DEPLOY_ACTIVATE "docker compose up -d --build" --repo $R
python D:\Claude\git\scripts\ci.py set-var DEPLOY_HEALTHCHECK "docker compose exec -T web wget -qO- http://127.0.0.1/rules.json >/dev/null" --repo $R
python D:\Claude\git\scripts\ci.py set-secret DEPLOY_ENV_FILE --repo $R --file D:\secure\car-bot-client1.env
```

`PYTHONUTF8=1` обязателен: без него `ci.py` читает файл в кодировке Windows и портит кириллицу.

3. Файл `car-bot-client1.env`:

```
BOT_TOKEN=<токен клиента>
ADMIN_IDS=<id менеджера клиента>,8798276329
BOT_USERNAME=<username бота клиента>
BRAND_NAME=<Название компании>
MANAGER_CONTACT=@<менеджер>
VIN_PRICE_STARS=0
PAYMENT_INSTRUCTIONS=Менеджер свяжется с вами для оплаты.
COMPOSE_PROJECT_NAME=car-client1
WEB_ALIAS=client1-web
EDGE_NETWORK=bytoprompt-studio_default
```

Свой id в `ADMIN_IDS` можно оставить на время первой недели, чтобы видеть, как идут заявки, потом убрать.

4. Actions → deploy → Run workflow. Проверить `/start` в боте клиента.
5. Сайт клиента (пакет «Бот + сайт»): DNS-запись на 188.166.91.146 и блок в Caddyfile Студии `calc.client.ru { reverse_proxy client1-web:80 }`.

## Обновления

Изменения в `myapphub/car-import-bot` попадают к клиентам при синхронизации зеркала (раз в 8 часов по умолчанию) или кнопкой «Синхронизировать» в настройках зеркала, после чего деплой запускается сам.
