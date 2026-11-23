# Python 3.12; Slice 7 LOCAL evaluation — required-fact split v2 (design v3 §4; owner-approved 2026-11-22)
# Versioned evaluator data, copied from the design table. Required facts answer part of the question; reported-only
# facts count in the coverage fraction but never fail a case alone. Changing this is a dated design change.
FACT_SPLIT_VERSION = 'v2-2026-11-22 (v3: FACT-017 full/partial days reported-only)'


def _split(required, reported=()):
    facts = {'R%d' % number: {'text': text, 'required': True} for number, text in enumerate(required, 1)}
    facts.update({'O%d' % number: {'text': text, 'required': False} for number, text in enumerate(reported, 1)})
    return facts


FACTS = {
    'FACT-001': _split(['the employee support map', 'basic security reminders',
                        'how to ask HR about individual onboarding'],
                       ['the checklist cannot enroll the employee or confirm completion']),
    'FACT-002': _split(['the first and last dates of the leave', 'whether the days are full or partial days',
                        'chat cannot submit or approve leave']),
    'FACT-003': _split(['closure days are separate and do not reduce the annual leave allowance'],
                       ['the allowance is 20 days', 'no individual balance is inferred']),
    'FACT-004': _split(["don't open unexpected links or attachments",
                        'describe it to IT Support via a simulated handoff'],
                       ['chat cannot investigate or remove it']),
    'FACT-005': _split(['MFA codes are never requested or shared (so the answer is no)'],
                       ['enrollment or recovery needs individual IT follow-up']),
    'FACT-006': _split(['the application name', 'the work purpose', 'asking does not install it'],
                       ['asking does not grant access or decide approval']),
    'FACT-007': _split(['the date', 'the time period', 'any accessibility need if relevant',
                        'chat cannot reserve or guarantee a desk']),
    'FACT-008': _split(['the host', 'the visit date', 'the visitor count',
                        'chat cannot register a real visitor or issue a pass']),
    'FACT-009': _split(['total = subtotal + fee + tax - discount', 'chat cannot open an actual invoice'],
                       ['the full list of invoice fields']),
    'FACT-010': _split(['"demo card" is only a label', "don't enter a full card number",
                        'chat cannot collect card details or complete payment']),
    'FACT-011': _split(['a matching amount or date is a clue, not proof',
                        'the user may describe two synthetic charge references and dates for review'],
                       ['chat cannot verify charges or decide a refund']),
    'FACT-012': _split(['preparing means the order is being readied',
                        'handed to carrier means a carrier transfer was recorded'],
                       ['the stages are illustrative, not live status']),
    'FACT-013': _split(['address changes are considered before "handed to carrier"',
                        'afterwards it needs individual review and rerouting is not guaranteed'],
                       ['chat cannot inspect the stage or change the order']),
    'FACT-014': _split(['keep the outer packaging and the order reference', 'chat does not decide a refund'],
                       ['describe the visible damage', 'chat does not submit a claim or decide responsibility']),
    'FACT-015': _split(['HR, IT Support and Facilities & Admin handle their own subjects',
                        'CentralSupport explains the overall support map']),
    'FACT-016': _split(['Billing & Payments', 'Shipping & Delivery'], ['CentralSupport explains the map']),
    'FACT-017': _split(['a leave request includes the first and last dates', 'avoid unexpected links',
                        'describe suspicious email to IT Support via a simulated handoff'],
                       ['say whether the days are full or partial days', 'chat cannot submit or approve leave']),
    'FACT-018': _split(['for the device: the asset label, the symptom and when it started',
                        'for the meeting room: confirm the display is powered', 'select the intended input',
                        'describe room faults to Facilities'],
                       ['do not share passwords or private files']),
    'FACT-019': _split(['an invoice states an amount due; it is not proof of payment',
                        'a delivery is late only after the upper end of the selected delivery window after '
                        'carrier handoff'],
                       ['the demo cannot verify a receipt', 'no loss or new delivery date is established']),
    'RULE-011': _split(['handed to carrier means a carrier transfer was recorded',
                        "the restricted HR part of the question is refused"],
                       ['the stage is not live order status']),
}

SOURCES = {  # Owner-confirmed 2026-11-22 (design §4/§10); other cases use cases.csv source_id.
    'FACT-017': ['SU-HR-002', 'SU-IT-002'],
    'FACT-018': ['SU-IT-005', 'SU-FA-003'],
    'FACT-019': ['SU-BP-003', 'SU-SD-004', 'SU-SD-003'],
}


# Flow: case ID -> {R1.., O1..} facts with required flags (judge call B) and, for FACT-017-019, the owner-confirmed
# own sources (judge call A's approved source text, with the eligible sources the app cleared or cited).
