# Python 3.12 stdlib; answer progress steps for the UI (owner request 2026-11-23).
# Holds only a step number per (user, conversation) while a question runs, in this process's memory.
# No question or answer content is kept; a restart simply loses the steps (the answer is unaffected).
from threading import Lock  # The workflow reports from worker threads while the route reads.

# 1 checking the question, 2 finding the department, 3 looking up information, 4 writing, 5 checking the answer.
STEPS = 5


class Progress:
    def __init__(self):
        self._steps, self._lock = {}, Lock()

    def report(self, user_id, conversation_id, step):
        """Record a step; never move back (a later supplementary search must not rewind the bar)."""
        key = (user_id, conversation_id)
        with self._lock:
            self._steps[key] = max(step, self._steps.get(key, 0))

    def get(self, user_id, conversation_id):
        """Current step, or None when no question is running in that conversation."""
        with self._lock:
            return self._steps.get((user_id, conversation_id))

    def clear(self, user_id, conversation_id):
        """Called when the question ends, however it ends."""
        with self._lock:
            self._steps.pop((user_id, conversation_id), None)

# Flow: workflow reports step numbers -> GET /progress reads them -> the UI draws the bar -> cleared at the end.
