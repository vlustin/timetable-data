#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Полный парсер РУТ (МИИТ): официальный каталог институт → курс → группа,
встроенный JSON и HTML двух учебных недель или датированных событий.
По умолчанию период выбирается по московской дате; результат записывается
в соседнюю папку moscow-rut-miit. Сетевые ошибки не скрываются.

python3 scripts/moscow_rut_miit_unified_parser.py --all --workers 4
python3 scripts/moscow_rut_miit_unified_parser.py --group-id 193600

--from/--to задают явный диапазон, --types фильтрует типы источника,
--no-smart-weeks отключает пропуск повторяющихся недель.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from zoneinfo import ZoneInfo
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote, parse_qs, urlparse
from urllib.request import Request, urlopen


BASE_URL = "https://www.miit.ru"
TIMETABLE_URL = f"{BASE_URL}/timetable"

UNIVERSITY_ID = "moscow-rut-miit"
UNIVERSITY_NAME = "РУТ (МИИТ)"
PARSER_VERSION = "3.3-current-catalog-unified"
TIMEZONE = "Europe/Moscow"

DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parents[1]

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
)

FETCH_TIMEOUT = 40
FETCH_RETRIES = 3
FETCH_RETRY_DELAY = 2.0

GROUP_RE = re.compile(r"^[А-ЯЁA-Z]{2,12}[-–—]?\d{2,4}[А-ЯЁA-ZА-Яа-я]?$")

WEEKDAY_TO_NUMBER = {
    "Понедельник": 1,
    "Вторник": 2,
    "Среда": 3,
    "Четверг": 4,
    "Пятница": 5,
    "Суббота": 6,
    "Воскресенье": 7,
}
NUMBER_TO_WEEKDAY = {v: k for k, v in WEEKDAY_TO_NUMBER.items()}


@dataclass(frozen=True)
class Group:
    id: str
    name: str
    institute: str = UNIVERSITY_NAME
    specialty: str = ""
    instituteId: str = ""
    course: int = 0


@dataclass(frozen=True)
class GroupRef:
    id: str
    name: str


@dataclass
class Item:
    id: str
    date: str
    weekday: str
    weekdayNumber: int
    time: str
    startTime: str
    endTime: str
    kind: str
    category: str
    subject: str
    teacher: str
    room: str
    address: str
    groups: list[GroupRef] = field(default_factory=list)
    subgroup: str = ""
    note: str = ""
    isOnline: bool = False
    onlineUrl: str = ""
    sourceType: int = 0
    sourceUrl: str = ""


def fetch_text(url: str, timeout: int | None = None, retries: int | None = None) -> str:
    timeout = FETCH_TIMEOUT if timeout is None else timeout
    retries = FETCH_RETRIES if retries is None else retries

    last_error: Exception | None = None

    for attempt in range(1, retries + 1):
        try:
            req = Request(
                url,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.7,en;q=0.6",
                    "Cache-Control": "no-cache",
                    "Pragma": "no-cache",
                },
            )
            with urlopen(req, timeout=timeout) as r:
                charset = r.headers.get_content_charset() or "utf-8"
                return r.read().decode(charset, errors="replace")
        except Exception as e:
            last_error = e
            if attempt < retries:
                print(f"      попытка {attempt}/{retries} не удалась: {e}; повторяю...")
                time.sleep(FETCH_RETRY_DELAY)

    raise last_error if last_error else RuntimeError("fetch failed")



def clean(value: object) -> str:
    if value is None:
        return ""
    text = html.unescape(str(value))
    text = text.replace("\xa0", " ").replace("\t", " ").strip()
    return re.sub(r"\s+", " ", text)


def strip_tags(value: str) -> str:
    return clean(re.sub(r"<[^>]+>", "", value or ""))


def is_group_name(value: str) -> bool:
    return bool(GROUP_RE.match(clean(value)))


def room_from_name(value: str) -> str:
    text = clean(value)
    text = re.sub(r"^(аудитория|ауд\.?|кабинет|каб\.?)\s*", "", text, flags=re.I)
    return text.strip(" .,:;—–-")


def extract_timetable_data(raw_html: str) -> list[dict]:
    marker = "window._timetableData"
    start = raw_html.find(marker)
    if start == -1:
        return []

    equals = raw_html.find("=", start)
    if equals == -1:
        return []

    array_start = raw_html.find("[", equals)
    if array_start == -1:
        return []

    # Ищем конец JSON-массива по балансу скобок с учётом строк.
    depth = 0
    in_string = False
    escape = False

    for i in range(array_start, len(raw_html)):
        ch = raw_html[i]

        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue

        if ch == '"':
            in_string = True
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                json_text = raw_html[array_start : i + 1]
                return json.loads(json_text)

    return []


def html_to_lines(raw_html: str) -> list[str]:
    raw = re.sub(r"<script[\s\S]*?</script>", "", raw_html, flags=re.I)
    raw = re.sub(r"<style[\s\S]*?</style>", "", raw, flags=re.I)
    raw = re.sub(r"<br\s*/?>", "\n", raw, flags=re.I)
    raw = re.sub(r"</(div|li|p|a|span|td|tr|section|h\d)>", "\n", raw, flags=re.I)
    text = re.sub(r"<[^>]+>", "", raw)
    return [clean(x) for x in text.splitlines() if clean(x)]


def parse_groups(raw_html: str) -> list[Group]:
    from miit_groups import parse_groups as parse_catalog
    catalog = parse_catalog(raw_html)
    institutes = {entry['id']: entry for entry in catalog['institutes']}
    return [Group(id=entry['id'], name=entry['name'], institute=institutes[entry['instituteId']]['name'],
                  instituteId=entry['instituteId'], course=entry['course']) for entry in catalog['groups']]


def parse_legacy_group_links(raw_html: str) -> list[Group]:
    raw = re.sub(r"<script[\s\S]*?</script>", "", raw_html, flags=re.I)
    raw = re.sub(r"<style[\s\S]*?</style>", "", raw, flags=re.I)

    pattern = re.compile(
        r"<a[^>]+href=[\"']/timetable/(\d+)[^\"']*[\"'][^>]*>(.*?)</a>",
        flags=re.I | re.S,
    )

    groups: list[Group] = []
    seen: set[str] = set()

    for match in pattern.finditer(raw):
        group_id = match.group(1)
        group_name = strip_tags(match.group(2))

        if not is_group_name(group_name):
            continue
        if group_id in seen:
            continue

        seen.add(group_id)
        groups.append(Group(id=group_id, name=group_name))

    return sorted(groups, key=lambda g: g.name)


def group_maps(groups: list[Group]) -> tuple[dict[str, Group], dict[str, Group]]:
    return {g.id: g for g in groups}, {g.name.lower(): g for g in groups}


def normalize_category(kind: str) -> str:
    text = kind.lower()

    if "экзам" in text:
        return "exam"
    if "конс" in text:
        return "consultation"
    if "зач" in text:
        return "credit"
    if "отмен" in text:
        return "cancelled"
    if "замен" in text:
        return "replacement"
    if any(x in text for x in ["лекц", "практи", "лаборатор", "семинар", "занят", "курсов"]):
        return "lesson"

    return "other"


def event_kind(event: dict) -> str:
    value = clean(event.get("textTitle")) or clean(event.get("badgeHint")) or "Занятие"
    return ", ".join(dict.fromkeys(part.strip() for part in re.split(r'[,;]', value) if part.strip()))


def event_subject(event: dict) -> str:
    return strip_tags(clean(event.get("text")))


def event_note(event: dict) -> str:
    parts = [
        strip_tags(clean(event.get("noteTitle"))),
        strip_tags(clean(event.get("noteText"))),
        strip_tags(clean(event.get("badgeText"))),
    ]
    return " ".join(p for p in parts if p).strip()


def event_teachers(event: dict) -> str:
    lecturers = event.get("lecturers") or []

    names: list[str] = []
    for lecturer in lecturers:
        # Для UI лучше полное ФИО, если оно есть.
        name = clean(lecturer.get("fullFio")) or clean(lecturer.get("shortFio"))
        if name and name not in names:
            names.append(name)

    if names:
        return ", ".join(names)

    return ""


def event_room(event: dict) -> str:
    rooms = event.get("rooms") or []

    names: list[str] = []
    for room in rooms:
        name = room_from_name(clean(room.get("name")))
        if name and name not in names:
            names.append(name)

    return ", ".join(names)


def group_ref_from_event_group(raw_group: dict, current_group: Group, groups_by_name: dict[str, Group]) -> GroupRef | None:
    # В разных версиях МИИТа названия полей могут отличаться.
    group_id = clean(
        raw_group.get("id")
        or raw_group.get("idEdGroup")
        or raw_group.get("id_ed_group")
        or raw_group.get("value")
    )

    name = clean(
        raw_group.get("name")
        or raw_group.get("title")
        or raw_group.get("text")
        or raw_group.get("groupName")
        or raw_group.get("nameGroup")
    )

    if not name and group_id:
        # Иногда есть id, но нет имени.
        # Для МИИТа попробуем найти имя по groups.json.
        # Тут проще пройти по name-map невозможно, поэтому оставляем id.
        name = ""

    if not name:
        return None

    if name.lower() == current_group.name.lower():
        return None

    found = groups_by_name.get(name.lower())
    if found:
        return GroupRef(id=found.id, name=found.name)

    return GroupRef(id=group_id if group_id.isdigit() else "", name=name)


def event_groups(event: dict, current_group: Group, groups_by_name: dict[str, Group]) -> list[GroupRef]:
    raw_groups = event.get("groups") or []

    result: list[GroupRef] = []
    for raw_group in raw_groups:
        if not isinstance(raw_group, dict):
            continue

        ref = group_ref_from_event_group(raw_group, current_group, groups_by_name)
        if not ref:
            continue

        if not any((g.id and g.id == ref.id) or g.name.lower() == ref.name.lower() for g in result):
            result.append(ref)

    return result


def is_online(*values: str) -> bool:
    text = " ".join(values).lower()
    return any(x in text for x in ["онлайн", "дистанц", "zoom", "teams", "webinar", "вебинар"])


def online_url(*values: str) -> str:
    text = " ".join(values)
    m = re.search(r"https?://\S+", text)
    return m.group(0).rstrip(".,);") if m else ""


def make_item_id(group_id: str, item: Item) -> str:
    base = "|".join(
        [
            UNIVERSITY_ID,
            group_id,
            item.date,
            str(item.weekdayNumber),
            item.time,
            item.kind,
            item.subject,
            item.teacher,
            item.room,
            ",".join(g.name for g in item.groups),
        ]
    )
    digest = hashlib.sha1(base.encode("utf-8")).hexdigest()[:12]
    return f"{UNIVERSITY_ID}_{group_id}_{item.date or 'w' + str(item.weekdayNumber)}_{digest}"


def parse_items_from_timetable_data(
    data: list[dict],
    group: Group,
    source_type: int,
    source_url: str,
    groups_by_name: dict[str, Group],
) -> list[Item]:
    items: list[Item] = []

    for day in data:
        day_date = clean(day.get("hisdate"))
        weekday = clean(day.get("dayDisplay"))
        weekday_number = int(day.get("dayNumber") or 0) or WEEKDAY_TO_NUMBER.get(weekday, 0)

        for slot in day.get("timeSlots") or []:
            start_time = clean(slot.get("slotStartDisplay"))
            end_time = clean(slot.get("slotEndDisplay"))
            time_value = f"{start_time} — {end_time}".strip(" —")

            for event in slot.get("events") or []:
                kind = event_kind(event)
                subject = event_subject(event)
                note = event_note(event)
                teacher = event_teachers(event)
                room = event_room(event)
                category = normalize_category(kind)
                groups = event_groups(event, group, groups_by_name)

                item = Item(
                    id="",
                    date=day_date,
                    weekday=weekday,
                    weekdayNumber=weekday_number,
                    time=time_value,
                    startTime=start_time,
                    endTime=end_time,
                    kind=kind,
                    category=category,
                    subject=subject,
                    teacher=teacher,
                    room=room,
                    address="",  # Не выдумываем адрес для МИИТа.
                    groups=groups,
                    subgroup="",
                    note=note,
                    isOnline=is_online(subject, note, room),
                    onlineUrl=online_url(subject, note, room),
                    sourceType=source_type,
                    sourceUrl=source_url,
                )
                item.id = make_item_id(group.id, item)
                items.append(item)

    return items


def monday_for(value: date) -> date:
    return value - timedelta(days=value.weekday())


def daterange_weeks(start: date, end: date) -> list[date]:
    current = monday_for(start)
    last = monday_for(end)
    result = []

    while current <= last:
        result.append(current)
        current += timedelta(days=7)

    return result


def week_index(day: date, semester_start: date) -> int:
    return ((monday_for(day) - monday_for(semester_start)).days // 7) + 1


def parity_for(day: date, semester_start: date, first_week_parity: str) -> str:
    first = first_week_parity if first_week_parity in {"odd", "even"} else "odd"
    idx = week_index(day, semester_start)

    if idx % 2 == 1:
        return first

    return "even" if first == "odd" else "odd"


def item_as_lesson(item: Item) -> Item:
    cloned = Item(
        id=item.id,
        date="",
        weekday=item.weekday,
        weekdayNumber=item.weekdayNumber,
        time=item.time,
        startTime=item.startTime,
        endTime=item.endTime,
        kind=item.kind,
        category=item.category,
        subject=item.subject,
        teacher=item.teacher,
        room=item.room,
        address=item.address,
        groups=item.groups,
        subgroup=item.subgroup,
        note=item.note,
        isOnline=item.isOnline,
        onlineUrl=item.onlineUrl,
        sourceType=item.sourceType,
        sourceUrl=item.sourceUrl,
    )
    cloned.id = make_item_id("", cloned)
    return cloned


def item_key(item: Item) -> tuple:
    return (
        item.date,
        item.weekdayNumber,
        item.time,
        item.kind,
        item.subject,
        item.teacher,
        item.room,
        tuple((g.id, g.name) for g in item.groups),
    )


def lesson_key(item: Item) -> tuple:
    return (
        item.weekdayNumber,
        item.time,
        item.kind,
        item.subject,
        item.teacher,
        item.room,
        tuple((g.id, g.name) for g in item.groups),
        item.subgroup,
    )


def dedupe(items: list[Item]) -> list[Item]:
    # Важно: сохраняем первое найденное событие, а не последнее.
    # Во время сессии МИИТ может отдавать один и тот же набор событий
    # на разные start-недели. Если перезаписывать item, sourceUrl у всех
    # событий становится последним запрошенным URL, что сбивает отладку.
    by_key: dict[tuple, Item] = {}
    for item in items:
        key = item_key(item)
        if key not in by_key:
            by_key[key] = item

    return sorted(
        by_key.values(),
        key=lambda x: (x.date or "9999-99-99", x.weekdayNumber or 99, x.startTime, x.subject, x.kind),
    )


def item_signature_without_source(item: Item) -> tuple:
    # Сигнатура без sourceUrl/id. Нужна, чтобы понять:
    # сайт отдаёт реально новую неделю или повторяет один и тот же набор событий.
    return (
        item.date,
        item.weekday,
        item.weekdayNumber,
        item.time,
        item.kind,
        item.category,
        item.subject,
        item.teacher,
        item.room,
        item.address,
        tuple((g.id, g.name) for g in item.groups),
        item.subgroup,
        item.note,
    )


def batch_signature(items: list[Item]) -> tuple:
    return tuple(sorted(item_signature_without_source(item) for item in items))


def batch_dates(items: list[Item]) -> tuple:
    return tuple(sorted({item.date for item in items if item.date}))


def build_lessons(
    lesson_items: list[Item],
    semester_start: date,
    first_week_parity: str,
) -> dict[str, list[Item]]:
    result_maps: dict[str, dict[tuple, Item]] = {"odd": {}, "even": {}}

    for item in lesson_items:
        if item.category != "lesson":
            continue

        try:
            d = date.fromisoformat(item.date)
            parity = parity_for(d, semester_start, first_week_parity)
            template = item_as_lesson(item)
            result_maps[parity][lesson_key(template)] = template
        except ValueError:
            # Если даты нет — кладём в обе недели, чтобы не потерять.
            template = item_as_lesson(item)
            result_maps["odd"][lesson_key(template)] = template
            result_maps["even"][lesson_key(template)] = template

    return {
        "odd": sorted(result_maps["odd"].values(), key=lambda x: (x.weekdayNumber or 99, x.startTime, x.subject)),
        "even": sorted(result_maps["even"].values(), key=lambda x: (x.weekdayNumber or 99, x.startTime, x.subject)),
    }


def item_dict(item: Item) -> dict:
    d = asdict(item)
    d["groups"] = [asdict(g) for g in item.groups]
    return d


def build_url(group_id: str, week_start: date, type_: int) -> str:
    return f"{TIMETABLE_URL}/{quote(group_id)}?start={week_start.isoformat()}&type={type_}"


def fetch_items_for_period(
    group: Group,
    start: date,
    end: date,
    types: list[int],
    groups_by_name: dict[str, Group],
    delay: float,
    smart_weeks: bool = True,
) -> list[Item]:
    items: list[Item] = []

    seen_urls: set[str] = set()
    finished_types: set[int] = set()
    previous_by_type: dict[int, tuple[tuple, tuple]] = {}
    failures: list[str] = []

    for week_start in daterange_weeks(start, end):
        for type_ in types:
            if smart_weeks and type_ in finished_types:
                print(f"    type={type_} {week_start.isoformat()}: пропуск, сайт повторяет тот же набор")
                continue

            url = build_url(group.id, max(week_start, start), type_)
            if url in seen_urls:
                continue
            seen_urls.add(url)

            try:
                raw = fetch_text(url)
                data = extract_timetable_data(raw)
                parsed = parse_items_from_timetable_data(
                    data=data,
                    group=group,
                    source_type=type_,
                    source_url=url,
                    groups_by_name=groups_by_name,
                )
                if 'window._timetableData' not in raw:
                    parsed = html_items_for_week(raw, group, week_start, type_, url, groups_by_name)
                parsed = [item for item in parsed if not item.date or start.isoformat() <= item.date <= end.isoformat()]

                print(f"    type={type_} {week_start.isoformat()}: {len(parsed)}")
                items.extend(parsed)

                if smart_weeks and parsed:
                    current_dates = batch_dates(parsed)
                    current_signature = batch_signature(parsed)
                    previous = previous_by_type.get(type_)

                    if previous == (current_dates, current_signature):
                        # Если на следующей неделе сайт отдал ровно те же даты и те же события,
                        # значит это не недельное расписание, а один и тот же сессионный набор.
                        # Дальше по этому type нет смысла гонять остальные недели.
                        finished_types.add(type_)
                        print(f"    type={type_}: повтор набора; дальнейшие недели по этому type пропускаю")
                    else:
                        previous_by_type[type_] = (current_dates, current_signature)

            except Exception as e:
                print(f"    type={type_} {week_start.isoformat()}: ошибка — {e}")
                failures.append(f'{url}: {e}')

            time.sleep(delay)

    if failures:
        raise RuntimeError('; '.join(failures))
    return dedupe(items)


def save_groups(root: Path, groups: list[Group]) -> Path:
    path = root / UNIVERSITY_ID / "groups.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "universityId": UNIVERSITY_ID,
                "university": UNIVERSITY_NAME,
                "groups": [asdict(g) for g in groups],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return path


def save_timetable(
    root: Path,
    group: Group,
    all_items: list[Item],
    semester_start: date,
    semester_end: date,
    first_week_parity: str,
    errors: list[str] | None = None,
) -> Path:
    lesson_source_items = [x for x in all_items if x.category == "lesson"]
    event_items = [x for x in all_items if x.category not in {"lesson", "cancelled", "replacement"}]

    lessons = build_lessons(lesson_source_items, semester_start, first_week_parity)

    payload = {
        "schemaVersion": 1,
        "universityId": UNIVERSITY_ID,
        "university": UNIVERSITY_NAME,
        "groupId": group.id,
        "group": group.name,
        "updatedAt": datetime.now(ZoneInfo(TIMEZONE)).isoformat(timespec="seconds"),
        "semester": {
            "startDate": semester_start.isoformat(),
            "endDate": semester_end.isoformat(),
            "firstWeekParity": first_week_parity,
        },
        "events": [item_dict(x) for x in event_items],
        "lessons": {
            "odd": [item_dict(x) for x in lessons["odd"]],
            "even": [item_dict(x) for x in lessons["even"]],
        },
        "exceptions": [item_dict(x) for x in all_items if x.category in {"cancelled", "replacement"}],
        "meta": {
            "source": "miit.ru",
            "parserVersion": PARSER_VERSION,
            "timezone": TIMEZONE,
            "errors": errors or [],
        },
    }

    path = root / UNIVERSITY_ID / "timetables" / f"{group.id}.json"
    from parse_miit import write_result
    write_result(path, payload)
    return path


def html_item(entry: dict, group: Group, source_type: int, source_url: str, groups_by_name: dict[str, Group]) -> Item:
    refs = []
    for value in entry.get('groups', []):
        name = value if isinstance(value, str) else value.get('name', '')
        if name == group.name or name.startswith(group.name + ' п/гр'):
            continue
        known = groups_by_name.get(name.lower())
        refs.append(GroupRef(id=known.id if known else '', name=name))
    values = {key: entry.get(key, '') for key in ['date', 'weekday', 'time', 'startTime', 'endTime', 'kind', 'subject', 'teacher', 'room', 'address', 'subgroup', 'note', 'onlineUrl']}
    item = Item(id='', weekdayNumber=entry.get('weekdayNumber', 0), category=normalize_category(values['kind']),
                groups=refs, isOnline=entry.get('isOnline', False), sourceType=source_type, sourceUrl=source_url, **values)
    item.id = make_item_id(group.id, item)
    return item


def parse_dated_html(raw: str, group: Group, source_type: int, source_url: str, groups_by_name: dict[str, Group]) -> list[Item]:
    from parse_miit import Document, details, semester
    document = Document(raw).root
    period = semester(document)
    months = {name: index + 1 for index, name in enumerate('января февраля марта апреля мая июня июля августа сентября октября ноября декабря'.split())}
    candidates = []
    for section in document.find(class_name='timetable__grid_md'):
        for block in section.find(class_name='info-block'):
            headers = block.find(class_name='info-block__header-text')
            if not headers:
                continue
            text = headers[0].text()
            match = re.search(r'(\d{1,2})\s+(' + '|'.join(months) + r')(?:\s+(\d{4}))?', text)
            if not match:
                continue
            year_start = date.fromisoformat(period['startDate']).year
            year_end = date.fromisoformat(period['endDate']).year
            possible = [date(int(match[3]) if match[3] else year, months[match[2]], int(match[1])) for year in range(year_start, year_end + 1)]
            possible = list(dict.fromkeys(day for day in possible if period['startDate'] <= day.isoformat() <= period['endDate']))
            if len(possible) != 1:
                raise ValueError(f'Неоднозначная дата события: {text}')
            day = possible[0]
            for slot in block.find(class_name='timetable__list-timeslot'):
                times = re.search(r'(\d{1,2}:\d{2})\s*[—–-]\s*(\d{1,2}:\d{2})', slot.text())
                subjects = slot.find(class_name='pl-4')
                if not times or len(subjects) != 1:
                    raise ValueError('Не удалось разобрать датированное событие')
                entry = details(subjects[0], subjects[0], group.name)
                entry.update(date=day.isoformat(), weekday=NUMBER_TO_WEEKDAY[day.isoweekday()], weekdayNumber=day.isoweekday(),
                             startTime=times[1].zfill(5), endTime=times[2].zfill(5), time=f'{times[1]} — {times[2]}')
                candidates.append(html_item(entry, group, source_type, source_url, groups_by_name))
    if not candidates:
        # Do not treat an unrecognised page as a successful empty schedule.
        if not re.search(r'(нет занятий|расписание отсутствует|занятия отсутствуют|событи[йя] нет)', document.text(), re.I):
            raise ValueError('Не найден ни JSON, ни поддерживаемый HTML датированного расписания')
    return dedupe(candidates)


def html_items_for_week(raw: str, group: Group, week_start: date, source_type: int, source_url: str, groups_by_name: dict[str, Group]) -> list[Item]:
    from parse_miit import Document, parse_regular
    if not Document(raw).root.find(node_id='week-1'):
        return parse_dated_html(raw, group, source_type, source_url, groups_by_name)
    payload = parse_regular(raw, group.id, group.name, source_url)
    period = payload['semester']
    parity = parity_for(week_start, date.fromisoformat(period['startDate']), period['firstWeekParity'])
    result = []
    for entry in payload['lessons'][parity]:
        day = monday_for(week_start) + timedelta(days=entry['weekdayNumber'] - 1)
        if period['startDate'] <= day.isoformat() <= period['endDate']:
            result.append(html_item(dict(entry, date=day.isoformat()), group, source_type, source_url, groups_by_name))
    return result


def current_payload(group: Group, groups_by_name: dict[str, Group], types: list[int], today: date, smart_weeks: bool = True) -> dict:
    from parse_miit import Document, parse_regular, period_links, pick_period, semester
    url = f'{TIMETABLE_URL}/{group.id}'
    raw = fetch_text(url)
    doc = Document(raw).root
    links = period_links(doc, group.id)
    if not links:
        raise ValueError('Официальная страница не содержит опубликованных периодов расписания')
    periodic = [link for link in links if 'периодичес' in link['label'].lower()]
    if periodic:
        selected = pick_period(links, today) if any(link['start'] <= today.isoformat() for link in periodic) else min(periodic, key=lambda link: link['start'])
    else:
        eligible = [link for link in links if link['start'] <= today.isoformat()]
        selected = max(eligible, key=lambda link: link['start']) if eligible else min(links, key=lambda link: link['start'])
    active = [node.attrs.get('href', '') for section in doc.find(class_name='page-header-addendum')
              for block in section.find(class_name='active') for node in block.find(tag='a')]
    if not any(urlparse(href).query == urlparse(selected['url']).query for href in active):
        raw = fetch_text(selected['url'])
    period = semester(Document(raw).root)
    if period['startDate'] != selected['start'] or period['endDate'] < today.isoformat():
        raise ValueError(f'Источник не содержит действующего периода на {today}: {period}')
    source_type = int(parse_qs(urlparse(selected['url']).query).get('type', ['0'])[0])
    is_periodic = 'периодичес' in selected['label'].lower()
    if not is_periodic:
        entries = []
        if source_type in types:
            if not smart_weeks:
                entries = fetch_items_for_period(group, date.fromisoformat(period['startDate']), date.fromisoformat(period['endDate']), [source_type], groups_by_name, 0.25, False)
            else:
                # Published non-periodic pages explicitly contain all dates, not a weekly slice.
                entries = parse_items_from_timetable_data(extract_timetable_data(raw), group, source_type, selected['url'], groups_by_name) if 'window._timetableData' in raw else parse_dated_html(raw, group, source_type, selected['url'], groups_by_name)
                entries = [item for item in entries if item.date and period['startDate'] <= item.date <= period['endDate']]
        payload = {'schemaVersion': 1, 'universityId': UNIVERSITY_ID, 'university': UNIVERSITY_NAME, 'groupId': group.id, 'group': group.name,
                   'semester': period, 'lessons': {'odd': [], 'even': []},
                   'events': [item_dict(item) for item in entries if item.category not in {'cancelled', 'replacement'}],
                   'exceptions': [item_dict(item) for item in entries if item.category in {'cancelled', 'replacement'}]}
    else:
        payload = parse_regular(raw, group.id, group.name, selected['url']) if 'window._timetableData' not in raw else None
    if payload is None:
        # Retain the original JSON reader for source versions that still provide it.
        items = fetch_items_for_period(group, date.fromisoformat(period['startDate']), date.fromisoformat(period['endDate']), types, groups_by_name, 0.25, smart_weeks)
        lessons = build_lessons([item for item in items if item.category == 'lesson'], date.fromisoformat(period['startDate']), period['firstWeekParity'])
        payload = {'schemaVersion': 1, 'universityId': UNIVERSITY_ID, 'university': UNIVERSITY_NAME, 'groupId': group.id, 'group': group.name,
                   'semester': period, 'events': [item_dict(item) for item in items if item.category not in {'lesson','cancelled','replacement'}],
                   'exceptions': [item_dict(item) for item in items if item.category in {'cancelled','replacement'}],
                   'lessons': {key: [item_dict(item) for item in value] for key,value in lessons.items()}}
    elif is_periodic:
        payload['lessons'] = {key: [item_dict(html_item(entry, group, source_type, selected['url'], groups_by_name)) for entry in entries]
                              for key,entries in payload['lessons'].items()}
    if source_type not in types:
        payload['lessons'] = {'odd': [], 'even': []}
    # Read published dated periods, rather than probing numeric types that redirect to lessons.
    for link in links:
        if link['url'] == selected['url'] or 'периодичес' in link['label'].lower():
            continue
        type_ = int(parse_qs(urlparse(link['url']).query).get('type', ['0'])[0])
        if type_ not in types or link['start'] > period['endDate']:
            continue
        dated_raw = fetch_text(link['url'])
        data = extract_timetable_data(dated_raw)
        entries = parse_items_from_timetable_data(data, group, type_, link['url'], groups_by_name) if 'window._timetableData' in dated_raw else parse_dated_html(dated_raw, group, type_, link['url'], groups_by_name)
        for item in entries:
            if item.date and period['startDate'] <= item.date <= period['endDate']:
                payload['exceptions' if item.category in {'cancelled','replacement'} else 'events'].append(item_dict(item))
    payload['updatedAt'] = datetime.now(ZoneInfo(TIMEZONE)).isoformat(timespec='seconds')
    payload['meta'] = {'source': 'miit.ru', 'parserVersion': PARSER_VERSION, 'timezone': TIMEZONE, 'errors': [], 'sourceUrl': selected['url'], 'scheduleFormat': 'periodic' if is_periodic else 'dated', 'scheduleStatus': 'upcoming' if today.isoformat() < period['startDate'] else 'current'}
    return payload


def find_group(groups: list[Group], group_id: str, group_name: str) -> Group:
    for group in groups:
        if group.id == group_id:
            return group

    target = group_name.lower().strip()
    for group in groups:
        if group.name.lower() == target:
            return group

    return Group(id=group_id, name=group_name)


def parse_types(value: str) -> list[int]:
    result = []
    for part in value.split(","):
        part = part.strip()
        if part:
            result.append(int(part))
    return result or [1, 4]


def main() -> int:
    parser = argparse.ArgumentParser(description="РУТ (МИИТ) parser via window._timetableData")
    parser.add_argument("--group-id", default="193600")
    parser.add_argument("--group-name", default="СЖД-341")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--groups-only", action="store_true")
    parser.add_argument("--retry-failed", action="store_true", help="Повторить ошибки полного запуска из parse-report.json")
    parser.add_argument("--from", dest="date_from", default=None)
    parser.add_argument("--to", dest="date_to", default=None)
    parser.add_argument("--types", type=parse_types, default=[1, 2, 4])
    parser.add_argument("--first-week-parity", choices=["odd", "even"], default="odd")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--delay", type=float, default=0.25)
    parser.add_argument("--timeout", type=int, default=40, help="Таймаут одного HTTP-запроса, секунд")
    parser.add_argument("--retries", type=int, default=3, help="Количество попыток на один HTTP-запрос")
    parser.add_argument("--no-smart-weeks", action="store_true", help="Не пропускать повторяющиеся недели; режим полного обхода")
    parser.add_argument("--workers", type=int, default=1, choices=range(1, 5), help="Одновременные группы, от 1 до 4")
    args = parser.parse_args()

    global FETCH_TIMEOUT, FETCH_RETRIES
    FETCH_TIMEOUT = args.timeout
    FETCH_RETRIES = args.retries

    root = Path(args.output).expanduser()
    if bool(args.date_from) != bool(args.date_to):
        parser.error('--from и --to нужно указывать вместе')
    start = date.fromisoformat(args.date_from) if args.date_from else None
    end = date.fromisoformat(args.date_to) if args.date_to else None
    if start and end and start > end:
        parser.error('Конец периода раньше начала')
    if args.retries < 1 or args.timeout < 1 or args.delay < 0 or args.limit < 0:
        parser.error('Неверные значения retries/timeout/delay/limit')

    print(f"Папка вывода: {root}")
    print("Скачиваю список групп...")

    from miit_groups import parse_groups as parse_catalog
    from parse_miit import write_result
    raw_catalog = fetch_text(TIMETABLE_URL)
    catalog = parse_catalog(raw_catalog)
    groups = parse_groups(raw_catalog)

    _by_id, by_name = group_maps(groups)

    groups_path = root / UNIVERSITY_ID / 'groups.json'
    write_result(groups_path, catalog)
    print(f"groups.json: {groups_path} ({len(groups)} групп)")

    if args.groups_only:
        print("Готово.")
        return 0

    previous_successes = set()
    if args.retry_failed:
        if args.limit or args.date_from or args.date_to:
            parser.error('--retry-failed нельзя совмещать с limit или явным периодом')
        report_path = root / UNIVERSITY_ID / 'parse-report.json'
        if not report_path.exists():
            parser.error('Не найден parse-report.json предыдущего полного запуска')
        previous_report = json.loads(report_path.read_text(encoding='utf-8'))
        if previous_report.get('requestedGroups') != len(groups):
            parser.error('Повтор допускается только после полного запуска с тем же каталогом')
        previous_successes = set(previous_report.get('successfulIds', set(_by_id) - set(previous_report['missingIds'])))
        previous_successes = {id_ for id_ in previous_successes if id_ in _by_id and (root / UNIVERSITY_ID / 'timetables' / f'{id_}.json').exists()}
        targets = [group for group in groups if group.id not in previous_successes]
    else:
        targets = groups if args.all else [find_group(groups, args.group_id, args.group_name)]
    if any(group.id not in _by_id for group in targets):
        parser.error('Выбранной группы нет в официальном актуальном каталоге')
    if args.limit:
        targets = targets[:args.limit]

    print(f"Выгружаю групп: {len(targets)}")
    print(f"Период: {start} — {end}; types={args.types}" if start else f"Период: автоматически по официальному источнику; types={args.types}")

    created_paths: list[Path] = []

    failures = []
    today = datetime.now(ZoneInfo(TIMEZONE)).date()

    def process(group):
        if start is None:
            payload = current_payload(group, by_name, args.types, today, not args.no_smart_weeks)
            if args.no_smart_weeks and payload['meta']['scheduleFormat'] == 'periodic':
                period = payload['semester']
                items = fetch_items_for_period(group, date.fromisoformat(period['startDate']), date.fromisoformat(period['endDate']), args.types, by_name, args.delay, False)
                return save_timetable(root, group, items, date.fromisoformat(period['startDate']), date.fromisoformat(period['endDate']), args.first_week_parity)
            payload['semester']['firstWeekParity'] = args.first_week_parity
            destination = root / UNIVERSITY_ID / 'timetables' / f'{group.id}.json'
            write_result(destination, payload)
            return destination
        else:
            items = fetch_items_for_period(
                group=group,
                start=start,
                end=end,
                types=args.types,
                groups_by_name=by_name,
                delay=args.delay,
                smart_weeks=not args.no_smart_weeks,
            )
            return save_timetable(
            root=root,
            group=group,
            all_items=items,
            semester_start=start,
            semester_end=end,
            first_week_parity=args.first_week_parity,
            errors=[],
        )

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        pending = {}
        for group in targets:
            pending[executor.submit(process, group)] = group
            time.sleep(args.delay)
        for idx, future in enumerate(as_completed(pending), 1):
            group = pending[future]
            try:
                path = future.result()
                created_paths.append(path)
                print(f'[{idx}/{len(targets)}] {group.name}: сохранено', flush=True)
            except Exception as error:
                failures.append({'id': group.id, 'name': group.name, 'error': str(error)})
                print(f'[{idx}/{len(targets)}] {group.name}: ОШИБКА: {error}', flush=True)

    timetables_dir = root / UNIVERSITY_ID / "timetables"
    existing_files = {p.stem for p in timetables_dir.glob("*.json")}
    expected_ids = set(_by_id) if args.retry_failed else {g.id for g in targets}
    successful_ids = previous_successes | {path.stem for path in created_paths}
    missing_ids = sorted(expected_ids - successful_ids)
    report = {'updatedAt': datetime.now(ZoneInfo(TIMEZONE)).isoformat(timespec='seconds'),
              'currentGroups': len(groups), 'requestedGroups': len(expected_ids), 'successfulFiles': len(successful_ids),
              'successfulIds': sorted(successful_ids), 'runRequestedGroups': len(targets), 'runSuccessfulFiles': len(created_paths),
              'failedGroups': failures, 'missingIds': missing_ids,
              'orphanTimetableIds': sorted(existing_files - {group.id for group in groups})}
    write_result(root / UNIVERSITY_ID / 'parse-report.json', report)

    print(f"Проверка файлов: ожидается {len(expected_ids)}, успешно создано {len(successful_ids)} (в текущем проходе {len(created_paths)})")
    if missing_ids:
        print("Не хватает файлов:")
        for missing_id in missing_ids:
            print(f"  {missing_id}")
    else:
        print("Все файлы групп сохранены.")

    print("Готово.")
    return int(bool(failures or missing_ids))


if __name__ == "__main__":
    raise SystemExit(main())
