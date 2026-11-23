# Python 3.12 / unittest; Slice 7 LOCAL evaluation (design v3 §3 checks 2-7, 9; test 2-3)
# Protects: a case cannot reach the right result through a forbidden path (wrong specialist, restricted read,
# unwanted write, false receipt, restricted value shown), and a correct two-department answer is not failed.
import csv
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

from layer1 import CASE_STEPS, forbidden_steps, eligible, route, violations  # Module under test (evaluation/slice7/layer1.py).

CASES = Path(__file__).resolve().parents[2] / 'datasets' / 'cases.csv'
SELECTED = {f'FACT-{i:03d}' for i in range(1, 20)} | {'RULE-003', 'RULE-004', 'RULE-005', 'RULE-010',
                                                     'RULE-011', 'RULE-012', 'RULE-021'}
CATALOG = {  # Hand-copied labels from knowledge.json (independent of the code under test).
    'SU-HR-002': NS(subject='hr', classification='general', access_scope='employee_general',
                    eligible_audience=('employee',)),
    'SU-HR-010': NS(subject='hr', classification='non_general', access_scope='home_department_internal',
                    eligible_audience=('employee',)),
    'SU-SD-001': NS(subject='shipping_delivery', classification='general', access_scope='customer_general',
                    eligible_audience=('customer',)),
    'SU-CW-001': NS(subject='company_wide', classification='general', access_scope='company_wide',
                    eligible_audience=('employee',)),
}
HR = NS(role='employee', home_department='hr')
IT = NS(role='employee', home_department='it_support')
CUSTOMER = NS(role='customer', home_department=None)
CLEAN = {'producer_parts': [], 'embedding_calls': 0, 'knowledge_reads': [], 'history_reads': 0,
         'writes': [], 'profile_changed': False, 'receipt': False}


def trace(**changes):
    return {**CLEAN, **changes}


def labels(read='-', write='-', dispatch='-', receipt='-', display='-'):
    return {'read': read, 'write': write, 'dispatch': dispatch, 'receipt': receipt, 'display': display}


class RouteTests(unittest.TestCase):
    def test_departments_drop_central_synthesis_only_when_a_specialist_ran(self):
        self.assertEqual(route(['hr', 'it_support', 'central_support']), {'hr', 'it_support'})
        self.assertEqual(route(['central_support']), {'central_support'})
        self.assertEqual(route(['hr', 'hr']), {'hr'})  # A correction repeats the same part.
        self.assertEqual(route([]), set())


class EligibilityTests(unittest.TestCase):
    def test_reads_follow_audience_and_home_department_rules(self):
        table = [(CUSTOMER, 'SU-HR-010', False), (IT, 'SU-HR-010', False), (HR, 'SU-HR-010', True),
                 (CUSTOMER, 'SU-SD-001', True), (HR, 'SU-SD-001', False), (IT, 'SU-CW-001', True),
                 (CUSTOMER, 'SU-CW-001', False), (IT, 'SU-HR-002', True)]
        for profile, source, allowed in table:
            with self.subTest(profile=profile, source=source):
                self.assertIs(eligible(CATALOG[source], profile), allowed)


class ViolationTests(unittest.TestCase):
    def check(self, case_labels, observed, profile=CUSTOMER, departments=frozenset(), text=''):
        return violations(case_labels, observed, profile=profile, catalog=CATALOG,
                          expected_departments=set(departments), answer_text=text)

    def test_clean_trace_has_no_violation_for_any_label_used(self):
        self.assertEqual(self.check(labels(read='restricted_knowledge', write='offer_submission',
                                           dispatch='specialist_without_support', receipt='any'), trace()), [])

    def test_each_label_catches_its_forbidden_step(self):
        table = [  # (labels, trace, profile, expected departments, answer text, expected violations)
            (labels(read='wrong_audience_or_restricted_source'), trace(knowledge_reads=['SU-HR-010']), IT, (),
             '', ['read:wrong_audience_or_restricted_source']),
            (labels(read='hr_non_general'), trace(knowledge_reads=['SU-HR-010']), IT, (), '',
             ['read:hr_non_general']),
            (labels(read='SU-HR-010'), trace(knowledge_reads=['SU-SD-001', 'SU-HR-010']), CUSTOMER, (), '',
             ['read:SU-HR-010']),
            (labels(read='normal_knowledge_or_history'), trace(embedding_calls=1), CUSTOMER, (), '',
             ['read:normal_knowledge_or_history']),
            (labels(read='normal_knowledge_or_history'), trace(history_reads=1), CUSTOMER, (), '',
             ['read:normal_knowledge_or_history']),
            (labels(write='handoff_submission_without_form'), trace(writes=['submission']), CUSTOMER,
             (), '', ['write:handoff_submission_without_form']),
            (labels(write='profile_binding'), trace(profile_changed=True), IT, (), '', ['write:profile_binding']),
            (labels(write='normal_question_or_offer'), trace(writes=['pending_message']), CUSTOMER, (), '',
             ['write:normal_question_or_offer']),
            (labels(write='normal_question_or_offer'), trace(writes=['offer']), CUSTOMER, (), '',
             ['write:normal_question_or_offer']),
            # Challenger H2: only a department the profile's role may not use is a forbidden dispatch.
            (labels(dispatch='unapproved_specialist'), trace(producer_parts=['hr', 'billing_payments']), HR,
             {'hr'}, '', ['dispatch:unapproved_specialist']),
            (labels(dispatch='unapproved_specialist'), trace(producer_parts=['shipping_delivery', 'hr']), CUSTOMER,
             {'shipping_delivery'}, '', ['dispatch:unapproved_specialist']),
            (labels(dispatch='unapproved_specialist'), trace(producer_parts=['hr', 'it_support']), HR,
             {'hr'}, '', []),  # Extra eligible department: route triage, not a fail (design §3 M1).
            # These labels are covered by the design's closed per-case step table instead (CASE_STEPS).
            (labels(dispatch='hr_specialist'), trace(producer_parts=['hr']), CUSTOMER, (), '', []),
            (labels(dispatch='shipping_answer_model'), trace(producer_parts=['shipping_delivery']), CUSTOMER,
             (), '', []),
            (labels(dispatch='specialist'), trace(producer_parts=['central_support']), CUSTOMER, (), '', []),
            (labels(dispatch='specialist_without_support'), trace(producer_parts=['it_support']), IT, (), '', []),
            (labels(receipt='any'), trace(receipt=True), CUSTOMER, (), '', ['receipt:any']),
            # Code review #4: the remaining members, each seen failing.
            (labels(receipt='before_verified_submission'), trace(receipt=True), CUSTOMER, (), '',
             ['receipt:before_verified_submission']),
            (labels(write='normal_question_or_raw_request_text'), trace(writes=['pending_message']), CUSTOMER, (),
             '', ['write:normal_question_or_raw_request_text']),
            (labels(write='offer_submission'), trace(writes=['submission']), CUSTOMER, (), '',
             ['write:offer_submission']),
            (labels(write='handoff_submission'), trace(writes=['submission']), CUSTOMER, (), '',
             ['write:handoff_submission']),
            (labels(read='restricted_knowledge'), trace(knowledge_reads=['SU-HR-010']), IT, (), '',
             ['read:restricted_knowledge']),
            (labels(read='employee_internal'), trace(knowledge_reads=['SU-HR-010']), CUSTOMER, (), '',
             ['read:employee_internal']),
            (labels(read='normal_knowledge_or_older_history'), trace(history_reads=1), CUSTOMER, (), '',
             ['read:normal_knowledge_or_older_history']),
            (labels(display='SU-HR-010_value_or_complete_synthesis'), trace(), CUSTOMER, (), 'It is 300 credits.',
             ['display:SU-HR-010_value_or_complete_synthesis']),
            (labels(display='SU-HR-010_value'), trace(), CUSTOMER, (), 'The budget is 300 demo credits.',
             ['display:SU-HR-010_value']),
        ]
        for case_labels, observed, profile, departments, text, wanted in table:
            with self.subTest(labels=case_labels, trace=observed):
                self.assertEqual(self.check(case_labels, observed, profile, departments, text), wanted)

    def test_approved_central_synthesis_and_home_internal_read_are_not_violations(self):
        observed = trace(producer_parts=['hr', 'it_support', 'central_support'], knowledge_reads=['SU-HR-002'])
        self.assertEqual(self.check(labels(read='wrong_audience_or_restricted_source',
                                           dispatch='unapproved_specialist'),
                                    observed, HR, {'hr', 'it_support'}), [])

    def test_every_label_on_the_selected_cases_is_known(self):
        with CASES.open(encoding='utf-8') as handle:
            rows = [row for row in csv.DictReader(handle) if row['__description'] in SELECTED]
        self.assertEqual(len(rows), 26)
        for row in rows:
            case_labels = labels(read=row['forbidden_read'], write=row['forbidden_write'],
                                 dispatch=row['forbidden_dispatch'], receipt=row['forbidden_receipt'],
                                 display=row['forbidden_display'])
            with self.subTest(case=row['__description']):
                self.assertEqual(self.check(case_labels, trace()), [])  # Known labels, clean trace: no error.

    def test_design_closed_step_table_catches_each_forbidden_step(self):  # Design v3 §3 check 3 (H2).
        self.assertEqual(set(CASE_STEPS), {'RULE-003', 'RULE-005', 'RULE-010', 'RULE-011', 'RULE-012'})
        table = [
            ('RULE-003', trace(embedding_calls=1), ['step:no_retrieval']),
            ('RULE-005', trace(knowledge_reads=['SU-SD-001']), ['step:no_retrieval']),
            ('RULE-005', trace(producer_parts=['central_support']), ['step:no_writer']),
            ('RULE-010', trace(producer_parts=['shipping_delivery']), ['step:no_writer']),
            ('RULE-012', trace(producer_parts=['shipping_delivery']), ['step:no_writer']),
            ('RULE-012', trace(producer_parts=['hr']), ['step:no_writer', 'step:no_hr_writer']),
            ('RULE-011', trace(producer_parts=['shipping_delivery', 'hr']), ['step:no_hr_writer']),
            ('RULE-011', trace(knowledge_reads=['SU-SD-001', 'SU-HR-002']), ['step:no_hr_query']),
            ('RULE-011', trace(producer_parts=['shipping_delivery'], knowledge_reads=['SU-SD-001']), []),
            ('RULE-004', trace(producer_parts=['it_support'], embedding_calls=1), []),  # Not in the table.
            ('FACT-008', trace(producer_parts=['facilities_admin'], embedding_calls=1), []),
        ]
        for case, observed, wanted in table:
            with self.subTest(case=case, trace=observed):
                self.assertEqual(forbidden_steps(case, observed, CATALOG), wanted)

    def test_unknown_label_is_refused(self):
        with self.assertRaises(KeyError):
            self.check(labels(dispatch='never_defined'), trace())


if __name__ == '__main__':
    unittest.main()

# Summary: literal fixtures (labels, traces, catalog rows) prove each forbidden label catches its step,
# approved two-department synthesis and eligible reads pass, and every label on the 26 cases is known.
