# Python 3.12; owner decision 2026-11-23 (pass 3): the fallback router sees only the question, role and home
# department, runs only when Jev is unsure, and returns departments, intent, dependency and status.
import unittest
from tests import test_evidence as base


class RouterContractTests(unittest.TestCase):
    Base = base.EvidenceTests
    setUp = Base.setUp

    def router(self, departments=('shipping_delivery',)):
        from types import SimpleNamespace
        calls = []
        def route(payload, *, timeout):
            calls.append(payload)
            return {'question': payload['question'], 'status': 'resolved', 'departments': list(departments),
                    'intent': 'information', 'dependency': 'independent'}
        self.service.answer_models = SimpleNamespace(resolve_routing=route, resolve_candidates=None)
        self.service.semantic_confidence_trigger = .7
        return calls

    def decide(self, weak_dependency):
        from shopunow.incoming import validate_judgments
        from shopunow.workflow import IncomingDecision
        from tests.test_incoming import judgment_answers
        answers = judgment_answers()
        answers['topic_shipping_delivery']['noul'] = 1.
        if weak_dependency:
            answers['department_dependency'].update(confidence=.4, probabilities={
                'independent': .6, 'dependent': .39, 'unclear': .01})
        return self.service.prepare(IncomingDecision('continue', destinations=('shipping_delivery',),
            intent='information', judgments=validate_judgments(answers)))

    def test_router_gets_only_question_role_and_home_department(self):
        calls = self.router()
        result = self.decide(weak_dependency=True)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0], {'question': self.request.question, 'role': self.profile.role,
                                    'home_department': self.profile.home_department})
        self.assertEqual(result.destinations, ('shipping_delivery',))

    def test_router_is_not_called_when_jev_is_confident(self):
        calls = self.router()
        self.assertEqual(self.decide(weak_dependency=False).kind, 'ready')
        self.assertEqual(calls, [])


if __name__ == '__main__':
    unittest.main()
