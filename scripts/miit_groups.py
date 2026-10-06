"""Official MIIT institute → course → group catalogue adapter."""
import argparse
import json
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from parse_miit import BASE, ROOT, UNIVERSITY, Document, fetch, write_result

LETTERS = dict(zip('абвгдеёжзийклмнопрстуфхцчшщъыьэюя',
    ['a','b','v','g','d','e','yo','zh','z','i','y','k','l','m','n','o','p','r','s','t','u','f','kh','ts','ch','sh','shch','','y','','e','yu','ya']))


def slug(value):
    value = ''.join(LETTERS.get(char, char) for char in value.lower())
    return re.sub(r'[^a-z0-9]+', '-', value).strip('-')


def natural_key(value):
    return tuple((1, int(part)) if part.isdigit() else (0, part.casefold()) for part in re.split(r'(\d+)', value))


def parse_groups(html):
    root = Document(html).root
    catalogs = root.find(class_name='timetable-catalog')
    if len(catalogs) != 1:
        raise ValueError('Не найден единственный официальный каталог групп')
    institutes, groups = [], {}
    for block in catalogs[0].find(class_name='info-block'):
        headers = block.find(class_name='info-block__header-text')
        if not headers:
            continue
        label = headers[0].text()
        match = re.fullmatch(r'(.+?)\s*\((.+)\)', label)
        if not match:
            raise ValueError(f'Не удалось прочитать название и сокращение института: {label}')
        institute_id = slug(block.attrs.get('id') or match[2])
        if not institute_id or any(institute['id'] == institute_id for institute in institutes):
            raise ValueError(f'Неуникальный идентификатор института: {institute_id}')
        institutes.append({'id': institute_id, 'name': match[1].strip(), 'shortName': match[2].strip()})
        for item in block.find(tag='li', class_name='text-form__item'):
            names = item.find(class_name='text-form__item-name')
            course_match = re.fullmatch(r'(\d+)\s+курс', names[0].text() if len(names) == 1 else '')
            if not course_match:
                raise ValueError(f'Неизвестный раздел курса в {label}')
            course = int(course_match[1])
            for link in item.find(tag='a'):
                href = urlparse(link.attrs.get('href', ''))
                if href.netloc and href.netloc != urlparse(BASE).netloc:
                    continue
                group_match = re.fullmatch(r'/timetable/(\d+)', href.path)
                if not group_match:
                    continue
                group = {'id': group_match[1], 'name': link.text(), 'instituteId': institute_id, 'course': course}
                if not group['name']:
                    raise ValueError(f"Пустое название группы {group['id']}")
                if group['id'] in groups and groups[group['id']] != group:
                    raise ValueError(f"Противоречивые данные группы {group['id']}: {groups[group['id']]} / {group}")
                groups[group['id']] = group
    # Ensure no timetable link disappeared due to an unsupported course/block layout.
    official_ids = {m[1] for link in catalogs[0].find(tag='a')
                    if (m := re.fullmatch(r'/timetable/(\d+)', urlparse(link.attrs.get('href', '')).path))}
    if not groups or official_ids != set(groups):
        raise ValueError(f'Не все группы разобраны: {sorted(official_ids - set(groups))}')
    order = {institute['id']: index for index, institute in enumerate(institutes)}
    return {'schemaVersion': 2, 'universityId': UNIVERSITY, 'university': 'РУТ (МИИТ)',
            'updatedAt': datetime.now(ZoneInfo('Europe/Moscow')).isoformat(timespec='seconds'),
            'sourceUrl': BASE + '/timetable', 'institutes': institutes,
            'groups': sorted(groups.values(), key=lambda group: (order[group['instituteId']], group['course'], natural_key(group['name'])))}


def group_changes(previous, current):
    old = {group['id']: group for group in previous.get('groups', [])}
    new = {group['id']: group for group in current['groups']}
    return {'added': [new[key] for key in sorted(new.keys() - old.keys())],
            'removed': [old[key] for key in sorted(old.keys() - new.keys())],
            'renamed': [{'id': key, 'before': old[key]['name'], 'after': new[key]['name']}
                        for key in sorted(new.keys() & old.keys()) if new[key]['name'] != old[key]['name']]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--html', type=Path)
    parser.add_argument('--output', type=Path, default=ROOT / UNIVERSITY / 'groups.json')
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    current = parse_groups(args.html.read_text(encoding='utf-8') if args.html else fetch(BASE + '/timetable'))
    original = ROOT / UNIVERSITY / 'groups.json'
    previous = json.loads(original.read_text(encoding='utf-8')) if original.exists() else {}
    changes = group_changes(previous, current)
    changes['orphanTimetableIds'] = sorted(path.stem for path in (ROOT / UNIVERSITY / 'timetables').glob('*.json') if path.stem not in {g['id'] for g in current['groups']})
    write_result(args.output, current)
    if args.report:
        write_result(args.report, changes)
    print(f"Институтов: {len(current['institutes'])}; групп: {len(current['groups'])}; новых ID: {len(changes['added'])}; исчезнувших ID: {len(changes['removed'])}; переименованных: {len(changes['renamed'])}")
    print(f"Старые файлы, отсутствующие в индексе: {len(changes['orphanTimetableIds'])}; физически не удалены")


if __name__ == '__main__':
    main()
