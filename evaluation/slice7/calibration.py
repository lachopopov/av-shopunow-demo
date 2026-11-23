# Python 3.12; Slice 7 LOCAL evaluation — judge calibration (design v3 §5; plan D3)
# 11 hand-written answer variants on dev cases FACT-008 (one department) and FACT-018 (two departments), plus one
# real earlier V4 answer (Sol's style). Each variant states the expected verdict for EVERY check; the bar is 100 %
# agreement. 11 variants are a smoke test for systematic rubric errors, not a measured judge error rate.
# PAID when run (2 terra calls per variant = 22): needs owner approval (run.sh calibrate).
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from assertions import _reference, judge_case  # noqa: E402 - same judge input shape as the main run.
from cases import rows  # noqa: E402

PASS = {'correct': True, 'grounded': True, 'required_facts': True, 'forbidden_claims': True, 'relevant': True}


def fails(*checks):
    return {**PASS, **{check: False for check in checks}}


FA8 = ['SU-FA-002']
FA18 = ['SU-IT-005', 'SU-FA-003']
VARIANTS = [
    # --- Must pass -------------------------------------------------------------------------------------------
    {'id': 'V01', 'kind': 'paraphrase with markdown bullets', 'case': 'FACT-008', 'cleared': FA8, 'expected': PASS,
     'answer': "The demo visitor process needs:\n- **The host**\n- **The visit date**\n- **How many visitors are "
               "coming**\n\nUse synthetic details only. This FAQ can't register an actual visitor or give out a "
               'pass.'},
    {'id': 'V02', 'kind': 'correct plus extra detail from a co-cleared source', 'case': 'FACT-018',
     'cleared': FA18 + ['SU-IT-001'], 'expected': PASS,
     'answer': 'For a device-support request, give the demo asset label, the symptom, and when the problem started; '
               "don't include passwords or private files. Leave out MFA codes too, and note that the FAQ does not "
               'create a live ticket. For a room display, confirm it is powered, select the intended input, and '
               'describe any fault to Facilities.'},
    {'id': 'V03', 'kind': 'concise, missing only reported-only facts', 'case': 'FACT-018', 'cleared': FA18,
     'expected': PASS,
     'answer': 'For device support, give the asset label, the symptom, and when it started. For a room display, '
               'check it is powered, select the intended input, and describe any fault to Facilities.'},
    {'id': 'V04', 'kind': 'RULE-011-style refusal note (partial sentence removed)', 'case': 'FACT-008',
     'cleared': FA8, 'expected': PASS,
     'answer': 'The synthetic demo visitor process asks for the host, the visit date, and the visitor count, using '
               "synthetic details. This FAQ cannot register a real visitor or issue a pass. I can't help with the "
               'other part of your request.'},
    {'id': 'V05', 'kind': 'real earlier V4 answer (Sol), its own sources and facts', 'case': 'NX-4657',
     'cleared': ['SU-BP-004'], 'expected': PASS,
     'answer': 'On the sample invoice:\n- **Delivery fee** is a separate shipping charge.\n- **Discount** reduces '
               'the total amount due.\n\nThese definitions apply to the sample invoice; they don’t explain an '
               'individual account charge.'},
    {'id': 'V06', 'kind': 'correct plus a suggestion to request a support handoff', 'case': 'FACT-008',
     'cleared': FA8, 'expected': PASS,
     'answer': 'The synthetic demo visitor process asks for the host, the visit date, and the visitor count. This '
               'FAQ cannot register a real visitor or issue a pass. If you need help with a specific visit, you can '
               'request a support handoff.'},
    # --- Must fail -------------------------------------------------------------------------------------------
    {'id': 'V07', 'kind': 'planted wrong value (contradicted)', 'case': 'FACT-008', 'cleared': FA8,
     'expected': fails('correct', 'required_facts', 'forbidden_claims'),
     'answer': 'The synthetic demo visitor process asks for the host, the visit date, and the visitor count. This '
               'FAQ can register your visitor and issue a pass.'},
    {'id': 'V08', 'kind': 'one missing required fact', 'case': 'FACT-008', 'cleared': FA8,
     'expected': fails('required_facts'),
     'answer': 'The synthetic demo visitor process asks for the host and the visit date, using synthetic details. '
               'This FAQ cannot register a real visitor or issue a pass.'},
    {'id': 'V09', 'kind': 'invented promise (unsupported + forbidden claim)', 'case': 'FACT-018', 'cleared': FA18,
     'expected': fails('grounded', 'forbidden_claims'),
     'answer': 'For a device-support request, provide the demo asset label, the symptom, and when the problem '
               'started; do not include passwords or private files. For a room display, confirm it is powered, '
               'select the intended input, and describe any fault to Facilities. Facilities will then send a '
               'technician who repairs the display the same day.'},
    {'id': 'V10', 'kind': 'hedged required fact', 'case': 'FACT-008', 'cleared': FA8,
     'expected': fails('required_facts'),
     'answer': 'The visitor process may ask for the host, and possibly the visit date and the number of visitors, '
               "but I can't confirm exactly what it needs. This FAQ cannot register a real visitor or issue a pass."},
    {'id': 'V11', 'kind': 'only one of two sub-questions answered', 'case': 'FACT-018', 'cleared': FA18,
     'expected': fails('required_facts'),
     'answer': 'For a device-support request, provide the demo asset label, the symptom, and when the problem '
               'started; do not include passwords or private files.'},
]

NX_4657 = {  # V4 not-seen case (handover_tracking/slice4-v4-unseen-20261120/not_seen_holdout.csv), split by §4 rule.
    'query': 'On a sample invoice, what do the delivery fee and discount lines mean?', 'profile_id': 'syn-cust-02',
    'facts': {'R1': {'text': 'the delivery fee is a separate shipping charge', 'required': True},
              'R2': {'text': 'the discount reduces the total due', 'required': True},
              'O1': {'text': 'the glossary cannot explain an individual account charge', 'required': False}},
    'forbidden': ["explanation of a specific account charge; any amount for the user's invoice"],
    'expected_answer': "On a sample invoice, the delivery fee is a separate shipping charge, and the discount reduces "
                       "the total due. This glossary explains the sample lines only and can't explain a charge on an "
                       'individual account.'}


def variant_case(variant):
    """(judge case, answer) built exactly like the main run's judge input."""
    catalog, profiles = _reference()
    out = {'trace': {'cleared_sources': list(variant['cleared']), 'cited_sources': []}}
    if variant['case'] == 'NX-4657':  # Not in the dataset: its own V4 case data; source is customer-eligible.
        sources ={source: {'question': catalog[source].source_question, 'facts': list(catalog[source].answer_facts)}
                   for source in variant['cleared']}
        return ({'id': 'NX-4657', 'query': NX_4657['query'], 'facts': NX_4657['facts'], 'sources': sources,
                 'forbidden': NX_4657['forbidden'], 'expected_answer': NX_4657['expected_answer']},
                variant['answer'])
    profile = profiles[rows()[variant['case']]['__metadata:profile_id']]
    return judge_case(variant['case'], out, profile, catalog), variant['answer']


def agreement(variant, verdict):
    """Per-check agreement between the judge's code-derived verdict and the variant's expectation."""
    if verdict.get('status') != 'judged':
        return {'agree': False, 'mismatches': ['JUDGE_INVALID']}
    failed = set(verdict['failed_checks'])
    mismatches = [check for check, ok in variant['expected'].items() if ok == (check in failed)]
    return {'agree': not mismatches, 'mismatches': mismatches}


def main(run_dir):
    """PAID: judge every variant once (2 calls each) and report agreement; full verdicts saved privately."""
    from assertions import judge_client, save_verdict
    from judge import judge_answer
    from ledger import Ledger
    ledger, results = Ledger(run_dir / 'ledger.json'), []
    for variant in VARIANTS:
        folder = run_dir / 'calibration' / variant['id']
        case, answer = variant_case(variant)
        verdict = judge_answer(judge_client(folder, ledger), ledger, case, answer)
        save_verdict(folder, verdict)
        results.append({'id': variant['id'], 'kind': variant['kind'], **agreement(variant, verdict)})
    summary = {'agreed': sum(row['agree'] for row in results), 'variants': len(results), 'results': results}
    from wire import write_private_json
    write_private_json(run_dir / 'calibration-summary.json', summary, indent=1)
    print(json.dumps(summary, indent=1))


if __name__ == '__main__':  # python calibration.py <run_dir> (run.sh calibrate)
    main(Path(sys.argv[1]))


# Flow: variant -> same judge input as the main run (own + cleared sources, fact split, forbidden labels) ->
# two terra calls -> code verdict -> per-check comparison with the expectation -> summary (bar: 11/11).
