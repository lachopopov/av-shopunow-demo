# Python 3.12 stdlib; Slice 7 LOCAL evaluation — the approved run list (design v3 §2, plan D6)
# Reads the canonical dataset (outside this repo, next to it) and emits Promptfoo test rows. Only `query` and
# `profile_id` are app inputs; `case_id`/`attempt` let the provider and assertions find evaluator-side data.
import csv
import json
import sys
from pathlib import Path

import two_turn  # The drawn two-turn writer case (design v3 §3a).

DATASET = Path(__file__).resolve().parents[2] / 'datasets' / 'cases.csv'  # Owner-approved dataset; git-ignored, never published.
CANONICAL = [f'FACT-{i:03d}' for i in range(1, 20)] + ['RULE-003', 'RULE-004', 'RULE-005', 'RULE-010',
                                                         'RULE-011', 'RULE-012']
OBSERVATIONAL = ['RULE-021']  # Run once, reported, not scored (a live run cannot force `uncertain`).
REPEATS = ['RULE-003', 'RULE-004', 'RULE-010', 'FACT-003', 'FACT-007', 'FACT-017']  # Fresh state and identity.


def rows():
    """Dataset rows by case ID."""
    with DATASET.open(encoding='utf-8') as handle:
        return {row['__metadata:id']: row for row in csv.DictReader(handle)}


def runs():
    """Promptfoo tests: each approved case once, then the six repeats (attempt 2)."""
    data = rows()
    order = [(case, 1) for case in CANONICAL + OBSERVATIONAL] + [(case, 2) for case in REPEATS]
    return [{'description': '%s #%d' % (case, attempt),
             'vars': {'query': data[case]['query'], 'profile_id': data[case]['__metadata:profile_id'],
                      'case_id': case, 'attempt': attempt},
             'metadata': {'split': data[case]['__metadata:split'],
                          'difficulty': data[case]['__metadata:intended_workflow'],  # easy/medium/difficult/other.
                          'scored': case not in OBSERVATIONAL}}
            for case, attempt in order] + [two_turn_row()]


def two_turn_row():
    """The moved two-turn support case: turn 1 is the prompt; turn 2 and the contact stay in two_turn.py."""
    case = two_turn.kept()
    return {'description': two_turn.CASE_ID + ' #1',
            'vars': {'query': case['turn1'], 'profile_id': case['profile_id'], 'case_id': two_turn.CASE_ID,
                     'attempt': 1},
            'metadata': {'split': 'moved', 'difficulty': 'other', 'scored': True}}  # Layer 1 only, no judge.


if __name__ == '__main__':  # Writes the tests file Promptfoo reads: python cases.py <path>.
    Path(sys.argv[1]).write_text(json.dumps(runs(), indent=1), encoding='utf-8')


# Flow: cases.csv -> approved IDs (25 canonical + RULE-021) + 6 repeats -> rows with query/profile (app inputs),
# case_id/attempt (lookup keys) and split/difficulty/scored labels for the report.
