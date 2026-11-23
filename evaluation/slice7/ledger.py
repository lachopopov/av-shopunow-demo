# Python 3.12 stdlib; Slice 7 LOCAL evaluation — shared cost ledger (design v3 §7)
# One JSON file guarded by an exclusive file lock, so the provider and the judge assertions (separate processes)
# see one running total. Amounts are Decimal strings; missing usage is counted as unknown, never as $0.
import fcntl  # POSIX advisory lock: one writer at a time across processes.
import json
import os
from contextlib import contextmanager
from decimal import Decimal

STOP_USD = Decimal('5.00')  # Owner-approved hard stop for app + judge + calibration (plan D6).
CASE_RESERVATION_USD = Decimal('0.40')  # Fixed: $0.30 app + 2 x $0.05 judge (design challenger M6).
JUDGE_RESERVATION_USD = Decimal('0.05')
INVALID_JUDGE_LIMIT = 3  # More than this many invalid judge results make the run invalid (design v3 §5).


class Ledger:
    def __init__(self, path, *, stop=STOP_USD, judge_cap=90):
        self.path, self.stop, self.judge_cap = str(path), stop, judge_cap
        with self._locked():
            pass  # Creates the file on first use.

    @contextmanager
    def _locked(self):
        descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)  # Private to the owner.
        with os.fdopen(descriptor, 'r+', encoding='utf-8') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)  # Released when the file closes.
            text = handle.read()
            state = json.loads(text) if text else {'known_usd': '0', 'unknown_items': 0, 'items': [],
                                                   'cases': [], 'judge_calls': 0, 'judge_invalid': []}
            yield state
            handle.seek(0)
            handle.truncate()
            json.dump(state, handle, indent=1)

    def record(self, kind, amount):
        """Add one priced item; amount None means the provider reported no usage (unknown)."""
        with self._locked() as state:
            if amount is None:
                state['unknown_items'] += 1
            else:
                state['known_usd'] = str(Decimal(state['known_usd']) + amount)
            state['items'].append({'kind': kind, 'usd': None if amount is None else str(amount)})

    def reserve_case(self, case_id):
        """True if this case may start: spent + the fixed case reservation stays within the stop."""
        with self._locked() as state:
            # Fail closed (code review #5): once any usage is unknown, spend can no longer be bounded.
            allowed = state['unknown_items'] == 0 and Decimal(state['known_usd']) + CASE_RESERVATION_USD <= self.stop
            if allowed:
                state['cases'].append(case_id)
            return allowed

    def reserve_judge(self):
        """True if one more judge call fits both the call cap and the dollar stop."""
        with self._locked() as state:
            allowed = (state['unknown_items'] == 0 and state['judge_calls'] < self.judge_cap
                       and Decimal(state['known_usd']) + JUDGE_RESERVATION_USD <= self.stop)
            if allowed:
                state['judge_calls'] += 1
            return allowed

    def judge_invalid(self, case_id):
        with self._locked() as state:
            state['judge_invalid'].append(case_id)

    def run_invalid(self):
        with self._locked() as state:
            return len(state['judge_invalid']) > INVALID_JUDGE_LIMIT

    def carry_over(self, path):
        """Add an earlier ledger (calibration) so the $5 stop and judge cap cover app + judge + calibration."""
        earlier = Ledger(path).totals()
        with self._locked() as state:
            state['known_usd'] = str(Decimal(state['known_usd']) + earlier['known_usd'])
            state['unknown_items'] += earlier['unknown_items']
            state['judge_calls'] += earlier['judge_calls']
            state['items'].append({'kind': 'carried_over', 'usd': str(earlier['known_usd']), 'from': str(path)})

    def totals(self):
        with self._locked() as state:
            known = Decimal(state['known_usd'])
            return {'known_usd': known, 'unknown_items': state['unknown_items'],
                    'total_usd': known if state['unknown_items'] == 0 else None,
                    'judge_calls': state['judge_calls'], 'judge_invalid': list(state['judge_invalid']),
                    'run_invalid': len(state['judge_invalid']) > INVALID_JUDGE_LIMIT}


# Flow: every reservation and cost goes through one locked read-modify-write of the ledger file ->
# cases and judge calls start only within the $5 stop -> totals stay "unknown" if any usage was missing.
