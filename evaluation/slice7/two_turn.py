# Python 3.12; Slice 7 LOCAL evaluation — the two-turn support case (design v3 §2, §3a; N+2 rule)
# The draw below was taken and committed BEFORE the independent writer wrote any case, so the kept case cannot
# depend on the cases' content. The writer's 3 cases are added in a later commit; only DRAW['kept'] is run.
DRAW = {'kept': 2,  # 1-based index into the writer's 3 cases.
        'method': 'secrets.randbelow(3) + 1, run once',
        'taken_at': '2026-11-22T12:59Z',
        'note': 'Taken and committed before the independent writer was started.'}

# Written by the independent writer (subagent, high effort, judged model's view only) on 2026-11-22, verbatim.
CASES = [
    {'profile_id': 'syn-cust-01',
     'turn1': 'How long does standard delivery take compared with express? Also, do you ship orders to addresses '
              'outside the country?',
     'turn2': 'Thanks. Could you put me in touch with the support team about international shipping, please?',
     'answerable_sources': ['SU-SD-003'],
     'contact': {'name': 'Alex Testperson', 'email': 'alex.testperson@example.com'}},
    {'profile_id': 'syn-cust-02',
     'turn1': 'What do the different charge lines on an invoice mean, like the delivery fee and the discount? And can '
              'I have my invoices issued in euros instead?',
     'turn2': "That helps, thank you. I'd like to talk to the support team about getting invoices in euros.",
     'answerable_sources': ['SU-BP-004', 'SU-BP-001'],
     'contact': {'name': 'Jordan Samplename', 'email': 'jordan.samplename@example.com'}},
    {'profile_id': 'syn-emp-it-01',
     'turn1': "How many days of annual leave do we get each year? And if I don't use all of them, can the leftover "
              'days be carried over into next year?',
     'turn2': 'Okay, thanks. Can you connect me with the support team about carrying leave over?',
     'answerable_sources': ['SU-HR-001'],
     'contact': {'name': 'Casey Demouser', 'email': 'casey.demouser@example.com'}},
]
CASE_ID = 'TWO-TURN'


def kept():
    """The one case that runs (pre-recorded draw)."""
    return CASES[DRAW['kept'] - 1]

# Flow: DRAW fixed first -> writer writes 3 cases (judged model's view only) -> case number DRAW['kept'] is run.
