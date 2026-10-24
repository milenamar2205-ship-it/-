"""
Источник расписания: rasp.spbgasu.ru.

Сайт отдаёт расписание ВСЕГО семестра одним POST-запросом (JSON, внутри поле
data.html с готовой разметкой). Здесь мы запрашиваем его, разбираем HTML и
возвращаем уроки в формате:
{
  "date": "2026-10-22", "start": "12:30", "end": "14:00",
  "subject": "Математический анализ", "kind": "практика",
  "room": "408/С", "teacher": "Васильчук В.Ю."
}
В качестве group_id используется само название группы (например, "ПИб-2").
"""
import re
import time
from datetime import date, datetime, timedelta

import aiohttp
from bs4 import BeautifulSoup

URL = "https://rasp.spbgasu.ru/bitrix/services/main/ajax.php"
PARAMS = {"mode": "class", "c": "gasu:raspisanie.csv", "action": "getRasp"}
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "https://rasp.spbgasu.ru/",
}
KINDS = {"л.": "лекция", "пр.": "практика", "лаб.": "лабораторная"}

# Кэш, чтобы не дёргать сайт по несколько раз за одну проверку
_CACHE: dict[str, tuple[float, list[dict]]] = {}
CACHE_TTL = 120  # секунд


async def _fetch_html(group: str) -> str:
    """POST-запрос к сайту. Bitrix требует CSRF-токен: первый запрос возвращает
    ошибку invalid_csrf вместе с токеном, затем повторяем запрос с ним в заголовке
    (в той же сессии, чтобы cookie сохранились)."""
    form = {
        "search_params[SEARCH]": group,
        "search_params[FILTER]": "GROUPS",
        "search_params[GROUP]": "",
        "search_params[SELECT]": "*",
        "search_params[ONLY_SESSIA]": "N",
    }
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(headers=HEADERS, timeout=timeout) as s:
        csrf = None
        for _ in range(3):
            headers = {"X-Bitrix-Csrf-Token": csrf} if csrf else {}
            async with s.post(URL, params=PARAMS, data=form, headers=headers) as r:
                r.raise_for_status()
                payload = await r.json(content_type=None)

            if payload.get("status") == "success":
                return payload["data"]["html"]

            errors = payload.get("errors") or []
            new_csrf = next(
                (e.get("customData", {}).get("csrf")
                 for e in errors if e.get("code") == "invalid_csrf"),
                None,
            )
            if new_csrf and new_csrf != csrf:
                csrf = new_csrf  # повторяем запрос с токеном
                continue
            raise RuntimeError(f"Сайт вернул ошибку: {errors}")
    raise RuntimeError("Не удалось пройти проверку CSRF на сайте")


def parse_lessons(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    out: list[dict] = []
    for day in soup.select("div.days"):
        date_el = day.select_one(".week_day .date")
        if not date_el:
            continue
        d = datetime.strptime(date_el.get_text(strip=True), "%d.%m.%Y").date()

        for les in day.select(".lessons > .lesson"):
            dn = les.select_one(".day_name")
            block = les.select_one(".lesson_block")
            if not dn or not block:
                continue
            tm = re.search(r"(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})", dn.get_text(" "))
            if not tm:
                continue

            subject = block.select_one(".lesson-name").get_text(" ", strip=True)
            kind = ""
            km = re.search(r"\(([^()]*)\)\s*$", subject)
            if km:
                kind = KINDS.get(km.group(1).strip(), km.group(1).strip())
                subject = subject[: km.start()].strip()

            # Остальные блоки: группа, аудитория, преподаватель (порядок/наличие может отличаться)
            divs = block.find_all("div", recursive=False)
            rest = [x.get_text(" ", strip=True) for x in divs[1:]]
            rest = [x for x in rest if x]
            room = next((x for x in rest if "/" in x), "")
            teacher = next((x for x in rest if x != room and re.search(r"[А-ЯЁ]\.", x)), "")

            pm = re.search(r"(\d+)\s*пара", dn.get_text(" "))
            out.append({
                "pair": int(pm.group(1)) if pm else None,
                "date": d.isoformat(),
                "start": tm.group(1).zfill(5),
                "end": tm.group(2).zfill(5),
                "subject": subject,
                "kind": kind,
                "room": room,
                "teacher": teacher,
            })
    return out


async def _get_all(group: str) -> list[dict]:
    now = time.time()
    cached = _CACHE.get(group)
    if cached and now - cached[0] < CACHE_TTL:
        return cached[1]
    lessons = parse_lessons(await _fetch_html(group))
    _CACHE[group] = (now, lessons)
    return lessons


async def find_group(name: str) -> tuple[str, str] | None:
    """Проверяет, что у такой группы есть расписание. Возвращает (group_id, название)."""
    name = name.strip()
    html = await _fetch_html(name)
    if "days" not in html:
        return None
    return name, name


async def fetch_lessons(group_id: str, monday: date) -> list[dict]:
    """Все занятия группы за неделю, начинающуюся с monday."""
    start, end = monday.isoformat(), (monday + timedelta(days=6)).isoformat()
    return [l for l in await _get_all(group_id) if start <= l["date"] <= end]