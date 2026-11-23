# Python 3.12 / unittest; Slice 7 LOCAL evaluation (design v3 §5 calibration)
# Protects: calibration uses exactly the approved 11 variants on dev cases (never a holdout case), each with an
# expected verdict for every check; the judge sees the same input shape as in the main run; agreement is scored
# per check, and an invalid judge result never counts as agreement (100 % bar).
import unittest

import calibration  # Module under test (evaluation/slice7/calibration.py).
from cases import rows

CHECKS = {'correct', 'grounded', 'required_facts', 'forbidden_claims', 'relevant'}


def verdict(failed=()):
    return {'status': 'judged', 'pass': not failed, 'failed_checks': list(failed), 'coverage': '1/1'}


class VariantTableTests(unittest.TestCase):
    def test_eleven_variants_six_must_pass_five_must_fail_on_dev_cases_only(self):
        variants = calibration.VARIANTS
        self.assertEqual([variant['id'] for variant in variants], ['V%02d' % number for number in range(1, 12)])
        passing = [variant['id'] for variant in variants if all(variant['expected'].values())]
        self.assertEqual(passing, ['V01', 'V02', 'V03', 'V04', 'V05', 'V06'])
        for variant in variants:
            with self.subTest(variant=variant['id']):
                self.assertEqual(set(variant['expected']), CHECKS)  # Every check has an expected verdict.
                if variant['case'] in rows():
                    self.assertIn(variant['case'], ('FACT-008', 'FACT-018'))
                    self.assertEqual(rows()[variant['case']]['__metadata:split'], 'dev')  # Never holdout.
                else:
                    self.assertEqual(variant['case'], 'NX-4657')  # The real earlier V4 answer (variant 5).

    def test_must_fail_variants_target_the_design_checks(self):
        expected_failures = {'V07': {'correct'}, 'V08': {'required_facts'}, 'V09': {'grounded', 'forbidden_claims'},
                             'V10': {'required_facts'}, 'V11': {'required_facts'}}
        for variant in calibration.VARIANTS[6:]:
            with self.subTest(variant=variant['id']):
                failing = {check for check, ok in variant['expected'].items() if not ok}
                self.assertTrue(expected_failures[variant['id']] <= failing)


class JudgeInputTests(unittest.TestCase):
    def test_dev_variant_gets_own_plus_co_cleared_sources_and_the_split(self):
        case, answer = calibration.variant_case(calibration.VARIANTS[1])  # V02: extra detail from SU-IT-001.
        self.assertEqual(list(case['sources']), ['SU-IT-005', 'SU-FA-003', 'SU-IT-001'])
        self.assertEqual(case['forbidden'], ['device_repaired_or_display_fixed'])
        self.assertEqual(sum(spec['required'] for spec in case['facts'].values()), 4)
        self.assertIn('asset label', answer)

    def test_v4_variant_uses_its_own_case_data(self):
        case, answer = calibration.variant_case(calibration.VARIANTS[4])
        self.assertEqual(list(case['sources']), ['SU-BP-004'])
        self.assertTrue(answer.startswith('On the sample invoice:'))  # Sol's real V4 answer, verbatim.


class AgreementTests(unittest.TestCase):
    def test_agreement_is_per_check_and_invalid_never_agrees(self):
        must_pass, missing_fact = calibration.VARIANTS[0], calibration.VARIANTS[7]
        self.assertTrue(calibration.agreement(must_pass, verdict())['agree'])
        disagreed = calibration.agreement(must_pass, verdict(['grounded']))
        self.assertEqual((disagreed['agree'], disagreed['mismatches']), (False, ['grounded']))
        self.assertTrue(calibration.agreement(missing_fact, verdict(['required_facts']))['agree'])
        invalid = calibration.agreement(must_pass, {'status': 'JUDGE_INVALID', 'reason': 'JUDGE_INVALID: x'})
        self.assertEqual((invalid['agree'], invalid['mismatches']), (False, ['JUDGE_INVALID']))


class RunTests(unittest.TestCase):
    def test_run_judges_every_variant_once_and_saves_a_private_summary(self):
        import json
        import os
        import tempfile
        from pathlib import Path
        from unittest import mock
        run_dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        with mock.patch('assertions.judge_client'), \
                mock.patch('judge.judge_answer', return_value=verdict()) as judged:
            calibration.main(run_dir)
        self.assertEqual(judged.call_count, 11)
        summary = json.loads((run_dir / 'calibration-summary.json').read_text())
        self.assertEqual((summary['agreed'], summary['variants']), (6, 11))  # All-pass verdict: only V01-V06 agree.
        self.assertEqual(os.stat(run_dir / 'calibration-summary.json').st_mode & 0o077, 0)


if __name__ == '__main__':
    unittest.main()

# Summary: the variant table must match design §5 (11 variants, 6 pass / 5 fail, dev cases + one V4 answer,
# every check expected); judge inputs reuse the main-run shape; agreement compares every check and rejects invalid.
