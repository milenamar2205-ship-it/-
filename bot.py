"""
Телеграм-бот: расписание СПбГАСУ + задачи + заметки к парам + календарь.
Весь интерфейс на кнопках. Запуск: python bot.py (нужны BOT_TOKEN и, при необходимости, TG_PROXY).
"""
import asyncio
import calendar
import html
import json
import logging
import os
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, ExceptionTypeFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BotCommand, CallbackQuery, ErrorEvent, InlineKeyboardButton,
    InlineKeyboardMarkup, KeyboardButton, Message, ReplyKeyboardMarkup,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import schedule_source as src



def _load_env(path: str):
    """Читает файл .env рядом с bot.py (строки вида KEY=VALUE). Переменные терминала приоритетнее."""
    try:
        with open(path, encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip().removeprefix("export ").strip()
                os.environ.setdefault(key, value.strip().strip('"').strip("'"))
    except FileNotFoundError:
        pass


_load_env(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

TOKEN = os.environ.get("BOT_TOKEN")
if not TOKEN:
    raise SystemExit(
        "Не найден BOT_TOKEN. Создайте файл .env рядом с bot.py со строкой BOT_TOKEN=ваш_токен "
        "или задайте переменную окружения."
    )
DB = "bot.db"
TZ = ZoneInfo(os.environ.get("BOT_TZ", "Europe/Moscow"))
CHECK_EVERY_MIN = 10

logging.basicConfig(level=logging.INFO)
router = Router()

WEEKDAYS = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
WD_SHORT = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
MONTHS = ["Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
          "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"]
MONTHS_GEN = ["января", "февраля", "марта", "апреля", "мая", "июня",
              "июля", "августа", "сентября", "октября", "ноября", "декабря"]
PAIR_BY_START = {"09:00": 1, "10:45": 2, "12:30": 3, "15:00": 4, "16:45": 5, "18:30": 6, "20:15": 7}
QUICK_TAGS = ["Контрольная", "Зачёт", "Сдать работу", "Лабораторная", "Важно"]
DIGEST_OPTS = ["off", "0700", "0730", "0800", "0830"]
REMIND_OPTS = [0, 15, 30, 60]

REPLY_KB = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="Меню")]], resize_keyboard=True)


# ====================== Утилиты ======================
def now() -> datetime:
    return datetime.now(TZ)


def today() -> date:
    return now().date()


def esc(s) -> str:
    return html.escape(str(s or ""))


def monday_of(d: date) -> date:
    return d - timedelta(days=d.weekday())


def d_long(d: date) -> str:
    return f"{WEEKDAYS[d.weekday()]}, {d.day} {MONTHS_GEN[d.month - 1]}"


def btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def kb(rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


def fit(lines, limit=3900) -> str:
    """Склеивает строки, не превышая лимит Telegram (каждая строка самодостаточна по HTML-тегам)."""
    out, size = [], 0
    for ln in lines:
        if size + len(ln) + 1 > limit:
            out.append("…")
            break
        out.append(ln)
        size += len(ln) + 1
    return "\n".join(out).strip()


def pairs_word(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} пара"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return f"{n} пары"
    return f"{n} пар"


def rel_label(d: date) -> str:
    return {0: "сегодня", 1: "завтра", -1: "вчера"}.get((d - today()).days, "")


def task_label(done, due, text, day: date | None = None) -> str:
    s = "[x] " if done else "[ ] "
    if day:
        s += f"{day:%d.%m} "
    if due:
        s += f"{due} "
    s += text
    return s if len(s) <= 34 else s[:33] + "…"


# ====================== База данных ======================
async def _ensure_columns(db, table: str, cols: dict):
    cur = await db.execute(f"PRAGMA table_info({table})")
    have = {r[1] for r in await cur.fetchall()}
    for name, ddl in cols.items():
        if name not in have:
            await db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


async def init_db():
    async with aiosqlite.connect(DB) as db:
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS users(
                chat_id INTEGER PRIMARY KEY, group_id TEXT, group_name TEXT);
            CREATE TABLE IF NOT EXISTS tasks(
                id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER,
                day TEXT, text TEXT, done INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS notes(
                id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER,
                day TEXT, start TEXT, text TEXT);
            CREATE TABLE IF NOT EXISTS snapshots(
                group_id TEXT, week TEXT, data TEXT, PRIMARY KEY(group_id, week));
            """
        )
        # миграция со старой версии бота
        await _ensure_columns(db, "users", {
            "digest_time": "TEXT", "remind_min": "INTEGER DEFAULT 30",
            "alerts": "INTEGER DEFAULT 1", "last_digest": "TEXT"})
        await _ensure_columns(db, "tasks", {"due_time": "TEXT", "reminded": "INTEGER DEFAULT 0"})
        await db.execute("CREATE INDEX IF NOT EXISTS idx_tasks_day ON tasks(chat_id, day)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_notes_day ON notes(chat_id, day)")
        await db.commit()


async def q_all(sql, args=()):
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute(sql, args)
        return await cur.fetchall()


async def q_one(sql, args=()):
    rows = await q_all(sql, args)
    return rows[0] if rows else None


async def q_exec(sql, args=()):
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute(sql, args)
        await db.commit()
        return cur.lastrowid


async def get_user(chat_id: int) -> dict | None:
    r = await q_one(
        "SELECT group_id, group_name, digest_time, remind_min, alerts FROM users WHERE chat_id=?",
        (chat_id,))
    if not r:
        return None
    return {"group_id": r[0], "group_name": r[1], "digest_time": r[2],
            "remind_min": 30 if r[3] is None else r[3], "alerts": 1 if r[4] is None else r[4]}


# ====================== Расписание ======================
async def get_week(user: dict, monday: date):
    """Возвращает (пары недели, статус): ok / stale (сайт недоступен, взяли сохранённое) / fail."""
    try:
        return await src.fetch_lessons(user["group_id"], monday), "ok"
    except Exception:
        logging.exception("Не удалось получить расписание")
    row = await q_one("SELECT data FROM snapshots WHERE group_id=? AND week=?",
                      (user["group_id"], monday.isoformat()))
    if row:
        return json.loads(row[0]), "stale"
    return [], "fail"


async def day_lessons(chat_id: int, d: date):
    user = await get_user(chat_id)
    if not user or not user["group_id"]:
        return [], "nogroup"
    week, status = await get_week(user, monday_of(d))
    lessons = sorted((l for l in week if l["date"] == d.isoformat()), key=lambda l: l["start"])
    return lessons, status


def lesson_lines(l: dict, notes: list[str]) -> list[str]:
    pair = l.get("pair") or PAIR_BY_START.get(l["start"])
    head = f"<b>{pair} пара</b>  {l['start']}–{l['end']}" if pair else f"<b>{l['start']}–{l['end']}</b>"
    kind = f" ({esc(l['kind'])})" if l.get("kind") else ""
    out = [head, f"{esc(l['subject'])}{kind}"]
    where = " · ".join(x for x in (f"ауд. {esc(l['room'])}" if l.get("room") else "",
                                    esc(l.get("teacher"))) if x)
    if where:
        out.append(where)
    for n in notes:
        out.append(f"<b>Заметка:</b> {esc(n)}")
    return out


# ====================== Экраны ======================
async def build_menu(chat_id: int):
    user = await get_user(chat_id)
    lines = ["<b>Расписание СПбГАСУ</b>"]
    t = today()
    if user and user["group_id"]:
        lines.append(f"Группа: {esc(user['group_name'])}")
        lines.append("")
        lessons, status = await day_lessons(chat_id, t)
        if lessons:
            lines.append(f"Сегодня: {pairs_word(len(lessons))}, с {lessons[0]['start']} до {lessons[-1]['end']}")
        elif status in ("ok", "stale"):
            lines.append("Сегодня пар нет")
        left = (await q_one("SELECT COUNT(*) FROM tasks WHERE chat_id=? AND done=0 AND day=?",
                            (chat_id, t.isoformat())))[0]
        late = (await q_one("SELECT COUNT(*) FROM tasks WHERE chat_id=? AND done=0 AND day<?",
                            (chat_id, t.isoformat())))[0]
        lines.append(f"Задач на сегодня: {left}")
        if late:
            lines.append(f"Просроченных задач: {late}")
    else:
        lines.append("Группа не выбрана. Откройте «Настройки» и укажите группу.")
    rows = [
        [btn("Сегодня", f"d:{t.isoformat()}"), btn("Завтра", f"d:{(t + timedelta(days=1)).isoformat()}")],
        [btn("Неделя", f"w:{monday_of(t).isoformat()}"), btn("Календарь", f"c:{t:%Y-%m}")],
        [btn("Задачи", "t"), btn("Настройки", "s")],
    ]
    return fit(lines), kb(rows)


async def build_day(chat_id: int, d: date):
    iso = d.isoformat()
    rel = rel_label(d)
    lines = [f"<b>{d_long(d)}</b>" + (f"  ({rel})" if rel else ""), ""]
    lessons, status = await day_lessons(chat_id, d)
    if status == "nogroup":
        lines += ["Группа не выбрана. Откройте «Настройки» и укажите группу.", ""]
    elif status == "stale":
        lines += ["<i>Сайт расписания недоступен, показаны сохранённые данные.</i>", ""]
    elif status == "fail":
        lines += ["Не удалось получить расписание с сайта. Попробуйте позже.", ""]

    note_rows = await q_all("SELECT start, text FROM notes WHERE chat_id=? AND day=? ORDER BY id", (chat_id, iso))
    notes: dict[str, list[str]] = {}
    for s, t in note_rows:
        notes.setdefault(s, []).append(t)

    if lessons:
        for l in lessons:
            lines += lesson_lines(l, notes.get(l["start"], [])) + [""]
    elif status in ("ok", "stale"):
        lines += ["Пар нет", ""]

    starts = {l["start"] for l in lessons}
    orphans = [(s, t) for s, t in note_rows if s not in starts]
    if orphans:
        lines.append("<b>Заметки</b>")
        lines += [f"{s}  {esc(t)}" for s, t in orphans]
        lines.append("")

    tasks = await q_all(
        "SELECT id, due_time, text, done FROM tasks WHERE chat_id=? AND day=? "
        "ORDER BY done, due_time IS NULL, due_time, id", (chat_id, iso))
    if tasks:
        lines.append("<b>Задачи</b>")
        for _id, due, text, done in tasks:
            body = f"<s>{esc(text)}</s>" if done else esc(text)
            lines.append(f"{'[x]' if done else '[ ]'} {due + '  ' if due else ''}{body}")

    rows = [[btn("‹ Пред. день", f"d:{(d - timedelta(days=1)).isoformat()}"),
             btn("Сегодня", f"d:{today().isoformat()}"),
             btn("След. день ›", f"d:{(d + timedelta(days=1)).isoformat()}")]]
    act = [btn("Добавить задачу", f"ta:{iso}")]
    if lessons:
        act.append(btn("Заметка к паре", f"nl:{iso}"))
    rows.append(act)
    for tid, due, text, done in tasks[:12]:
        rows.append([btn(task_label(done, due, text), f"tg:{tid}:d{iso}"), btn("✕", f"tx:{tid}:d{iso}")])
    rows.append([btn("Неделя", f"w:{monday_of(d).isoformat()}"),
                 btn("Календарь", f"c:{d:%Y-%m}"), btn("Меню", "m")])
    return fit(lines), kb(rows)


async def build_week(chat_id: int, mon: date):
    mon = monday_of(mon)
    end = mon + timedelta(days=6)
    lines = [f"<b>Неделя {mon.day} {MONTHS_GEN[mon.month - 1]} – {end.day} {MONTHS_GEN[end.month - 1]}</b>", ""]
    user = await get_user(chat_id)
    week, status = [], "nogroup"
    if user and user["group_id"]:
        week, status = await get_week(user, mon)
    if status == "nogroup":
        lines += ["Группа не выбрана. Откройте «Настройки» и укажите группу.", ""]
    elif status == "stale":
        lines += ["<i>Сайт расписания недоступен, показаны сохранённые данные.</i>", ""]
    elif status == "fail":
        lines += ["Не удалось получить расписание с сайта. Попробуйте позже.", ""]

    a, b = mon.isoformat(), end.isoformat()
    notes = await q_all("SELECT day, start, text FROM notes WHERE chat_id=? AND day BETWEEN ? AND ? ORDER BY id",
                        (chat_id, a, b))
    tasks = await q_all(
        "SELECT day, due_time, text FROM tasks WHERE chat_id=? AND done=0 AND day BETWEEN ? AND ? "
        "ORDER BY due_time IS NULL, due_time, id", (chat_id, a, b))

    for i in range(7):
        d = mon + timedelta(days=i)
        iso = d.isoformat()
        dl = sorted((l for l in week if l["date"] == iso), key=lambda l: l["start"])
        dn = [(s, t) for day, s, t in notes if day == iso]
        dt = [(due, t) for day, due, t in tasks if day == iso]
        if i == 6 and not dl and not dn and not dt:
            continue
        mark = "  (сегодня)" if d == today() else ""
        lines.append(f"<b>{WEEKDAYS[i]}, {d:%d.%m}</b>{mark}")
        if dl:
            for l in dl:
                kind = f" ({esc(l['kind'])})" if l.get("kind") else ""
                room = f", {esc(l['room'])}" if l.get("room") else ""
                lines.append(f"{l['start']}  {esc(l['subject'])}{kind}{room}")
                for s, t in dn:
                    if s == l["start"]:
                        lines.append(f"       <b>Заметка:</b> {esc(t)}")
        elif status in ("ok", "stale"):
            lines.append("Пар нет")
        starts = {l["start"] for l in dl}
        for s, t in dn:
            if s not in starts:
                lines.append(f"<b>Заметка:</b> {s} {esc(t)}")
        for due, t in dt:
            lines.append(f"<b>Задача:</b> {due + ' ' if due else ''}{esc(t)}")
        lines.append("")

    rows = [[btn(f"[{WD_SHORT[i]}]" if (mon + timedelta(days=i)) == today() else WD_SHORT[i],
                 f"d:{(mon + timedelta(days=i)).isoformat()}") for i in range(7)],
            [btn("‹ Пред. неделя", f"w:{(mon - timedelta(days=7)).isoformat()}"),
             btn("Эта неделя", f"w:{monday_of(today()).isoformat()}"),
             btn("След. неделя ›", f"w:{(mon + timedelta(days=7)).isoformat()}")],
            [btn("Календарь", f"c:{mon:%Y-%m}"), btn("Меню", "m")]]
    return fit(lines), kb(rows)


async def build_calendar(chat_id: int, year: int, month: int):
    first = date(year, month, 1)
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    a, b = first.isoformat(), nxt.isoformat()
    tcount = dict(await q_all(
        "SELECT day, COUNT(*) FROM tasks WHERE chat_id=? AND done=0 AND day>=? AND day<? GROUP BY day",
        (chat_id, a, b)))
    ncount = dict(await q_all(
        "SELECT day, COUNT(*) FROM notes WHERE chat_id=? AND day>=? AND day<? GROUP BY day",
        (chat_id, a, b)))
    marked = sorted(set(tcount) | set(ncount))

    lines = [f"<b>{MONTHS[month - 1]} {year}</b>", "", "Точка у числа означает, что в этот день есть задачи или заметки.", ""]
    if marked:
        for iso in marked[:20]:
            d = date.fromisoformat(iso)
            parts = []
            if tcount.get(iso):
                parts.append(f"задач: {tcount[iso]}")
            if ncount.get(iso):
                parts.append(f"заметок: {ncount[iso]}")
            lines.append(f"{d:%d.%m}, {WD_SHORT[d.weekday()]} — {', '.join(parts)}")
    else:
        lines.append("В этом месяце задач и заметок нет.")

    rows = [[btn(w, "x") for w in WD_SHORT]]
    for week in calendar.monthcalendar(year, month):
        row = []
        for day in week:
            if day == 0:
                row.append(btn(" ", "x"))
                continue
            d = date(year, month, day)
            label = str(day) + ("•" if d.isoformat() in marked else "")
            if d == today():
                label = f"[{label}]"
            row.append(btn(label, f"d:{d.isoformat()}"))
        rows.append(row)
    prev = first - timedelta(days=1)
    rows.append([btn("‹", f"c:{prev:%Y-%m}"), btn("Текущий месяц", f"c:{today():%Y-%m}"),
                 btn("›", f"c:{nxt:%Y-%m}")])
    rows.append([btn("Задачи", "t"), btn("Меню", "m")])
    return fit(lines), kb(rows)


async def build_tasks(chat_id: int):
    rows_db = await q_all(
        "SELECT id, day, due_time, text FROM tasks WHERE chat_id=? AND done=0 "
        "ORDER BY day, due_time IS NULL, due_time, id LIMIT 30", (chat_id,))
    done_cnt = (await q_one("SELECT COUNT(*) FROM tasks WHERE chat_id=? AND done=1", (chat_id,)))[0]
    lines = ["<b>Задачи</b>", ""]
    if not rows_db:
        lines.append("Невыполненных задач нет.")
    cur_day = None
    for tid, day, due, text in rows_db:
        d = date.fromisoformat(day)
        if day != cur_day:
            if cur_day is not None:
                lines.append("")
            tag = "  (просрочено)" if d < today() else (f"  ({rel_label(d)})" if rel_label(d) else "")
            lines.append(f"<b>{d_long(d)}</b>{tag}")
            cur_day = day
        lines.append(f"[ ] {due + '  ' if due else ''}{esc(text)}")
    rows = [[btn("Добавить задачу", "ta:-")]]
    for tid, day, due, text in rows_db[:20]:
        rows.append([btn(task_label(False, due, text, date.fromisoformat(day)), f"tg:{tid}:t"),
                     btn("✕", f"tx:{tid}:t")])
    if done_cnt:
        rows.append([btn(f"Выполненные ({done_cnt})", "tc")])
    rows.append([btn("Календарь", f"c:{today():%Y-%m}"), btn("Меню", "m")])
    return fit(lines), kb(rows)


async def build_done(chat_id: int):
    rows_db = await q_all(
        "SELECT id, day, due_time, text FROM tasks WHERE chat_id=? AND done=1 ORDER BY id DESC LIMIT 20",
        (chat_id,))
    lines = ["<b>Выполненные задачи</b>", ""]
    if not rows_db:
        lines.append("Пока пусто.")
    else:
        lines.append("Нажмите на задачу, чтобы вернуть её в список.")
    rows = [[btn(task_label(True, due, text, date.fromisoformat(day)), f"tg:{tid}:tc"), btn("✕", f"tx:{tid}:tc")]
            for tid, day, due, text in rows_db]
    if rows_db:
        rows.append([btn("Очистить выполненные", "tcc")])
    rows.append([btn("‹ К задачам", "t"), btn("Меню", "m")])
    return fit(lines), kb(rows)


async def build_settings(chat_id: int):
    u = await get_user(chat_id) or {"group_name": None, "digest_time": None, "remind_min": 30, "alerts": 1}
    dig = u["digest_time"]
    rem = u["remind_min"]
    lines = [
        "<b>Настройки</b>", "",
        f"Группа: {esc(u['group_name']) if u['group_name'] else 'не выбрана'}",
        f"Утренняя сводка: {dig if dig else 'выключена'}",
        f"Напоминание о задачах со временем: {'за ' + str(rem) + ' мин' if rem else 'выключено'}",
        f"Уведомления об изменениях в расписании: {'включены' if u['alerts'] else 'выключены'}",
    ]
    digest_row = []
    for v in DIGEST_OPTS:
        label = "Выкл" if v == "off" else f"{v[:2]}:{v[2:]}"
        active = (dig is None and v == "off") or (dig is not None and dig.replace(":", "") == v)
        digest_row.append(btn(f"[{label}]" if active else label, f"sd:{v}"))
    rem_row = []
    for v in REMIND_OPTS:
        label = "Выкл" if v == 0 else ("1 час" if v == 60 else f"{v} мин")
        rem_row.append(btn(f"[{label}]" if v == rem else label, f"sr:{v}"))
    rows = [
        [btn("Сменить группу", "sg")],
        [btn("Утренняя сводка", "x")], digest_row,
        [btn("Напоминать о задачах за", "x")], rem_row,
        [btn(f"Уведомления об изменениях: {'вкл' if u['alerts'] else 'выкл'}", "sa")],
        [btn("Меню", "m")],
    ]
    return fit(lines), kb(rows)


async def render_view(chat_id: int, view: str):
    if view.startswith("d"):
        return await build_day(chat_id, date.fromisoformat(view[1:]))
    if view == "t":
        return await build_tasks(chat_id)
    if view == "tc":
        return await build_done(chat_id)
    return await build_menu(chat_id)


async def show(target, text: str, markup):
    """Редактирует сообщение с кнопкой (для CallbackQuery) или отправляет новое."""
    if isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(text, reply_markup=markup)
        except TelegramBadRequest as e:
            if "not modified" not in str(e):
                await target.message.answer(text, reply_markup=markup)
    else:
        await target.answer(text, reply_markup=markup)


async def finish(m: Message, mid, text: str, markup):
    """После ввода текста: убираем сообщение пользователя и обновляем экран-подсказку."""
    try:
        await m.delete()
    except Exception:
        pass
    if mid:
        try:
            await m.bot.edit_message_text(text, chat_id=m.chat.id, message_id=mid, reply_markup=markup)
            return
        except Exception:
            pass
    await m.answer(text, reply_markup=markup)


# ====================== Разбор ввода ======================
def parse_task_input(raw: str, default_day: date):
    """'27.10 сдать лабу 14:00' -> (дата, время|None, текст)."""
    text, day = raw.strip(), default_day
    m = re.match(r"^(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?\s+(.+)$", text, re.S)
    if m:
        dd, mm = int(m[1]), int(m[2])
        yy = int(m[3]) if m[3] else None
        if yy is not None and yy < 100:
            yy += 2000
        try:
            if yy is None:
                day = date(today().year, mm, dd)
                if day < today() - timedelta(days=60):
                    day = date(today().year + 1, mm, dd)
            else:
                day = date(yy, mm, dd)
        except ValueError:
            raise ValueError("Не понял дату. Формат: дд.мм, например 27.10")
        text = m[4].strip()
    due = None
    tm = re.search(r"(?:(?<!\w)(?:в|к|до|на)\s+)?(?<!\d)([01]?\d|2[0-3]):([0-5]\d)(?!\d)", text, re.I)
    if tm:
        due = f"{int(tm[1]):02d}:{tm[2]}"
        text = re.sub(r"\s+", " ", text[:tm.start()] + " " + text[tm.end():]).strip()
    if not text:
        raise ValueError("Напишите текст задачи.")
    return day, due, text[:200]


# ====================== Состояния ввода ======================
class AddTask(StatesGroup):
    text = State()


class AddNote(StatesGroup):
    text = State()


class SetGroup(StatesGroup):
    name = State()


# ====================== Обработчики ======================
@router.error(ExceptionTypeFilter(Exception))
async def on_error(event: ErrorEvent):
    logging.error("Ошибка в обработчике", exc_info=event.exception)
    text = f"Ошибка: {type(event.exception).__name__}: {event.exception}"
    upd = event.update
    try:
        if upd.callback_query:
            await upd.callback_query.answer(text[:190], show_alert=True)
        elif upd.message:
            await upd.message.answer(esc(text)[:500])
    except Exception:
        pass


@router.message(Command("start"))
async def cmd_start(m: Message, state: FSMContext):
    await state.clear()
    await q_exec("INSERT OR IGNORE INTO users(chat_id) VALUES(?)", (m.chat.id,))
    await m.answer("Бот расписания СПбГАСУ. Кнопка «Меню» внизу всегда под рукой.", reply_markup=REPLY_KB)
    user = await get_user(m.chat.id)
    if not user or not user["group_id"]:
        sent = await m.answer("Для начала напишите название вашей группы, например ПИб-2.")
        await state.set_state(SetGroup.name)
        await state.update_data(mid=sent.message_id)
    else:
        text, markup = await build_menu(m.chat.id)
        await m.answer(text, reply_markup=markup)


@router.message(Command("menu"))
async def cmd_menu(m: Message, state: FSMContext):
    await state.clear()
    text, markup = await build_menu(m.chat.id)
    await m.answer(text, reply_markup=markup)


async def _cancel_to_menu(m: Message, state: FSMContext, mid):
    await state.clear()
    text, markup = await build_menu(m.chat.id)
    await finish(m, mid, text, markup)


def _is_cancel(m: Message) -> bool:
    return m.text.strip().lower() in ("меню", "отмена")


@router.message(AddTask.text, F.text)
async def got_task(m: Message, state: FSMContext):
    data = await state.get_data()
    if _is_cancel(m):
        return await _cancel_to_menu(m, state, data.get("mid"))
    try:
        day, due, text = parse_task_input(m.text, date.fromisoformat(data["day"]))
    except ValueError as e:
        await m.answer(str(e))
        return
    await q_exec("INSERT INTO tasks(chat_id, day, due_time, text) VALUES(?,?,?,?)",
                 (m.chat.id, day.isoformat(), due, text))
    await state.clear()
    view = "t" if data.get("back") == "t" else f"d{day.isoformat()}"
    out, markup = await render_view(m.chat.id, view)
    await finish(m, data.get("mid"), out, markup)


@router.message(AddNote.text, F.text)
async def got_note(m: Message, state: FSMContext):
    data = await state.get_data()
    if _is_cancel(m):
        return await _cancel_to_menu(m, state, data.get("mid"))
    await q_exec("INSERT INTO notes(chat_id, day, start, text) VALUES(?,?,?,?)",
                 (m.chat.id, data["day"], data["start"], m.text.strip()[:300]))
    await state.clear()
    out, markup = await build_day(m.chat.id, date.fromisoformat(data["day"]))
    await finish(m, data.get("mid"), out, markup)


@router.message(SetGroup.name, F.text)
async def got_group(m: Message, state: FSMContext):
    data = await state.get_data()
    if _is_cancel(m):
        return await _cancel_to_menu(m, state, data.get("mid"))
    found = await src.find_group(m.text.strip())
    if not found:
        await m.answer("Такую группу не нашла. Проверьте название (например, ПИб-2) и отправьте ещё раз.")
        return
    gid, name = found
    await q_exec("INSERT OR IGNORE INTO users(chat_id) VALUES(?)", (m.chat.id,))
    await q_exec("UPDATE users SET group_id=?, group_name=? WHERE chat_id=?", (gid, name, m.chat.id))
    await state.clear()
    out, markup = await build_menu(m.chat.id)
    await finish(m, data.get("mid"), out, markup)


@router.message(F.text == "Меню")
async def btn_menu(m: Message, state: FSMContext):
    await cmd_menu(m, state)


@router.callback_query()
async def on_cb(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    chat_id = cb.message.chat.id
    p = (cb.data or "").split(":")
    cmd = p[0]
    result = None

    if cmd == "x":
        await cb.answer()
        return
    elif cmd == "m":
        result = await build_menu(chat_id)
    elif cmd == "d":
        result = await build_day(chat_id, date.fromisoformat(p[1]))
    elif cmd == "w":
        result = await build_week(chat_id, date.fromisoformat(p[1]))
    elif cmd == "c":
        y, mo = map(int, p[1].split("-"))
        result = await build_calendar(chat_id, y, mo)
    elif cmd == "t":
        result = await build_tasks(chat_id)
    elif cmd == "tc":
        result = await build_done(chat_id)
    elif cmd == "tcc":
        await q_exec("DELETE FROM tasks WHERE chat_id=? AND done=1", (chat_id,))
        result = await build_tasks(chat_id)
    elif cmd == "tg":
        await q_exec("UPDATE tasks SET done=1-done, reminded=0 WHERE id=? AND chat_id=?", (int(p[1]), chat_id))
        result = await render_view(chat_id, p[2])
    elif cmd == "tx":
        await q_exec("DELETE FROM tasks WHERE id=? AND chat_id=?", (int(p[1]), chat_id))
        result = await render_view(chat_id, p[2])
    elif cmd == "ta":
        from_tasks = p[1] == "-"
        day_iso = today().isoformat() if from_tasks else p[1]
        d = date.fromisoformat(day_iso)
        await state.set_state(AddTask.text)
        await state.update_data(day=day_iso, back="t" if from_tasks else "d", mid=cb.message.message_id)
        text = (f"<b>Новая задача на {d.day} {MONTHS_GEN[d.month - 1]}</b>\n\n"
                "Напишите текст задачи сообщением.\n"
                "Время добавьте в конце, и я напомню: «сдать лабу 14:00».\n"
                "Другая дата: начните с неё, например «27.10 купить билеты».")
        result = (text, kb([[btn("Отмена", "t" if from_tasks else f"d:{day_iso}")]]))
    elif cmd == "nl":
        iso = p[1]
        lessons, _ = await day_lessons(chat_id, date.fromisoformat(iso))
        rows = []
        for l in lessons:
            pair = l.get("pair") or PAIR_BY_START.get(l["start"])
            label = f"{pair} пара  {l['start']}  {l['subject']}" if pair else f"{l['start']}  {l['subject']}"
            rows.append([btn(label[:40], f"ns:{iso}:{l['start'].replace(':', '')}")])
        rows.append([btn("‹ Назад", f"d:{iso}")])
        result = ("<b>К какой паре добавить заметку?</b>", kb(rows))
    elif cmd == "ns":
        iso, hhmm = p[1], p[2]
        start = f"{hhmm[:2]}:{hhmm[2:]}"
        lessons, _ = await day_lessons(chat_id, date.fromisoformat(iso))
        lesson = next((l for l in lessons if l["start"] == start), None)
        old = await q_all("SELECT text FROM notes WHERE chat_id=? AND day=? AND start=? ORDER BY id",
                          (chat_id, iso, start))
        await state.set_state(AddNote.text)
        await state.update_data(day=iso, start=start, mid=cb.message.message_id)
        lines = ["<b>Заметка к паре</b>"]
        if lesson:
            lines.append(f"{start}  {esc(lesson['subject'])}")
        for (t,) in old:
            lines.append(f"Уже есть: {esc(t)}")
        lines += ["", "Выберите готовый вариант или напишите свою заметку сообщением."]
        rows = []
        for i in range(0, len(QUICK_TAGS), 2):
            rows.append([btn(t, f"nq:{iso}:{hhmm}:{QUICK_TAGS.index(t)}") for t in QUICK_TAGS[i:i + 2]])
        if old:
            rows.append([btn("Удалить заметки этой пары", f"nc:{iso}:{hhmm}")])
        rows.append([btn("‹ Назад", f"d:{iso}")])
        result = (fit(lines), kb(rows))
    elif cmd == "nq":
        iso, hhmm, idx = p[1], p[2], int(p[3])
        await q_exec("INSERT INTO notes(chat_id, day, start, text) VALUES(?,?,?,?)",
                     (chat_id, iso, f"{hhmm[:2]}:{hhmm[2:]}", QUICK_TAGS[idx]))
        result = await build_day(chat_id, date.fromisoformat(iso))
    elif cmd == "nc":
        iso, hhmm = p[1], p[2]
        await q_exec("DELETE FROM notes WHERE chat_id=? AND day=? AND start=?",
                     (chat_id, iso, f"{hhmm[:2]}:{hhmm[2:]}"))
        result = await build_day(chat_id, date.fromisoformat(iso))
    elif cmd == "s":
        result = await build_settings(chat_id)
    elif cmd == "sd":
        val = None if p[1] == "off" else f"{p[1][:2]}:{p[1][2:]}"
        await q_exec("UPDATE users SET digest_time=?, last_digest=NULL WHERE chat_id=?", (val, chat_id))
        result = await build_settings(chat_id)
    elif cmd == "sr":
        await q_exec("UPDATE users SET remind_min=? WHERE chat_id=?", (int(p[1]), chat_id))
        result = await build_settings(chat_id)
    elif cmd == "sa":
        await q_exec("UPDATE users SET alerts=1-COALESCE(alerts,1) WHERE chat_id=?", (chat_id,))
        result = await build_settings(chat_id)
    elif cmd == "sg":
        await state.set_state(SetGroup.name)
        await state.update_data(mid=cb.message.message_id)
        result = ("Напишите название группы сообщением, например ПИб-2.", kb([[btn("Отмена", "s")]]))

    if result:
        await show(cb, *result)
    await cb.answer()


# ====================== Фоновые задачи ======================
def by_slot(lessons):
    out = {}
    for l in lessons:
        out.setdefault((l["date"], l["start"]), []).append(l)
    return out


def _sig(l):
    return (l["subject"], l.get("kind", ""), l.get("room", ""), l.get("teacher", ""), l["end"])


def diff(old: list[dict], new: list[dict], since: str) -> list[str]:
    """Сравнивает расписание; учитываются только пары начиная с даты since (ISO)."""
    o = by_slot([l for l in old if l["date"] >= since])
    n = by_slot([l for l in new if l["date"] >= since])
    out = []
    for k in sorted(set(o) | set(n)):
        ol, nl = o.get(k, []), n.get(k, [])
        if sorted(map(_sig, ol)) == sorted(map(_sig, nl)):
            continue
        date_s = f"{k[0][8:]}.{k[0][5:7]} {k[1]}"
        if not ol:
            for l in nl:
                out.append(f"Новая пара: {date_s} {esc(l['subject'])}, ауд. {esc(l['room'])}")
        elif not nl:
            for l in ol:
                out.append(f"Пару отменили: {date_s} {esc(l['subject'])}")
        elif len(ol) == 1 and len(nl) == 1:
            a, b = ol[0], nl[0]
            ch = []
            if a["room"] != b["room"]:
                ch.append(f"аудитория {esc(a['room'])} → <b>{esc(b['room'])}</b>")
            if a["subject"] != b["subject"]:
                ch.append(f"предмет {esc(a['subject'])} → <b>{esc(b['subject'])}</b>")
            if a.get("teacher") != b.get("teacher"):
                ch.append(f"преподаватель {esc(a.get('teacher'))} → <b>{esc(b.get('teacher'))}</b>")
            if a["end"] != b["end"]:
                ch.append(f"конец {a['end']} → <b>{b['end']}</b>")
            if a.get("kind") != b.get("kind"):
                ch.append(f"тип {esc(a.get('kind'))} → <b>{esc(b.get('kind'))}</b>")
            out.append(f"Изменение: {date_s} {esc(b['subject'])}: " + "; ".join(ch))
        else:
            out.append(f"Изменение: {date_s} — несколько пар в одно время, проверьте расписание")
    return out


async def check_changes(bot: Bot):
    groups = [r[0] for r in await q_all(
        "SELECT DISTINCT group_id FROM users WHERE group_id IS NOT NULL AND COALESCE(alerts,1)=1")]
    mon = monday_of(today())
    for gid in groups:
        for ws in (mon, mon + timedelta(days=7)):
            try:
                new = await src.fetch_lessons(gid, ws)
            except Exception:
                logging.exception("fetch failed for %s", gid)
                continue
            row = await q_one("SELECT data FROM snapshots WHERE group_id=? AND week=?", (gid, ws.isoformat()))
            await q_exec("INSERT OR REPLACE INTO snapshots VALUES(?,?,?)",
                         (gid, ws.isoformat(), json.dumps(new, ensure_ascii=False)))
            if row is None:
                continue  # первый запуск: просто запоминаем
            changes = diff(json.loads(row[0]), new, today().isoformat())
            if not changes:
                continue
            chats = [r[0] for r in await q_all(
                "SELECT chat_id FROM users WHERE group_id=? AND COALESCE(alerts,1)=1", (gid,))]
            text = "<b>Изменения в расписании</b>\n\n" + "\n".join(changes)
            markup = kb([[btn("Открыть неделю", f"w:{ws.isoformat()}"), btn("Меню", "m")]])
            for chat in chats:
                try:
                    await bot.send_message(chat, fit(text.split("\n")), reply_markup=markup)
                except Exception:
                    logging.exception("send failed to %s", chat)


async def minute_tick(bot: Bot):
    n = now()
    today_iso = n.date().isoformat()

    # Утренняя сводка
    rows = await q_all(
        "SELECT chat_id, digest_time FROM users WHERE digest_time IS NOT NULL AND group_id IS NOT NULL "
        "AND (last_digest IS NULL OR last_digest != ?)", (today_iso,))
    for chat_id, t in rows:
        h, mi = map(int, t.split(":"))
        delta = n.hour * 60 + n.minute - (h * 60 + mi)
        if 0 <= delta <= 180:
            await q_exec("UPDATE users SET last_digest=? WHERE chat_id=?", (today_iso, chat_id))
            try:
                text, markup = await build_day(chat_id, n.date())
                await bot.send_message(chat_id, "<b>Сводка на сегодня</b>\n\n" + text, reply_markup=markup)
            except Exception:
                logging.exception("digest failed for %s", chat_id)

    # Напоминания о задачах со временем
    rows = await q_all(
        "SELECT t.id, t.chat_id, t.due_time, t.text, u.remind_min FROM tasks t "
        "JOIN users u ON u.chat_id=t.chat_id "
        "WHERE t.done=0 AND COALESCE(t.reminded,0)=0 AND t.due_time IS NOT NULL AND t.day=? "
        "AND COALESCE(u.remind_min,30)>0", (today_iso,))
    for tid, chat_id, due, text, rem in rows:
        h, mi = map(int, due.split(":"))
        due_dt = n.replace(hour=h, minute=mi, second=0, microsecond=0)
        if due_dt - timedelta(minutes=rem) <= n <= due_dt + timedelta(minutes=30):
            await q_exec("UPDATE tasks SET reminded=1 WHERE id=?", (tid,))
            try:
                await bot.send_message(
                    chat_id, f"<b>Напоминание</b>\n{due}  {esc(text)}",
                    reply_markup=kb([[btn("Готово", f"tg:{tid}:d{today_iso}"),
                                      btn("Открыть день", f"d:{today_iso}")]]))
            except Exception:
                logging.exception("reminder failed for %s", chat_id)


# ====================== Запуск ======================
async def main():
    await init_db()
    # Если Telegram недоступен напрямую, задайте прокси: TG_PROXY=socks5://host:port
    session = AiohttpSession(proxy=os.environ.get("TG_PROXY") or None)
    bot = Bot(TOKEN, session=session, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    await bot.set_my_commands([BotCommand(command="menu", description="Открыть меню")])
    sched = AsyncIOScheduler(timezone=TZ)
    sched.add_job(check_changes, "interval", minutes=CHECK_EVERY_MIN, args=[bot])
    sched.add_job(minute_tick, "interval", minutes=1, args=[bot])
    sched.start()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())