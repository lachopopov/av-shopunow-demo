# HTTPX 0.28.1; ShopUNow UI -> FastAPI backend client. Runs on the Streamlit server, never in the browser.
# The backend token is sent from here only; the browser sees the shared-password page and results.
from dataclasses import dataclass  # Small immutable result for the UI to render.
import httpx  # Pinned HTTP client; one request per call, no retries.

NOTICE_SECONDS = 3.0  # LLD Slice 6: show a pending notice if a question takes longer than this.
# Backend visible budgets (MLD §11) plus a transport margin; distinct from the 3-second notice.
# Worst-case backend time is 15 s pre-admission + 90 s question budget; 15 s transport margin on top.
QUESTION_TIMEOUT = httpx.Timeout(connect=5.0, read=120.0, write=10.0, pool=5.0)
HANDOFF_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0)  # 20 s + 10 s margin.
OTHER_TIMEOUT = httpx.Timeout(connect=5.0, read=25.0, write=10.0, pool=5.0)  # 15 s + 10 s margin.
PROGRESS_TIMEOUT = httpx.Timeout(3.0)  # A progress read is optional: never hold the page up for long.

# Fixed UI wording per backend code. The backend message is never shown, so unknown text cannot leak.
MESSAGES = {
    'execution_capacity': 'The service is busy right now. Nothing was started; you can try again.',
    'profile_unavailable': 'This profile is unavailable.',
    'ownership_failure': 'This conversation is unavailable.',
    'conversation_busy': 'This conversation is still working on another request.',
    'conversation_unresolved': 'This conversation could not be verified. Start a new conversation.',
    'conversation_full': 'This conversation is full; start a new conversation.',
    'conversation_read_only': 'Start a new conversation to ask a question.',
    'question_limit': 'Too many questions in the last minute. Please wait and try again.',
    'history_limit': 'Too many history requests in the last minute. Please wait.',
    'profile_read_limit': 'Too many profile requests in the last minute. Please wait.',
    'write_limit': 'Too many saves in the last minute. Please wait.',
    'creation_busy': 'A conversation is already being created.',
    'offer_unavailable': 'This support offer is unavailable.',
    'handoff_busy': ('This support request is busy or could not be confirmed. Do not submit it again; '
                     'if this keeps happening, start a new conversation and ask for support there.'),
    'handoff_conflict': 'Different details were already submitted for this support offer.',
    'unauthorized': 'The app is not configured correctly.',
    'invalid_request': 'The request is not valid. Check the fields and try again.',
    'body_too_large': 'The request is too large.',
}
# Codes where the backend proves the operation did not do its work, so the user may simply try again.
DEFINITE = set(MESSAGES)
UNCERTAIN_MESSAGE = ('The result could not be confirmed. It may or may not have been saved; '
                     'reopen the conversation to check before trying again.')
# History shows no submission state (CD-100); an identical resubmission returns the same receipt.
HANDOFF_UNCERTAIN_MESSAGE = ('The support request could not be confirmed. Submit the same details again to get '
                             'your receipt; a second request will not be created.')
OUTCOME_KINDS = {'answer', 'clarification', 'refusal', 'handoff_offer', 'confirmed_attack_block', 'fallback_error'}


@dataclass(frozen=True)
class Result:
    ok: bool
    body: dict | None = None
    code: str | None = None
    message: str = ''
    uncertain: bool = False  # True: a write may have happened; the UI never resubmits automatically.


class Client:
    def __init__(self, base_url, token, *, transport=None):
        self._http = httpx.Client(base_url=base_url, headers={'x-api-token': token}, transport=transport,
                                  follow_redirects=False, trust_env=False)

    def _call(self, method, path, *, timeout, write, expect, json=None, params=None,
              uncertain_message=UNCERTAIN_MESSAGE):
        try:
            response = self._http.request(method, path, json=json, params=params, timeout=timeout)
        except httpx.HTTPError:
            return Result(False, message=uncertain_message if write else 'The service could not be reached.',
                          uncertain=write)
        try:
            body = response.json()
        except ValueError:
            body = None
        if response.status_code == 200 and isinstance(body, dict) and all(
                body.get(key) == value for key, value in expect.items()):
            return Result(True, body=body)
        error = body.get('error') if isinstance(body, dict) else None
        code = error.get('code') if isinstance(error, dict) else None
        if code in DEFINITE and (code != 'execution_capacity' or response.status_code == 503):
            return Result(False, code=code, message=MESSAGES[code])
        # Timeouts, stops, technical errors, unknown codes, mismatched bindings, host pages: unknown outcome.
        return Result(False, code=code if isinstance(code, str) else None,
                      message=uncertain_message if write else 'The request failed. Please try again.',
                      uncertain=write)

    def resolve_profile(self, user_id):
        return self._call('POST', '/profiles/resolve', json={'user_id': user_id}, timeout=OTHER_TIMEOUT,
                          write=False, expect={'user_id': user_id})

    def preference(self, user_id):
        return self._call('GET', '/preferences', params={'user_id': user_id}, timeout=OTHER_TIMEOUT,
                          write=False, expect={'user_id': user_id})

    def create_conversation(self, user_id):
        return self._call('POST', '/conversations', json={'user_id': user_id}, timeout=OTHER_TIMEOUT,
                          write=True, expect={'user_id': user_id})

    def list_conversations(self, user_id):
        return self._call('GET', '/conversations', params={'user_id': user_id}, timeout=OTHER_TIMEOUT,
                          write=False, expect={'user_id': user_id})

    def history(self, user_id, conversation_id):
        return self._call('GET', '/history', params={'user_id': user_id, 'conversation_id': conversation_id},
                          timeout=OTHER_TIMEOUT, write=False,
                          expect={'user_id': user_id, 'conversation_id': conversation_id})

    def progress(self, user_id, conversation_id):
        # Owner request 2026-11-23: the step a running question has reached (1-5), or None when unknown.
        # Any failure only means "no step shown"; it never affects the question itself.
        result = self._call('GET', '/progress', params={'user_id': user_id, 'conversation_id': conversation_id},
                            timeout=PROGRESS_TIMEOUT, write=False,
                            expect={'user_id': user_id, 'conversation_id': conversation_id})
        step = result.body.get('step') if result.ok else None
        return step if isinstance(step, int) and 1 <= step <= 5 else None

    def ask(self, user_id, conversation_id, question, response_style):
        payload = {'user_id': user_id, 'conversation_id': conversation_id, 'question': question}
        if response_style is not None:
            payload['response_style'] = response_style  # Session-only presentation choice.
        result = self._call('POST', '/interactions', json=payload, timeout=QUESTION_TIMEOUT, write=True,
                            expect={'user_id': user_id, 'conversation_id': conversation_id})
        if result.ok and (result.body.get('outcome') or {}).get('kind') not in OUTCOME_KINDS:
            return Result(False, message=UNCERTAIN_MESSAGE, uncertain=True)
        return result

    def submit_handoff(self, user_id, conversation_id, offer_id, *, name, phone, email):
        payload = {'user_id': user_id, 'conversation_id': conversation_id, 'offer_id': offer_id, 'name': name}
        if phone:
            payload['phone'] = phone
        if email:
            payload['email'] = email
        result = self._call('POST', '/handoffs', json=payload, timeout=HANDOFF_TIMEOUT, write=True,
                            expect={'user_id': user_id, 'conversation_id': conversation_id, 'simulated': True},
                            uncertain_message=HANDOFF_UNCERTAIN_MESSAGE)
        if result.ok and not result.body.get('receipt_id'):
            return Result(False, message=HANDOFF_UNCERTAIN_MESSAGE, uncertain=True)
        return result


def visible(items, selection):
    """Only results captured under the current (user, conversation, generation) are shown (LLD §2)."""
    return [row for row in items if row['sel'] == selection]


# Flow: one explicit request -> token from server config -> bounded wait -> bound success, a fixed
# definite-error message, or an uncertain outcome (no auto retry): a question is checked by reopening,
# a handoff by submitting the same details again, which returns the saved receipt (CD-100).


def wait_with_progress(future, update, interval=1.0):
    """Wait for one running request; after NOTICE_SECONDS call update() about every `interval` seconds."""
    from concurrent.futures import TimeoutError as StillRunning
    try:
        return future.result(timeout=NOTICE_SECONDS)  # Fast results (offers, blocks) show no progress.
    except StillRunning:
        pass
    while True:
        update()  # Redraw the progress bar with the latest step.
        try:
            return future.result(timeout=interval)
        except StillRunning:
            continue


def wait_with_notice(future, notify):
    """Wait for one running request; call notify() once if it is still running after NOTICE_SECONDS."""
    from concurrent.futures import TimeoutError as StillRunning
    try:
        return future.result(timeout=NOTICE_SECONDS)
    except StillRunning:
        notify()  # The client's own read timeout bounds the rest of the wait.
        return future.result()
