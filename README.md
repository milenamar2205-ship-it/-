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

# Бот расписания СПбГАСУ

Расписание группы, календарь, задачи и заметки к парам. Управление кнопками.

## Запуск (Windows, PowerShell, из папки бота)
```
py -m venv venv
venv\Scripts\activate
python -m pip install -r requirements.txt
$env:BOT_TOKEN="токен_от_BotFather"
$env:TG_PROXY="socks5://127.0.0.1:40000"   # только если Telegram недоступен напрямую
python bot.py
```

## Файлы
- `bot.py` — бот (интерфейс, база, напоминания, отслеживание изменений)
- `schedule_source.py` — получение и разбор расписания с rasp.spbgasu.ru
- `test_site.py` — проверка связи с сайтом без бота: `python test_site.py ПИб-2`

## Настройки через переменные окружения
- `BOT_TOKEN` — токен бота (обязательно)
- `TG_PROXY` — SOCKS5/HTTP прокси для Telegram (по желанию)
- `BOT_TZ` — часовой пояс, по умолчанию `Europe/Moscow`

Данные (группа, задачи, заметки) хранятся в файле `bot.db` рядом с ботом.