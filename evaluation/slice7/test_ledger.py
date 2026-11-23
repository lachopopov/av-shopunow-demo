# Python 3.12 / unittest; Slice 7 LOCAL evaluation (design v3 §7, test 4)
# Protects: the paid run never passes the owner's $5 stop, never counts missing usage as free, and stays
# correct when the provider and two judge assertions (separate processes) record costs at the same time.
import multiprocessing  # Real concurrent writers, as Promptfoo's separate Python processes would be.
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from ledger import Ledger  # Module under test (evaluation/slice7/ledger.py).


def add_cents(path, times):
    ledger = Ledger(path)
    for _ in range(times):
        ledger.record('app', Decimal('0.01'))


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / 'ledger.json'

    def tearDown(self):
        self.directory.cleanup()

    def test_case_runs_only_if_spent_plus_fixed_reservation_fits_the_stop(self):
        ledger = Ledger(self.path)
        ledger.record('app', Decimal('4.60'))
        self.assertTrue(ledger.reserve_case('FACT-001'))  # 4.60 + 0.40 = 5.00: allowed.
        ledger.record('app', Decimal('0.01'))
        self.assertFalse(ledger.reserve_case('FACT-002'))  # 4.61 + 0.40 > 5.00: refused.

    def test_missing_usage_is_unknown_not_zero(self):
        ledger = Ledger(self.path)
        ledger.record('app', Decimal('0.10'))
        ledger.record('judge', None)
        totals = ledger.totals()
        self.assertEqual(totals['known_usd'], Decimal('0.10'))
        self.assertEqual(totals['unknown_items'], 1)
        self.assertIsNone(totals['total_usd'])  # Never a total that silently treats unknown as $0.

    def test_state_is_shared_and_safe_across_concurrent_processes(self):
        Ledger(self.path)  # Create the file once.
        workers = [multiprocessing.Process(target=add_cents, args=(self.path, 50)) for _ in range(2)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        self.assertEqual(Ledger(self.path).totals()['known_usd'], Decimal('1.00'))  # 100 x $0.01, none lost.

    def test_judge_calls_stop_at_the_cap(self):
        ledger = Ledger(self.path, judge_cap=3)
        self.assertEqual([ledger.reserve_judge() for _ in range(4)], [True, True, True, False])

    def test_judge_call_is_refused_when_it_would_pass_the_stop(self):
        ledger = Ledger(self.path)
        ledger.record('app', Decimal('4.96'))
        self.assertFalse(ledger.reserve_judge())  # 4.96 + 0.05 > 5.00.

    def test_unknown_usage_stops_new_cases_and_judge_calls(self):  # Code review #5: fail closed.
        ledger = Ledger(self.path)
        ledger.record('app', None)
        self.assertFalse(ledger.reserve_case('FACT-001#1'))
        self.assertFalse(ledger.reserve_judge())

    def test_totals_report_run_validity_and_judge_calls(self):  # Challenger LOW.
        ledger = Ledger(self.path)
        ledger.reserve_judge()
        for case in ('a#1', 'b#1', 'c#1', 'd#1'):
            ledger.judge_invalid(case)
        totals = ledger.totals()
        self.assertEqual((totals['judge_calls'], totals['run_invalid']), (1, True))

    def test_calibration_spend_and_judge_calls_carry_into_the_main_ledger(self):  # Design §7, challenger LOW.
        calibration = Ledger(self.path.with_name('calibration.json'))
        calibration.record('judge', Decimal('0.04'))
        calibration.reserve_judge()
        main = Ledger(self.path)
        main.carry_over(calibration.path)
        totals = main.totals()
        self.assertEqual((totals['known_usd'], totals['judge_calls']), (Decimal('0.04'), 1))

    def test_more_than_three_invalid_judge_results_make_the_run_invalid(self):
        ledger = Ledger(self.path)
        for _ in range(3):
            ledger.judge_invalid('FACT-001')
        self.assertFalse(ledger.run_invalid())
        ledger.judge_invalid('FACT-002')
        self.assertTrue(ledger.run_invalid())


if __name__ == '__main__':
    unittest.main()

# Summary: literal dollar amounts check the $5 stop with the fixed reservations, unknown usage stays unknown,
# two real processes add costs concurrently without loss, and the judge cap and invalid-run rule hold.
