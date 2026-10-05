import unittest
import contextlib
import io
import json
import tempfile
from datetime import date
from pathlib import Path
from unittest.mock import patch
from parse_miit import Document, main, parse_regular, period_links, pick_period, update_group


def page():
    # Two rendering variants exist on MIIT; read only the desktop table.
    cell = '''<td class="timetable__grid-day"><div class="timetable__grid-day-lesson"><span class="timetable__grid-text_gray">Лекция</span>Мосты</div><a class="icon-academic-cap" title="Мазур Евгений Витальевич, к.т.н.">Мазур Е.В.</a><a class="icon-location" title="РОАТ, Часовая 1, 105">Аудитория 105</a></td>'''
    table = f'''<table class="timetable__grid"><tr><th></th><th>Понедельник<small>12 октября</small></th><th>Вторник</th></tr><tr><td>1 пара<div>08:30 — 09:50</div></td><td class="timetable__grid-day"><div class="timetable__grid-day-lesson"></div></td>{cell}</tr></table>'''
    return f'''<div class="page-header-addendum"><a href="/timetable/193594?start=2026-06-01&type=1">Сессия</a><a href="/timetable/193594?start=2026-09-01&type=1">Периодическое</a></div>Расписание действует с 01.09.2026 по 04.01.2027<div id="week-1"><div class="d-md-none">Лекция Мосты</div>{table}</div><div id="week-2">{table}</div>'''


class ParserTests(unittest.TestCase):
    def test_current_period_is_selected_by_label_not_type(self):
        links = period_links(Document(page()).root, '193594')
        self.assertEqual(pick_period(links, date(2026, 10, 5))['start'], '2026-09-01')

    def test_extracts_both_weeks_without_mobile_duplicates(self):
        result = parse_regular(page(), '193594', 'СЖД-441', 'https://www.miit.ru/timetable/193594')
        self.assertEqual(result['semester']['endDate'], '2027-01-04')
        for parity in ['odd', 'even']:
            self.assertEqual(len(result['lessons'][parity]), 1)
            entry = result['lessons'][parity][0]
            self.assertEqual(entry['weekdayNumber'], 2)
            self.assertEqual(entry['subject'], 'Мосты')
            self.assertEqual(entry['teacher'], 'Мазур Евгений Витальевич')
            self.assertEqual(entry['room'], '105')
            self.assertEqual(entry['address'], 'РОАТ, Часовая 1')
            self.assertEqual(entry['startTime'], '08:30')
            self.assertNotIn('date', entry)

    def test_session_page_cannot_replace_regular_lessons(self):
        with self.assertRaises(ValueError):
            parse_regular('Расписание действует с 01.06.2026 по 30.06.2026', '193594', 'СЖД-441', '')

    def test_period_selection_rejects_session_and_future_only(self):
        with self.assertRaises(ValueError):
            pick_period([{'start': '2026-06-01', 'label': 'Сессия'}], date(2026, 10, 5))
        with self.assertRaises(ValueError):
            pick_period([{'start': '2027-02-01', 'label': 'Периодическое'}], date(2026, 10, 5))

    def test_nested_subject_and_duplicate_kind(self):
        html = page().replace('Лекция</span>Мосты', 'Лекция, Лекция</span><a href="/subject/1"><strong>Мосты</strong> и тоннели</a>')
        result = parse_regular(html, '193594', 'СЖД-441', '')
        entry = result['lessons']['odd'][0]
        self.assertEqual(entry['subject'], 'Мосты и тоннели')
        self.assertEqual(entry['kind'], 'Лекция')

    def test_different_lessons_in_one_cell_keep_their_teachers_and_rooms(self):
        html = page().replace('Аудитория 105</a></td>', '''Аудитория 105</a><div class="timetable__grid-day-lesson"><span class="timetable__grid-text_gray">Практическое занятие</span>Тоннели</div><a class="icon-academic-cap" title="Филаткин Андрей Сергеевич">Филаткин А.С.</a><a class="icon-location" title="РОАТ, Часовая 1, 356">Аудитория 356</a></td>''')
        lessons = parse_regular(html, '193594', 'СЖД-441', '')['lessons']['odd']
        self.assertEqual([(e['subject'], e['teacher'], e['room']) for e in lessons], [
            ('Мосты', 'Мазур Евгений Витальевич', '105'),
            ('Тоннели', 'Филаткин Андрей Сергеевич', '356'),
        ])

    def test_external_period_links_are_ignored(self):
        html = page().replace('href="/timetable/193594?start=2026-09-01', 'href="https://example.com/timetable/193594?start=2026-09-01')
        self.assertEqual(len(period_links(Document(html).root, '193594')), 1)

    def test_redirected_period_is_rejected(self):
        html = page().replace('01.09.2026', '01.08.2026')
        with patch('parse_miit.fetch', return_value=html):
            with self.assertRaisesRegex(ValueError, 'другой период'):
                update_group({'id': '193594', 'name': 'СЖД-441'}, date(2026, 10, 5))

    def test_unknown_group_is_not_silently_skipped(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as error:
                main(['--group', 'СЖД-441', '--group', 'НЕСУЩЕСТВУЮЩАЯ'])
        self.assertEqual(error.exception.code, 2)

    def test_failed_update_preserves_existing_json(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / '193594.json'
            destination.write_text('{"original":true}', encoding='utf-8')
            with patch('parse_miit.update_group', side_effect=ValueError('Нет действующего расписания')):
                with contextlib.redirect_stderr(io.StringIO()):
                    result = main(['--group', '193594', '--output', temporary, '--date', '2026-10-05'])
            self.assertEqual(result, 1)
            self.assertEqual(json.loads(destination.read_text()), {'original': True})

    def test_offline_expired_period_is_rejected_without_overwrite(self):
        with tempfile.TemporaryDirectory() as temporary:
            html_path = Path(temporary) / 'page.html'
            html_path.write_text(page(), encoding='utf-8')
            destination = Path(temporary) / '193594.json'
            destination.write_text('{"original":true}', encoding='utf-8')
            with contextlib.redirect_stderr(io.StringIO()):
                result = main(['--group', '193594', '--html', str(html_path), '--output', temporary, '--date', '2027-06-01'])
            self.assertEqual(result, 1)
            self.assertEqual(json.loads(destination.read_text()), {'original': True})


if __name__ == '__main__':
    unittest.main()
