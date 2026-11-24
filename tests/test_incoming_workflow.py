# LangGraph 1.2.20; controlled model boundaries, actual gate transitions.
import importlib.util
import unittest
from shopunow.incoming import IncomingContext
from tests.test_incoming import judgment_answers


class IncomingWorkflowTests(unittest.TestCase):
    def test_semantic_uncertainty_never_overrides_existing_protective_dispositions(self):
        from shopunow.workflow import build_incoming_graph
        for field,label,expected in (('sentiment','negative','handoff'),
                                     ('intent','individual_case_or_action','handoff')):
            with self.subTest(field=field):
                answers=judgment_answers()
                answers[field]['choice']=label
                answers[field]['confidence']=.2
                labels=list(answers[field]['probabilities'])
                answers[field]['probabilities']={name:(.5 if name==label else .5/(len(labels)-1)) for name in labels}
                answers['company_wide_topic']['noul']=.53
                graph=build_incoming_graph(judge=lambda context:answers,reviewer=None,authorize=lambda:True,
                    security_trigger=.7,topic_trigger=.5,semantic_confidence_trigger=.7,topic_confidence_trigger=.1)
                result=graph.invoke({'context':IncomingContext('Question','customer',None,(),False)})['decision']
                self.assertEqual(result.kind,expected)
                self.assertEqual(result.destinations,())

    def test_semantic_confidence_preserves_both_sides_without_finalizing_weak_topic(self):
        from shopunow.workflow import build_incoming_graph
        for probability in (.47,.53):
            with self.subTest(probability=probability):
                answers=judgment_answers()
                answers['topic_shipping_delivery']['noul']=.95
                answers['company_wide_topic']['noul']=probability
                graph=build_incoming_graph(judge=lambda context:answers,reviewer=None,authorize=lambda:True,
                    security_trigger=.7,topic_trigger=.5,semantic_confidence_trigger=.7,topic_confidence_trigger=.1)
                decision=graph.invoke({'context':IncomingContext('Question','customer',None,(),False)})['decision']
                self.assertEqual(decision.destinations,('shipping_delivery',))
                self.assertIn('central_support',decision.proposed_destinations)
                self.assertEqual(decision.judgments['company_wide_topic'].noul,probability)

    def run_gate(self, *, answers=None, review=None, trigger=0.5, allowed=lambda: True, injected=None):
        self.assertIsNotNone(importlib.util.find_spec('shopunow.workflow'), 'Incoming graph is missing')
        from shopunow.workflow import build_incoming_graph
        self.calls = []
        def judge(context):
            self.calls.append('judge')
            return judgment_answers() if answers is None else answers
        def reviewer(context, risks):
            self.calls.append('review')
            return review
        graph = build_incoming_graph(judge=judge, reviewer=reviewer,
                                     authorize=allowed, security_trigger=trigger, topic_trigger=0.5)
        return graph.invoke({'context':IncomingContext('Question', 'customer', None, (), False),
                             **(injected or {})})['decision']

    def test_absent_calibrated_trigger_starts_no_model_work(self):
        self.assertEqual(self.run_gate(trigger=None).kind, 'disabled')
        self.assertEqual(self.calls, [])

    def test_invalid_judgments_never_authorize_routing(self):
        self.assertEqual(self.run_gate(answers={}).kind, 'dependency_failure')
        self.assertEqual(self.calls, ['judge'])

    def test_high_risk_is_reviewed_and_never_directly_blocked(self):
        answers = judgment_answers()
        answers['prompt_injection']['noul'] = 1.0
        decision = self.run_gate(answers=answers, review={'verdict':'clear', 'attack_types':[]})
        # No matched topic: an information question goes to central support (owner 2026-11-23, Q-c).
        self.assertEqual((decision.kind, decision.destinations), ('continue', ('central_support',)))
        self.assertEqual(self.calls, ['judge', 'review'])

    def test_confirmed_attack_has_no_route_or_offer_decision(self):
        answers = judgment_answers()
        answers['profile_override_attempt']['noul'] = 0.5
        decision = self.run_gate(answers=answers,
            review={'verdict':'confirmed_attack', 'attack_types':['profile_override_attempt']})
        self.assertEqual(decision.kind, 'confirmed_attack')
        self.assertEqual(decision.destinations, ())

    def test_malformed_reviewer_returns_security_uncertainty(self):
        answers = judgment_answers()
        answers['sql_injection']['noul'] = 0.9
        self.assertEqual(self.run_gate(answers=answers, review={}).kind, 'security_uncertain')

    def test_customer_employee_topic_is_refused_without_dispatch(self):
        answers = judgment_answers()
        answers['topic_hr']['noul'] = 0.9
        decision = self.run_gate(answers=answers)
        self.assertEqual(decision.kind, 'refusal')
        self.assertEqual(decision.refused_departments, ('hr',))
        self.assertEqual(decision.destinations, ())

    def test_customer_department_specific_and_company_wide_routes_are_distinct(self):
        answers = judgment_answers()
        answers['topic_shipping_delivery']['noul'] = 0.9
        self.assertEqual(self.run_gate(answers=answers).destinations, ('shipping_delivery',))
        answers['company_wide_topic']['noul'] = 0.9
        self.assertEqual(self.run_gate(answers=answers).destinations,
                         ('central_support', 'shipping_delivery'))

    def test_stop_before_graph_prevents_all_model_calls(self):
        self.assertEqual(self.run_gate(allowed=lambda: False).kind, 'stopped')
        self.assertEqual(self.calls, [])

    def test_new_request_cannot_inherit_a_supplied_previous_clearance_or_route(self):
        from shopunow.workflow import IncomingDecision
        answers = judgment_answers()
        answers['sql_injection']['noul'] = 0.9
        decision = self.run_gate(answers=answers, review={}, injected={
            'decision':IncomingDecision('continue', ('hr',)), 'answers':judgment_answers()})
        self.assertEqual(decision.kind, 'security_uncertain')
        self.assertEqual(decision.destinations, ())
        self.assertEqual(self.calls, ['judge', 'review'])

    def test_mixed_intent_retains_unresolved_individual_action_for_later_handoff(self):
        answers = judgment_answers()
        answers['topic_shipping_delivery']['noul'] = 0.9
        answers['intent']['choice'] = 'mixed'
        answers['intent']['probabilities'] = {key: float(key == 'mixed')
                                             for key in answers['intent']['probabilities']}
        decision = self.run_gate(answers=answers)
        self.assertEqual(decision.kind, 'continue')
        self.assertEqual(getattr(decision, 'intent', None), 'mixed')
        self.assertIs(getattr(decision, 'requires_action_handoff', None), True)

    def test_department_free_question_keeps_service_and_access_gates_before_central_support(self):
        # Owner 2026-11-23 (Q-c): information naming no department goes to central support, after the
        # negative-sentiment, action, clarification and customer-refusal gates.
        for intent,sentiment,restricted,expected,destinations in (
                ('information','neutral',False,'continue',('central_support',)),
                ('information','negative',False,'handoff',()),
                ('individual_case_or_action','neutral',False,'handoff',()),
                ('mixed','neutral',False,'handoff',()),
                ('unclear_or_other','neutral',False,'clarification',()),
                ('information','neutral',True,'refusal',())):
            with self.subTest(intent=intent,sentiment=sentiment,restricted=restricted):
                answers=judgment_answers()
                for key,label in (('intent',intent),('sentiment',sentiment)):
                    answers[key]['choice']=label
                    answers[key]['probabilities']={name:float(name==label) for name in answers[key]['probabilities']}
                answers['topic_hr']['noul']=float(restricted)
                decision=self.run_gate(answers=answers)
                self.assertEqual((decision.kind,decision.destinations),(expected,destinations))

# Flow: controlled judgment -> real graph/security/access -> internal continuation decision.
