import unittest
from miit_groups import group_changes, parse_groups


def catalog():
    return '''<section class="timetable-catalog"><div id="ИСТИ" class="info-block"><span class="info-block__header-text">Институт строительства транспортной инфраструктуры (ИСТИ)</span><ul><li class="text-form__item"><span class="text-form__item-name">3 курс</span><div class="dropdown"><a href="#">СЖД</a><div class="dropdown-menu"><a href="/timetable/193600">СЖД-341</a><a href="/timetable/193603">СЖД-342</a></div></div></li><li class="text-form__item"><span class="text-form__item-name">1 курс</span><a href="/timetable/123">НЕСТАНДАРТНОЕ-110</a><a href="/timetable/124">НЕСТАНДАРТНОЕ-12</a></li></ul></div></section>'''


class GroupTests(unittest.TestCase):
    def test_source_hierarchy_and_dropdowns(self):
        data = parse_groups(catalog())
        self.assertEqual(data['schemaVersion'], 2)
        self.assertEqual(data['institutes'], [{'id': 'isti', 'name': 'Институт строительства транспортной инфраструктуры', 'shortName': 'ИСТИ'}])
        self.assertEqual([group['name'] for group in data['groups']], ['НЕСТАНДАРТНОЕ-12', 'НЕСТАНДАРТНОЕ-110', 'СЖД-341', 'СЖД-342'])
        self.assertEqual(data['groups'][0]['instituteId'], 'isti')
        self.assertEqual(data['groups'][0]['course'], 1)

    def test_conflicting_group_membership_is_not_guessed(self):
        with self.assertRaisesRegex(ValueError, 'Противоречивые'):
            parse_groups(catalog().replace('/timetable/123', '/timetable/193600'))

    def test_unsupported_course_fails_instead_of_skipping_groups(self):
        with self.assertRaisesRegex(ValueError, 'курс'):
            parse_groups(catalog().replace('3 курс', 'Магистратура'))

    def test_changes_use_ids_and_preserve_rename_information(self):
        before = {'groups': [{'id': '1', 'name': 'OLD'}, {'id': '2', 'name': 'RENAMED'}]}
        after = {'groups': [{'id': '2', 'name': 'NEWNAME'}, {'id': '3', 'name': 'NEW'}]}
        changes = group_changes(before, after)
        self.assertEqual(changes['added'][0]['id'], '3')
        self.assertEqual(changes['removed'][0]['id'], '1')
        self.assertEqual(changes['renamed'], [{'id': '2', 'before': 'RENAMED', 'after': 'NEWNAME'}])


if __name__ == '__main__':
    unittest.main()
