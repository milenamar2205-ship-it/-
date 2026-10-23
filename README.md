# Бот расписания СПбГАСУ

## Запуск
1. Создайте бота у @BotFather, получите токен.
2. `pip install -r requirements.txt`
3. `export BOT_TOKEN="ваш_токен"` (Windows PowerShell: `$env:BOT_TOKEN="ваш_токен"`)
4. `python bot.py`

## Как подключить сайт (schedule_source.py)
1. Откройте https://rasp.spbgasu.ru в Chrome, нажмите F12 → вкладка **Network** → фильтр **Fetch/XHR**.
2. Выберите группу и неделю — в списке появятся запросы за расписанием.
3. Кликните на запрос → **Headers** (URL и параметры) и **Response** (формат данных).
4. Реализуйте `find_group` и `fetch_lessons` в `schedule_source.py`, приведя ответ к формату из комментария.

Если запросов нет, а расписание вставляется прямо в страницу, используйте Playwright
(`pip install playwright && playwright install chromium`) и разберите HTML.
