"""Проверка связи с rasp.spbgasu.ru без бота: python test_site.py [ГРУППА]"""
import asyncio
import sys
from datetime import date

import schedule_source as src


async def main(group: str):
    html = await src._fetch_html(group)
    print("Ответ получен, размер HTML:", len(html), "символов")
    lessons = src.parse_lessons(html)
    print("Разобрано пар за семестр:", len(lessons))
    today = date.today().isoformat()
    for l in sorted((x for x in lessons if x["date"] == today), key=lambda x: x["start"]):
        print(l)


asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "ПИб-2"))