#!/usr/bin/env python3
"""Read the current MIIT timetable. Uses only the Python standard library."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import tempfile
import time
from datetime import date, datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
UNIVERSITY = "moscow-rut-miit"
BASE = "https://www.miit.ru"
VERSION = "4.1-current-period"
WEEKDAYS = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]


class Node:
    def __init__(self, tag="root", attrs=()):
        self.tag = tag
        self.attrs = dict(attrs)
        self.children: list[Node | str] = []

    def text(self):
        return " ".join(" ".join(child.text() if isinstance(child, Node) else child for child in self.children).split())

    def has(self, class_name):
        return class_name in self.attrs.get("class", "").split()

    def find(self, tag=None, class_name=None, node_id=None):
        result = []
        for child in self.children:
            if not isinstance(child, Node):
                continue
            if (tag is None or child.tag == tag) and (class_name is None or child.has(class_name)) and (node_id is None or child.attrs.get("id") == node_id):
                result.append(child)
            result.extend(child.find(tag, class_name, node_id))
        return result


class Document(HTMLParser):
    VOID = set("area base br col embed hr img input link meta param source track wbr".split())

    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.root = Node()
        self.stack = [self.root]
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        node = Node(tag, attrs)
        self.stack[-1].children.append(node)
        if tag not in self.VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def fetch(url):
    request = Request(url, headers={"User-Agent": "TimetableHub/4.0 (public university timetable)", "Accept": "text/html"})
    for attempt in range(3):
        try:
            with urlopen(request, timeout=35) as response:
                return response.read().decode("utf-8")
        except Exception:
            if attempt == 2:
                raise
            time.sleep(2 * (attempt + 1))


def unique(values):
    return list(dict.fromkeys(value for value in values if value))


def period_links(document, group_id):
    sections = document.find(class_name="page-header-addendum")
    links = []
    for section in sections:
        for link in section.find(tag="a"):
            href = link.attrs.get("href", "")
            if urlparse(urljoin(BASE, href)).netloc != urlparse(BASE).netloc:
                continue
            query = parse_qs(urlparse(href).query)
            if urlparse(href).path != f"/timetable/{group_id}" or "start" not in query:
                continue
            start = query["start"][0]
            date.fromisoformat(start)
            links.append({"url": urljoin(BASE, href), "start": start, "label": link.text()})
    return links


def pick_period(links, today):
    # The numeric type is not stable across periods: select by the published label.
    regular = [link for link in links if "периодичес" in link["label"].lower()]
    candidates = regular
    current = [link for link in candidates if link["start"] <= today.isoformat()]
    if not candidates:
        raise ValueError("На странице нет периодического расписания занятий; сессия не будет использована вместо него")
    if not current:
        raise ValueError(f"На {today} опубликованы только будущие периоды расписания")
    return max(current, key=lambda link: link["start"])


def semester(document):
    match = re.search(r"Расписание действует\s+с\s+(\d{2}\.\d{2}\.\d{4})\s+по\s+(\d{2}\.\d{2}\.\d{4})", document.text())
    if not match:
        raise ValueError("Не удалось определить срок действия расписания; старый JSON сохранён")
    start = datetime.strptime(match[1], "%d.%m.%Y").date().isoformat()
    end = datetime.strptime(match[2], "%d.%m.%Y").date().isoformat()
    if start > end:
        raise ValueError("Конец периода раньше начала")
    return {"startDate": start, "endDate": end, "firstWeekParity": "odd"}


def subject_text(node):
    parts = []
    for child in node.children:
        if isinstance(child, str):
            parts.append(child)
        elif child.tag not in {"svg", "script", "style"} and not any(child.has(name) for name in ["timetable__grid-text_gray", "timetable__grid-about", "timetable-icon-link"]):
            parts.append(subject_text(child))
    return " ".join(" ".join(parts).split())


def lesson_scopes(cell):
    """Keep sibling metadata with its lesson, including wrapped lesson blocks."""
    branches = []
    for index, child in enumerate(cell.children):
        if not isinstance(child, Node):
            continue
        if child.has("timetable__grid-day-lesson") or child.find(class_name="timetable__grid-day-lesson"):
            branches.append(index)
    for position, start in enumerate(branches):
        end = branches[position + 1] if position + 1 < len(branches) else len(cell.children)
        scope = Node()
        scope.children = cell.children[start:end]
        subjects = scope.find(class_name="timetable__grid-day-lesson")
        if len(subjects) != 1:
            raise ValueError("Не удалось разделить несколько занятий внутри одного блока")
        yield scope, subjects[0]


def details(container, subject_node, group_name):
    kinds = unique(part.strip() for node in subject_node.find(class_name="timetable__grid-text_gray") for part in re.split(r"[,;]", node.text()))
    subject = subject_text(subject_node)
    teachers = unique(node.attrs.get("title", node.text()).split(",")[0].strip() for node in container.find(class_name="icon-academic-cap"))
    locations = container.find(class_name="icon-location")
    rooms = unique(re.sub(r"^Аудитория\s*", "", node.text()) for node in locations)
    addresses = unique(node.attrs.get("title", "").rsplit(",", 1)[0].strip() for node in locations)
    groups = unique(node.text() for node in container.find(class_name="icon-community"))
    subgroups = [name for name in groups if re.match(re.escape(group_name) + r"\s+п/гр", name)]
    return {"subject": subject, "kind": ", ".join(kinds), "teacher": "; ".join(teachers),
            "room": ", ".join(rooms), "address": "; ".join(addresses), "groups": groups,
            "subgroup": ", ".join(name.replace(group_name, "").strip() for name in subgroups),
            "note": "", "isOnline": any("онлайн" in room.lower() or "дистанц" in room.lower() for room in rooms), "onlineUrl": ""}


def entry_id(group_id, entry, parity=""):
    identity = json.dumps([parity, entry], ensure_ascii=False, sort_keys=True)
    return f"{UNIVERSITY}_{group_id}_{hashlib.sha256(identity.encode()).hexdigest()[:12]}"


def parse_week(pane, group_id, group_name, source_url, parity):
    tables = pane.find(tag="table", class_name="timetable__grid")
    if len(tables) != 1:
        raise ValueError(f"Ожидалась одна таблица для {parity}, найдено {len(tables)}")
    rows = tables[0].find(tag="tr")
    if not rows:
        raise ValueError("Таблица расписания не содержит строк")
    headers = rows[0].find(tag="th")
    if len(headers) < 2:
        raise ValueError("Не найдены заголовки дней недели")
    weekdays = []
    for header in headers[1:]:
        day = next((index + 1 for index, name in enumerate(WEEKDAYS) if name in header.text()), None)
        if day is None:
            raise ValueError(f"Неизвестный день недели: {header.text()}")
        weekdays.append(day)
    entries = []
    for row in rows[1:]:
        cells = [child for child in row.children if isinstance(child, Node) and child.tag == "td"]
        if not cells:
            continue
        times = re.search(r"(\d{1,2}:\d{2})\s*[—–-]\s*(\d{1,2}:\d{2})", cells[0].text())
        if not times or len(cells) != len(weekdays) + 1:
            raise ValueError("Изменилась структура таблицы времени: выгрузка остановлена")
        for day, cell in zip(weekdays, cells[1:]):
            for scope, subject_node in lesson_scopes(cell):
                entry = details(scope, subject_node, group_name)
                if not entry["subject"]:
                    continue
                entry.update({"weekday": WEEKDAYS[day - 1], "weekdayNumber": day,
                              "startTime": times[1].zfill(5), "endTime": times[2].zfill(5),
                              "time": f"{times[1]} — {times[2]}", "category": "lesson", "sourceUrl": source_url})
                entry["id"] = entry_id(group_id, entry, parity)
                entries.append(entry)
    return list({entry["id"]: entry for entry in entries}.values())


def parse_regular(html, group_id, group_name, source_url):
    document = Document(html).root
    result = {"schemaVersion": 1, "universityId": UNIVERSITY, "university": "РУТ (МИИТ)",
              "groupId": group_id, "group": group_name,
              "updatedAt": datetime.now(ZoneInfo("Europe/Moscow")).isoformat(timespec="seconds"),
              "semester": semester(document), "events": [], "lessons": {"odd": [], "even": []}, "exceptions": [],
              "meta": {"source": "miit.ru", "parserVersion": VERSION, "timezone": "Europe/Moscow", "sourceUrl": source_url, "errors": []}}
    for index, parity in [(1, "odd"), (2, "even")]:
        panes = document.find(node_id=f"week-{index}")
        if len(panes) != 1:
            raise ValueError(f"Нет раздела week-{index}; сессия не должна подменять регулярные занятия")
        result["lessons"][parity] = parse_week(panes[0], group_id, group_name, source_url, parity)
    if not any(result["lessons"].values()):
        raise ValueError("Обе недели пусты; старый JSON сохранён")
    return result


def update_group(group, today):
    url = f"{BASE}/timetable/{group['id']}"
    html = fetch(url)
    document = Document(html).root
    selected = pick_period(period_links(document, group["id"]), today)
    active_links = [node.attrs.get("href") for section in document.find(class_name="page-header-addendum")
                    for item in section.find(class_name="active") for node in item.find(tag="a")]
    if not any(urljoin(BASE, href or "") == selected["url"] for href in active_links):
        html = fetch(selected["url"])
    result = parse_regular(html, group["id"], group["name"], selected["url"])
    if result["semester"]["startDate"] != selected["start"]:
        raise ValueError("Сайт вернул другой период вместо выбранного; старый JSON сохранён")
    validate_date(result, today)
    return result


def validate_date(result, today):
    if not (result["semester"]["startDate"] <= today.isoformat() <= result["semester"]["endDate"]):
        raise ValueError(f"Нет действующего расписания на {today}: источник предлагает {result['semester']}")


def write_result(destination, result):
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=destination.parent, suffix=".json.tmp", delete=False) as output:
            temporary = Path(output.name)
            output.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        temporary.replace(destination)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--group", action="append", help="ID или название группы; можно указать несколько раз")
    selection.add_argument("--all", action="store_true", help="Обновить все группы (запросы выполняются последовательно)")
    parser.add_argument("--date", default=datetime.now(ZoneInfo("Europe/Moscow")).date().isoformat())
    parser.add_argument("--output", type=Path, default=ROOT / UNIVERSITY / "timetables")
    parser.add_argument("--html", type=Path, help="Разобрать сохранённый HTML без запроса к сайту")
    args = parser.parse_args(argv)
    try:
        today = date.fromisoformat(args.date)
    except ValueError:
        parser.error("Дата должна быть в формате YYYY-MM-DD")
    groups = json.loads((ROOT / UNIVERSITY / "groups.json").read_text(encoding="utf-8"))["groups"]
    known = {value for group in groups for value in [group["id"], group["name"]]}
    unknown = set(args.group or []) - known
    if unknown:
        parser.error("Неизвестные группы: " + ", ".join(sorted(unknown)))
    selected = groups if args.all else [group for group in groups if group["id"] in (args.group or []) or group["name"] in (args.group or [])]
    if not selected or (args.html and len(selected) != 1):
        parser.error("Укажите --group ID/название или --all; для --html нужна ровно одна группа")
    failures = 0
    for group in selected:
        try:
            result = parse_regular(args.html.read_text(encoding="utf-8"), group["id"], group["name"], f"{BASE}/timetable/{group['id']}") if args.html else update_group(group, today)
            validate_date(result, today)
            destination = args.output / f"{group['id']}.json"
            write_result(destination, result)
            print(f"{group['name']}: I неделя {len(result['lessons']['odd'])}, II неделя {len(result['lessons']['even'])}; {result['semester']['startDate']} — {result['semester']['endDate']}", flush=True)
        except Exception as error:
            failures += 1
            print(f"{group['name']}: ОШИБКА: {error}", file=sys.stderr, flush=True)
        if not args.html and len(selected) > 1:
            time.sleep(1)
    return int(failures > 0)


if __name__ == "__main__":
    raise SystemExit(main())
