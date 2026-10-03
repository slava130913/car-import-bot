# Авто из Китая под ключ: MVP

Репозиторий: Gitea git.myapphub.tech (основной), копия на https://github.com/slava130913/car-import-bot  
Сайт: GitHub Pages https://slava130913.github.io/car-import-bot/ (временный), на своём сервере по [docs/publish.md](docs/publish.md)

Telegram-бот и веб-калькулятор для тех, кто пригоняет машины из Китая.

Что внутри:

- **Калькулятор «под ключ»**: цена, перевод денег, расходы в Китае, логистика по маршрутам, пошлина по единым ставкам для физлиц, утильсбор (льготный и коммерческий), таможенный сбор, СБКТС, ЭПТС, доставка по России. Электромобили и последовательные гибриды считаются по схеме пошлина + акциз + НДС.
- **Заказ проверки по VIN**: клиент вводит VIN и контакт, платит в Telegram Stars (или по инструкции), админу приходит уведомление. Отчёт на этом этапе делается вручную.
- **Заявка «хочу пригнать»**: модель, бюджет, город, контакт. Передаётся агентам.
- **Админ-команды**: `/orders`, `/leads`, `/done <id>`, `/stats`. Готовый отчёт отправляется клиенту так: прикрепить файл в чат с ботом и подписать `/send <id>`.
- **Веб-версия** калькулятора и лендинг в `web/`, работает как обычный сайт и как Telegram Mini App.

Все ставки лежат в одном файле `data/rules.json` с датой версии и ссылками на источники. Код чисел не содержит.

## Структура

```
data/rules.json      ставки, диапазоны расходов, запасной курс, источники
calc/engine.py       расчёт (чистые функции)
calc/rates.py        курс ЦБ РФ с кешем и запасным вариантом
bot/main.py          Telegram-бот (aiogram 3)
bot/db.py            SQLite: пользователи, расчёты, заказы, заявки
web/index.html       лендинг + калькулятор
web/calc.js          порт engine.py на JS
scripts/build_web.py     копирует rules.json в web/ и пишет web/config.js (username бота)
scripts/vin_report.py    перевод китайского отчёта по VIN и сборка HTML-отчёта
run.ps1                  запуск на Windows одной командой
deploy/install.sh        установка на VPS как systemd-сервис
docs/launch-checklist.md чек-лист запуска, шаблоны текстов
tests/               юнит-тесты, тест паритета Python и JS, тест базы
```

## Запуск бота

Windows, одной командой (создаст окружение, поставит зависимости, соберёт сайт, запустит бота):

```bash
powershell -ExecutionPolicy Bypass -File run.ps1
```

Перед этим заполните `.env` (файл уже создан из `.env.example`): `BOT_TOKEN` от @BotFather, `ADMIN_IDS` (ваш Telegram id, узнать у @userinfobot), `BOT_USERNAME`, `VIN_PRICE_STARS` (цена отчёта в Stars, 0 = оплата вне бота).

Вручную:

```bash
pip install -r requirements.txt
python scripts/build_web.py
python -m bot.main
```

VPS (Ubuntu): `sudo bash deploy/install.sh`. Docker: `docker compose up -d`.

## Веб-калькулятор

Опубликованная копия (приватная, HTTPS, подходит как URL для Mini App после открытия доступа): https://claude.ai/artifact/XxxCXRABsy83WNQeNMMbcT

```bash
python scripts/build_web.py
python -m http.server 8080 --directory web
```

Откройте http://localhost:8080. Username бота берётся из `.env` → `BOT_USERNAME`.

Продакшен: при пуше в GitHub папка `web/` автоматически выкладывается на GitHub Pages (workflow в `.github/workflows/pages.yml`). В настройках репозитория: Settings → Pages → Source: GitHub Actions; Settings → Variables → `BOT_USERNAME`. Чтобы сделать Mini App: @BotFather → Bot Settings → Menu Button → URL страницы.

## Тесты

```bash
python -m pytest -q
```

Тест паритета запускает `web/calc.js` через Node и сравнивает результат с Python до рубля.

## Обновление ставок

1. Поправьте числа в `data/rules.json`, обновите `version` и ссылку в `source`.
2. Запустите тесты. Если менялись льготные пороги или сетка, поправьте ожидания в `tests/test_engine.py`.
3. Запустите `python scripts/export_rules.py` и перевыложите `web/`.
4. Перезапустите бота.

## Отчёт по VIN вручную

1. Заказ приходит админу с VIN и контактом.
2. Купите китайский отчёт (Taobao: «车辆历史报告», или через агента), сохраните текст в файл.
3. `python scripts/vin_report.py <VIN> report.txt` → `reports/<VIN>.html` (нужен `ANTHROPIC_API_KEY` в `.env`). Проверьте глазами.
4. Прикрепите файл в чат с ботом с подписью `/send <номер заказа>`. Бот отправит клиенту и закроет заказ.

## Что требует проверки перед запуском на людей

- Сетка коммерческого утильсбора (мощность выше 160 л.с.) взята из вторичных источников. Сверьте с Постановлением Правительства РФ № 1291 в редакции от 06.02.2026. Ячейки `null` в `rules.json` не подтверждены, калькулятор честно пишет «не подтверждено».
- Диапазоны расходов на логистику, СБКТС, брокера и расходы в Китае собраны по публикациям осени 2026. Уточните у двух-трёх реальных агентов и поправьте `low/mid/high`.
- Таможенный сбор за оформление для физлиц по некоторым источникам фиксированный, по другим зависит от стоимости. Сумма небольшая, но стоит уточнить у брокера.
- Запасной курс в `fallback_rates` на 03.10.2026. Бот тянет курс ЦБ онлайн, запасной нужен только если сеть недоступна.

## Деплой

Полная инструкция для своего сервера и Gitea: [docs/publish.md](docs/publish.md). Коротко: `deploy/install.sh` ставит бота как сервис, `deploy/nginx-web.conf` раздаёт сайт, `deploy/deploy.sh` обновляет всё одной командой.

Любой VPS с Python 3.11+. Пример systemd-юнита:

```
[Unit]
Description=car-import-bot
After=network.target

[Service]
WorkingDirectory=/opt/car
EnvironmentFile=/opt/car/.env
ExecStart=/opt/car/.venv/bin/python -m bot.main
Restart=always

[Install]
WantedBy=multi-user.target
```

## Дальше (после проверки спроса)

- Автоматизация отчёта по VIN через партнёра в Китае.
- Кабинет агента: приём заявок, статусы, рейтинг.
- Парсинг объявлений с che168 и dongchedi с расчётом цены в России.
