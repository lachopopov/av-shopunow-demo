# Re-export: the question host moved to shopunow.question_host in Slice 6; tests record request states.
from shopunow.question_host import QuestionHTTPApp as _ServedQuestionHost


class QuestionHTTPApp(_ServedQuestionHost):
    def __init__(self, **options):
        super().__init__(record_states=True, **options)
