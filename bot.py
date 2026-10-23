import asyncio
import html
import json
import logging
import os
import re
from datetime import date, timedelta

import aiosqlite
from aiogram import Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import schedule_source as src

TOKEN = os.environ["BOT_TOKEN"]  # токен от @BotFather
DB = "bot.db"
CHECK_EVERY_MIN = 10

logging.basicConfig(level=logging.INFO)
router = Router()

DAYS = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]


# ---------- БД ----------
async def init_db():
    async with aiosqlite.connect(DB) as db:
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS users(
                chat_id INTEGER PRIMARY KEY, group_id TEXT, group_name TEXT);
            CREATE TABLE IF NOT EXISTS tasks(
                id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER,
                day TEXT, text TEXT, done INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS snapshots(
                group_id TEXT, week TEXT, data TEXT, PRIMARY KEY(group_id, week));
            """
        )
        await db.commit()


async def get_user(chat_id: int):
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT group_id, group_name FROM users WHERE chat_id=?", (chat_id,))
        return await cur.fetchone()


# ---------- Вспомогательное ----------
def monday_of(d: date) -> date:
    return d - timedelta(days=d.weekday())


def esc(s) -> str:
    return html.escape(str(s or ""))


async def day_text(chat_id: int, group_id: str, d: date) -> str:
    lessons = [l for l in await src.fetch_lessons(group_id, monday_of(d)) if l["date"] == d.isoformat()]
    lessons.sort(key=lambda l: l["start"])
    out = [f"<b>{DAYS[d.weekday()]}, {d:%d.%m}</b>"]
    if lessons:
        for l in lessons:
            kind = f" ({esc(l['kind'])})" if l.get("kind") else ""
            out.append(
                f"🕘 {l['start']}–{l['end']}  <b>{esc(l['subject'])}</b>{kind}\n"
                f"     🚪 {esc(l['room'])}  👤 {esc(l['teacher'])}"
            )
    else:
        out.append("Пар нет 🎉")

    async with aiosqlite.connect(DB) as db:
        cur = await db.execute(
            "SELECT id, text, done FROM tasks WHERE chat_id=? AND day=? ORDER BY id",
            (chat_id, d.isoformat()),
        )
        tasks = await cur.fetchall()
    if tasks:
        out.append("\n📝 <b>Мои задачи</b>")
        for tid, text, done in tasks:
            out.append(f"{'✅' if done else '⬜'} {esc(text)}  <i>(#{tid})</i>")
    return "\n".join(out)


async def need_group(m: Message):
    u = await get_user(m.chat.id)
    if not u or not u[0]:
        await m.answer("Сначала выберите группу: /group НАЗВАНИЕ")
        return None
    return u


# ---------- Команды ----------
@router.message(Command("start", "help"))
async def start(m: Message):
    await m.answer(
        "Привет! Я показываю расписание СПбГАСУ и сообщаю, если оно меняется.\n\n"
        "/group НАЗВАНИЕ — выбрать группу\n"
        "/today, /tomorrow, /week — расписание\n"
        "/add [дд.мм] текст — добавить задачу (без даты — на сегодня)\n"
        "/tasks — все невыполненные задачи\n"
        "/done N — отметить задачу выполненной\n"
        "/del N — удалить задачу"
    )


@router.message(Command("group"))
async def set_group(m: Message, command: CommandObject):
    if not command.args:
        return await m.answer("Напишите так: /group НАЗВАНИЕ_ГРУППЫ")
    try:
        found = await src.find_group(command.args.strip())
    except NotImplementedError:
        return await m.answer("Источник расписания ещё не подключён (см. README).")
    if not found:
        return await m.answer("Группу не нашла 😕 Проверьте название.")
    gid, name = found
    async with aiosqlite.connect(DB) as db:
        await db.execute(
            "INSERT INTO users(chat_id, group_id, group_name) VALUES(?,?,?) "
            "ON CONFLICT(chat_id) DO UPDATE SET group_id=excluded.group_id, group_name=excluded.group_name",
            (m.chat.id, gid, name),
        )
        await db.commit()
    await m.answer(f"Готово, группа: <b>{esc(name)}</b>. Об изменениях в расписании сообщу сама.")


@router.message(Command("today"))
async def today(m: Message):
    if u := await need_group(m):
        await m.answer(await day_text(m.chat.id, u[0], date.today()))


@router.message(Command("tomorrow"))
async def tomorrow(m: Message):
    if u := await need_group(m):
        await m.answer(await day_text(m.chat.id, u[0], date.today() + timedelta(days=1)))


@router.message(Command("week"))
async def week(m: Message):
    if u := await need_group(m):
        mon = monday_of(date.today())
        for i in range(6):
            await m.answer(await day_text(m.chat.id, u[0], mon + timedelta(days=i)))


@router.message(Command("add"))
async def add_task(m: Message, command: CommandObject):
    if not command.args:
        return await m.answer("Пример: /add 25.10 сдать лабу  или  /add купить тубус")
    text, day = command.args.strip(), date.today()
    mt = re.match(r"^(\d{1,2})\.(\d{1,2})\s+(.+)$", text)
    if mt:
        dd, mm, text = int(mt[1]), int(mt[2]), mt[3]
        try:
            day = date(day.year, mm, dd)
        except ValueError:
            return await m.answer("Не поняла дату, формат дд.мм")
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute(
            "INSERT INTO tasks(chat_id, day, text) VALUES(?,?,?)", (m.chat.id, day.isoformat(), text)
        )
        await db.commit()
    await m.answer(f"Добавила на {day:%d.%m}: {esc(text)} (#{cur.lastrowid})")


@router.message(Command("tasks"))
async def list_tasks(m: Message):
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute(
            "SELECT id, day, text FROM tasks WHERE chat_id=? AND done=0 ORDER BY day, id", (m.chat.id,)
        )
        rows = await cur.fetchall()
    if not rows:
        return await m.answer("Задач нет 👌")
    await m.answer("\n".join(f"⬜ {date.fromisoformat(d):%d.%m} — {esc(t)} (#{i})" for i, d, t in rows))


async def _task_action(m: Message, command: CommandObject, sql: str, ok: str):
    if not command.args or not command.args.strip().lstrip("#").isdigit():
        return await m.answer("Укажите номер задачи, например: /done 3")
    tid = int(command.args.strip().lstrip("#"))
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute(sql, (tid, m.chat.id))
        await db.commit()
    await m.answer(ok if cur.rowcount else "Такой задачи нет.")


@router.message(Command("done"))
async def done(m: Message, command: CommandObject):
    await _task_action(m, command, "UPDATE tasks SET done=1 WHERE id=? AND chat_id=?", "Отмечено ✅")


@router.message(Command("del"))
async def delete(m: Message, command: CommandObject):
    await _task_action(m, command, "DELETE FROM tasks WHERE id=? AND chat_id=?", "Удалено 🗑")


# ---------- Отслеживание изменений ----------
def lesson_line(l: dict) -> str:
    return f"{l['date'][8:]}.{l['date'][5:7]} {l['start']} {esc(l['subject'])}"


def diff(old: list[dict], new: list[dict]) -> list[str]:
    key = lambda l: (l["date"], l["start"])
    o, n = {key(l): l for l in old}, {key(l): l for l in new}
    msgs = []
    for k, nl in n.items():
        if k not in o:
            msgs.append(f"➕ Новая пара: {lesson_line(nl)}, ауд. {esc(nl['room'])}")
            continue
        ol, ch = o[k], []
        if ol["room"] != nl["room"]:
            ch.append(f"аудитория {esc(ol['room'])} → <b>{esc(nl['room'])}</b>")
        if ol["subject"] != nl["subject"]:
            ch.append(f"предмет {esc(ol['subject'])} → <b>{esc(nl['subject'])}</b>")
        if ol["teacher"] != nl["teacher"]:
            ch.append(f"преподаватель {esc(ol['teacher'])} → <b>{esc(nl['teacher'])}</b>")
        if ol["end"] != nl["end"]:
            ch.append(f"конец {ol['end']} → <b>{nl['end']}</b>")
        if ch:
            msgs.append(f"✏️ {lesson_line(nl)}: " + "; ".join(ch))
    for k, ol in o.items():
        if k not in n:
            msgs.append(f"❌ Пару отменили: {lesson_line(ol)}")
    return msgs


async def check_changes(bot: Bot):
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT DISTINCT group_id FROM users WHERE group_id IS NOT NULL")
        groups = [r[0] for r in await cur.fetchall()]

    mon = monday_of(date.today())
    for gid in groups:
        for week_start in (mon, mon + timedelta(days=7)):
            try:
                new = await src.fetch_lessons(gid, week_start)
            except Exception:
                logging.exception("fetch failed for %s", gid)
                continue
            async with aiosqlite.connect(DB) as db:
                cur = await db.execute(
                    "SELECT data FROM snapshots WHERE group_id=? AND week=?", (gid, week_start.isoformat())
                )
                row = await cur.fetchone()
                await db.execute(
                    "INSERT OR REPLACE INTO snapshots VALUES(?,?,?)",
                    (gid, week_start.isoformat(), json.dumps(new, ensure_ascii=False)),
                )
                await db.commit()
                cur = await db.execute("SELECT chat_id FROM users WHERE group_id=?", (gid,))
                chats = [r[0] for r in await cur.fetchall()]
            if row is None:  # первый запуск — просто запоминаем
                continue
            changes = diff(json.loads(row[0]), new)
            if changes:
                text = "🔔 <b>Расписание изменилось</b>\n\n" + "\n".join(changes)
                for chat in chats:
                    try:
                        await bot.send_message(chat, text)
                    except Exception:
                        logging.exception("send failed to %s", chat)


# ---------- Запуск ----------
async def main():
    await init_db()
    bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    sched = AsyncIOScheduler()
    sched.add_job(check_changes, "interval", minutes=CHECK_EVERY_MIN, args=[bot])
    sched.start()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
