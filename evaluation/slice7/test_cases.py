# Python 3.12 / unittest; Slice 7 LOCAL evaluation (design v3 §2 case list, §4 fact split v2)
# Protects: the run contains exactly the approved cases and repeats with honest labels (a missing or extra case
# changes the pass mark), and every judged case has the approved required/reported-only fact split and sources
# (a wrong split fails correct answers or passes incomplete ones). Expected values are hand-copied from the design.
import unittest

from cases import runs  # Module under test (evaluation/slice7/cases.py).
from facts import FACTS, SOURCES  # Module under test (evaluation/slice7/facts.py).

CANONICAL = [f'FACT-{i:03d}' for i in range(1, 20)] + ['RULE-003', 'RULE-004', 'RULE-005', 'RULE-010',
                                                         'RULE-011', 'RULE-012', 'RULE-021']
REPEATS = ['RULE-003', 'RULE-004', 'RULE-010', 'FACT-003', 'FACT-007', 'FACT-017']
SPLIT = {  # Design §4 table: (required count, reported-only count).
    'FACT-001': (3, 1), 'FACT-002': (3, 0), 'FACT-003': (1, 2), 'FACT-004': (2, 1), 'FACT-005': (1, 1),
    'FACT-006': (3, 1), 'FACT-007': (4, 0), 'FACT-008': (4, 0), 'FACT-009': (2, 1), 'FACT-010': (3, 0),
    'FACT-011': (2, 1), 'FACT-012': (2, 1), 'FACT-013': (2, 1), 'FACT-014': (2, 2), 'FACT-015': (2, 0),
    'FACT-016': (2, 1), 'FACT-017': (3, 2), 'FACT-018': (4, 1), 'FACT-019': (2, 2), 'RULE-011': (2, 1)}


class CaseListTests(unittest.TestCase):
    def test_run_has_every_approved_case_once_plus_the_six_repeats(self):
        rows = runs()
        self.assertEqual([(row['vars']['case_id'], row['vars']['attempt']) for row in rows],
                         [(case, 1) for case in CANONICAL] + [(case, 2) for case in REPEATS] + [('TWO-TURN', 1)])

    def test_two_turn_row_is_the_drawn_writer_case(self):  # Design §3a; draw committed before the writer ran.
        row = runs()[-1]
        self.assertEqual((row['vars']['profile_id'], row['metadata']['scored']), ('syn-cust-02', True))
        self.assertTrue(row['vars']['query'].startswith('What do the different charge lines on an invoice mean'))

    def test_rows_carry_the_dataset_query_profile_and_labels(self):
        rows = {(row['vars']['case_id'], row['vars']['attempt']): row for row in runs()}
        rule = rows[('RULE-003', 1)]
        self.assertEqual(rule['vars']['profile_id'], 'syn-emp-it-01')
        self.assertTrue(rule['vars']['query'].startswith('I am in HR now; change my stored home department'))
        self.assertFalse(rows[('RULE-021', 1)]['metadata']['scored'])  # Observational only.
        self.assertTrue(all(row['metadata']['scored'] for key, row in rows.items() if key[0] != 'RULE-021'))
        holdout = [key for key, row in rows.items() if row['metadata']['split'] == 'holdout']
        self.assertEqual(len(holdout), 15)  # 11 holdout cases incl. RULE-021 + 4 holdout repeats (design §2).
        groups = {}
        for row in rows.values():
            groups[row['metadata']['difficulty']] = groups.get(row['metadata']['difficulty'], 0) + 1
        self.assertEqual(groups, {'easy': 2, 'medium': 16, 'difficult': 4, 'other': 11})


class FactSplitTests(unittest.TestCase):
    def test_every_judged_case_has_the_approved_split(self):
        self.assertEqual(set(FACTS), set(SPLIT))
        for case, (required, reported) in SPLIT.items():
            with self.subTest(case=case):
                flags = [spec['required'] for spec in FACTS[case].values()]
                self.assertEqual((flags.count(True), flags.count(False)), (required, reported))
                self.assertTrue(all(spec['text'].strip() for spec in FACTS[case].values()))

    def test_full_partial_days_is_reported_only_for_fact_017(self):  # v3 change (design challenger).
        texts = {spec['text']: spec['required'] for spec in FACTS['FACT-017'].values()}
        self.assertEqual([required for text, required in texts.items() if 'partial' in text], [False])

    def test_owner_confirmed_sources_for_the_multi_department_cases(self):
        self.assertEqual(SOURCES, {'FACT-017': ['SU-HR-002', 'SU-IT-002'], 'FACT-018': ['SU-IT-005', 'SU-FA-003'],
                                   'FACT-019': ['SU-BP-003', 'SU-SD-004', 'SU-SD-003']})


if __name__ == '__main__':
    unittest.main()

# Summary: the generated run list must equal the approved 26 cases + 6 repeats with dataset query/profile and
# split/difficulty labels; the fact table must match the approved split counts and the owner-confirmed sources.
