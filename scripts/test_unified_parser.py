import contextlib
import io
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch
import moscow_rut_miit_unified_parser as parser
from test_parse_miit import page


class UnifiedTests(unittest.TestCase):
    def test_embedded_json_reader_preserves_brackets_inside_strings(self):
        data = [{'hisdate': '2026-10-06', 'text': 'Предмет [часть]'}]
        self.assertEqual(parser.extract_timetable_data('window._timetableData = ' + json.dumps(data) + ';'), data)

    def test_current_html_has_separate_weeks_and_period(self):
        html = page().replace('<a href="/timetable/193594?start=2026-06-01&type=1">Сессия</a>', '')
        with patch.object(parser, 'fetch_text', return_value=html):
            payload = parser.current_payload(parser.Group('193594', 'СЖД-441'), {}, [1, 4], date(2026, 10, 6))
        self.assertEqual(payload['semester']['startDate'], '2026-09-01')
        self.assertEqual(len(payload['lessons']['odd']), 1)
        self.assertEqual(len(payload['lessons']['even']), 1)
        self.assertEqual(payload['lessons']['odd'][0]['date'], '')

    def test_network_errors_are_not_successful_empty_files(self):
        with patch.object(parser, 'fetch_text', side_effect=OSError('timeout')):
            with self.assertRaisesRegex(RuntimeError, 'timeout'):
                parser.fetch_items_for_period(parser.Group('1','ТЕСТ-111'), date(2026,10,5), date(2026,10,5), [1], {}, 0)

    def test_smart_weeks_and_full_mode_remain_available(self):
        data = [{'hisdate':'2026-10-05', 'dayDisplay':'Понедельник', 'dayNumber':1,
                 'timeSlots':[{'slotStartDisplay':'09:00','slotEndDisplay':'10:00','events':[{'textTitle':'Экзамен','text':'Мосты'}]}]}]
        source = 'window._timetableData = ' + json.dumps(data)
        for smart, calls in [(True, 2), (False, 4)]:
            with patch.object(parser, 'fetch_text', return_value=source) as fetch:
                items = parser.fetch_items_for_period(parser.Group('1','ТЕСТ-111'), date(2026,10,5), date(2026,10,26), [1], {}, 0, smart)
            self.assertEqual(fetch.call_count, calls)
            self.assertEqual(len(items), 1)

    def test_exceptions_and_events_are_written_separately(self):
        group = parser.Group('1','ТЕСТ-111')
        items = []
        for kind in ['Экзамен', 'Зачёт', 'Консультация', 'Отмена занятия', 'Замена занятия']:
            data = [{'hisdate':'2026-10-06','dayDisplay':'Вторник','dayNumber':2,'timeSlots':[{'slotStartDisplay':'09:00','slotEndDisplay':'10:00','events':[{'textTitle':kind,'text':'Мосты'}]}]}]
            items.extend(parser.parse_items_from_timetable_data(data, group, 4, '', {}))
        with tempfile.TemporaryDirectory() as folder:
            path = parser.save_timetable(Path(folder), group, items, date(2026,9,1), date(2027,1,4), 'odd')
            payload = json.loads(path.read_text())
        self.assertEqual(len(payload['events']), 3)
        self.assertEqual(len(payload['exceptions']), 2)

    def test_dated_lessons_do_not_become_weekly_templates(self):
        html = page().replace('<a href="/timetable/193594?start=2026-06-01&type=1">Сессия</a>', '').replace('Периодическое', 'Разовое').replace('type=1', 'type=2')
        item = parser.html_item({'date':'2026-10-06', 'kind':'Лекция', 'subject':'Мосты'}, parser.Group('193594','СЖД-441'), 2, '', {})
        with patch.object(parser, 'fetch_text', return_value=html + '<script>window._timetableData = [];</script>'), patch.object(parser, 'parse_items_from_timetable_data', return_value=[item]):
            payload = parser.current_payload(parser.Group('193594','СЖД-441'), {}, [1,2,4], date(2026,10,6))
        self.assertEqual(payload['lessons'], {'odd': [], 'even': []})
        self.assertEqual(payload['events'][0]['date'], '2026-10-06')
        self.assertEqual(payload['events'][0]['category'], 'lesson')

    def test_full_report_counts_only_successes_and_retry_recovers_missing(self):
        from test_miit_groups import catalog
        def payload(group, *args):
            if group.id == '123':
                raise OSError('timeout')
            return {'semester': {}, 'groupId': group.id}
        with tempfile.TemporaryDirectory() as folder, contextlib.redirect_stdout(io.StringIO()):
            root = Path(folder) / parser.UNIVERSITY_ID
            (root / 'timetables').mkdir(parents=True)
            (root / 'timetables/123.json').write_text('{"old": true}')
            with patch('sys.argv', ['parser', '--all', '--output', folder, '--delay', '0']), patch.object(parser, 'fetch_text', return_value=catalog()), patch.object(parser, 'current_payload', side_effect=payload):
                self.assertEqual(parser.main(), 1)
            report = json.loads((root / 'parse-report.json').read_text())
            self.assertEqual(report['successfulFiles'], 3)
            self.assertEqual(report['missingIds'], ['123'])
            self.assertEqual(json.loads((root / 'timetables/123.json').read_text()), {'old': True})
            with patch('sys.argv', ['parser', '--retry-failed', '--output', folder, '--delay', '0']), patch.object(parser, 'fetch_text', return_value=catalog()), patch.object(parser, 'current_payload', return_value={'semester': {}, 'groupId': '123'}) as retry:
                self.assertEqual(parser.main(), 0)
                self.assertEqual(retry.call_count, 1)
            report = json.loads((root / 'parse-report.json').read_text())
            self.assertEqual(report['successfulFiles'], 4)
            self.assertEqual(report['runSuccessfulFiles'], 1)
            self.assertEqual(report['missingIds'], [])

    def test_future_period_is_saved_with_actual_dates(self):
        html = page().replace('<a href="/timetable/193594?start=2026-06-01&type=1">Сессия</a>', '').replace('2026-09-01', '2026-11-10').replace('01.09.2026', '10.11.2026')
        with patch.object(parser, 'fetch_text', return_value=html):
            payload = parser.current_payload(parser.Group('193594','СЖД-441'), {}, [1,2,4], date(2026,10,6))
        self.assertEqual(payload['semester']['startDate'], '2026-11-10')
        self.assertEqual(payload['meta']['scheduleStatus'], 'upcoming')

    def test_expired_period_does_not_replace_current_schedule(self):
        html = page().replace('<a href="/timetable/193594?start=2026-06-01&type=1">Сессия</a>', '').replace('04.01.2027', '01.10.2026')
        with patch.object(parser, 'fetch_text', return_value=html):
            with self.assertRaisesRegex(ValueError, 'действующего периода'):
                parser.current_payload(parser.Group('193594','СЖД-441'), {}, [1,2,4], date(2026,10,6))

    def test_tuesday_semester_start_anchors_parity_to_monday(self):
        self.assertEqual(parser.parity_for(date(2026,10,5), date(2026,9,1), 'odd'), 'even')


if __name__ == '__main__':
    unittest.main()
