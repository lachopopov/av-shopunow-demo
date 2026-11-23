# Streamlit 1.67.0; ShopUNow support assistant UI (LLD Slice 6). Talks to the FastAPI backend only.
# Shared password gate and synthetic profiles: a demo boundary, not real authentication (MLD §1).
import hmac  # Constant-time password comparison.
import os
from concurrent.futures import ThreadPoolExecutor
import streamlit as st
import api_client

# Approved synthetic roster (labels only; the backend's stored binding is authoritative).
PROFILES = ('syn-cust-01', 'syn-cust-02', 'syn-cust-03', 'syn-emp-hr-01', 'syn-emp-hr-02',
            'syn-emp-it-01', 'syn-emp-it-02', 'syn-emp-fac-01', 'syn-emp-fac-02')
NOTICE_TEXT = 'Still working on your question. Answers can take up to about 90 seconds.'
DEMO_NOTICE = 'Demo only: profiles are shared by all visitors. Use fictitious contact details only.'  # D15, owner.

st.set_page_config(page_title='ShopUNow support', page_icon=':material/support_agent:')


def setting(name):
    # Server-side configuration: Streamlit secrets first, then the environment. Never shown in the page.
    try:
        value = st.secrets.get(name)
    except Exception:  # No secrets file configured.
        value = None
    return (value or os.environ.get(name) or '').strip()


backend_url, api_token, password = (setting('SHOPUNOW_BACKEND_URL'), setting('SHOPUNOW_API_TOKEN'),
                                    setting('SHOPUNOW_UI_PASSWORD'))
if not (backend_url and api_token and password):
    st.error('The app is not configured.', title='Configuration missing')
    st.stop()

st.title('ShopUNow support')

# --- Shared password gate -------------------------------------------------------------------------
if not st.session_state.get('authenticated'):
    st.text_input('Password', type='password', key='password')
    if st.button('Sign in', key='sign_in'):
        if hmac.compare_digest(st.session_state.password.encode(), password.encode()):
            st.session_state.authenticated = True
            del st.session_state['password']  # Do not keep the submitted password in session state.
            st.rerun()
        st.error('The password is not correct.')
    st.stop()

# --- Session state, initialised in one place ---------------------------------------------------
if 'client' not in st.session_state:
    st.session_state.client = api_client.Client(backend_url, api_token)  # Token stays on this server.
for name, default in (('generation', 0), ('conversation_id', None), ('transcript', []), ('receipts', {}),
                      ('binding', None), ('conversations', None), ('notice', None), ('pending', None)):
    st.session_state.setdefault(name, default)
client = st.session_state.client


@st.cache_resource
def request_pool():
    return ThreadPoolExecutor(max_workers=4)  # Lets the page show the 3-second notice while waiting.


def reset_view(conversation_id=None):
    # A switch increments the generation: anything captured earlier can no longer be displayed.
    st.session_state.generation += 1
    st.session_state.conversation_id = conversation_id
    st.session_state.notice = None


def selection():
    return (st.session_state.get('user_id'), st.session_state.conversation_id, st.session_state.generation)


def add(kind, text, *, offer_id=None, sel=None):
    st.session_state.transcript.append({'sel': sel or selection(), 'kind': kind, 'text': text, 'offer_id': offer_id})


def collect(pending):
    # Store a finished question's result under the selection that asked it (shown only there).
    result = pending['future'].result()
    if not result.ok:
        add('warning' if result.uncertain else 'error', result.message, sel=pending['sel'])
    else:
        outcome = result.body['outcome']
        add('assistant', outcome['text'], sel=pending['sel'], offer_id=outcome.get('offer_id'))
    st.session_state.pending = None


# A click during a waiting question restarts the script (challenger D3): the request keeps running and
# its result is collected on whichever later run first sees it finished.
if st.session_state.pending is not None and st.session_state.pending['future'].done():
    collect(st.session_state.pending)


def profile_changed():
    reset_view()
    st.session_state.binding = st.session_state.conversations = None
    st.session_state.pop('reopen', None)


def reopen_changed():
    chosen = st.session_state.get('reopen')
    reset_view(chosen)
    if chosen is None:
        return
    result = client.history(st.session_state.user_id, chosen)
    if not result.ok:
        st.session_state.notice = ('error', result.message)
        return
    notices = {}  # Exchange notices sit right after the answer with the same request_id.
    for notice in result.body['notices']:
        notices.setdefault(notice['request_id'], []).append(notice)
    for row in result.body['messages']:  # Complete owned transcript, in stored order.
        add('user' if row['speaker'] == 'user' else 'assistant', row['text'])
        if row['speaker'] == 'assistant':
            for notice in notices.pop(row['request_id'], []):
                add('notice', notice['code_owned_text'], offer_id=notice['offer_id'])
    for rest in notices.values():  # Pre-clearance and technical notices have no assistant message.
        for notice in rest:
            add('notice', notice['code_owned_text'], offer_id=notice['offer_id'])


# --- Sidebar: profile, response style, conversations ------------------------------------------
with st.sidebar:
    st.warning(DEMO_NOTICE, icon=':material/info:')  # Shown before any profile or contact detail is used.
    user_id = st.selectbox('Synthetic profile', PROFILES, index=None, key='user_id', on_change=profile_changed,
                           placeholder='Choose a profile')
    if user_id and st.session_state.binding is None:
        resolved = client.resolve_profile(user_id)
        st.session_state.binding = resolved.body['profile'] if resolved.ok else {'error': resolved.message}
    binding = st.session_state.binding
    if user_id and binding:
        if 'error' in binding:
            st.error(binding['error'])
        else:
            st.caption(f"Role: {binding['role']}" + (f" · Department: {binding['home_department']}"
                                                      if binding['home_department'] else ''))
    style = st.segmented_control('Response style', ['brief', 'detailed'], key='style')
    st.caption('Applies to this session; not saved.')
    if user_id and binding and 'error' not in binding:
        if st.button('New conversation', key='new_conversation', icon=':material/add:'):
            created = client.create_conversation(user_id)  # One explicit attempt; never retried here.
            if created.ok:
                reset_view(created.body['conversation_id'])
                st.session_state.conversations = None
            else:
                st.session_state.notice = ('warning' if created.uncertain else 'error', created.message)
        if st.button('Load my conversations', key='load_conversations', icon=':material/history:'):
            listed = client.list_conversations(user_id)
            st.session_state.conversations = listed.body['conversations'] if listed.ok else []
            if not listed.ok:
                st.session_state.notice = ('error', listed.message)
        if st.session_state.conversations:
            labels = {row['conversation_id']: row['created_at'] for row in st.session_state.conversations}
            st.selectbox('Reopen a conversation', list(labels), index=None, key='reopen',
                         format_func=lambda value: f'Started {labels[value][:16].replace("T", " ")}',
                         on_change=reopen_changed)

# --- Main area ----------------------------------------------------------------------------------
if not user_id:
    st.info('Choose a synthetic profile to start.')
    st.stop()
if st.session_state.notice:
    kind, text = st.session_state.notice
    getattr(st, kind)(text)
if st.session_state.conversation_id is None:
    st.info('Start a new conversation or reopen an earlier one.')
    st.stop()

current = selection()
for row in api_client.visible(st.session_state.transcript, current):
    if row['kind'] in ('user', 'assistant'):
        with st.chat_message(row['kind']):
            st.text(row['text'])  # Plain text: model or stored text never renders links or images.
    elif row['kind'] == 'notice':
        st.info(row['text'])
    else:
        getattr(st, row['kind'])(row['text'])
    offer_id = row['offer_id']
    if offer_id is None:
        continue
    receipt = st.session_state.receipts.get((current, offer_id))
    if receipt:
        st.success(f'Simulated support request saved. Receipt: {receipt}. '
                   'No email, call or staff assignment happens in this demo.')
        continue
    with st.form(key=f'handoff_{offer_id}', border=True):
        st.markdown('Request a simulated support handoff')
        name = st.text_input('Name', key=f'name_{offer_id}', max_chars=200)
        phone = st.text_input('Phone (optional if email given)', key=f'phone_{offer_id}', max_chars=40)
        email = st.text_input('Email (optional if phone given)', key=f'email_{offer_id}', max_chars=254)
        if st.form_submit_button('Request support handoff'):
            if not name.strip() or not (phone.strip() or email.strip()):
                st.error('Enter your name and a phone number or email.')
            else:
                sent = client.submit_handoff(current[0], current[1], offer_id, name=name, phone=phone.strip(),
                                             email=email.strip())
                if sent.ok:
                    st.session_state.receipts[(current, offer_id)] = sent.body['receipt_id']
                    st.rerun()
                else:
                    (st.warning if sent.uncertain else st.error)(sent.message)

pending = st.session_state.pending
if pending is not None:
    if pending['sel'] == current:
        st.info(NOTICE_TEXT)
    else:  # Asked under another selection: its result stays bound there and is never shown here.
        st.caption('An earlier question is still finishing; you can ask again shortly.')

    @st.fragment(run_every=2)
    def check_pending():
        if st.session_state.pending is None or st.session_state.pending['future'].done():
            st.rerun()  # Whole page: collect the result under its own selection.
    check_pending()

question = st.chat_input('Ask a question', key='question', submit_mode='disable', max_chars=2000,
                         disabled=pending is not None)  # One question at a time; no duplicates.
if question:
    captured = current  # Bind the answer to the selection that asked it.
    add('user', question, sel=captured)
    with st.chat_message('user'):
        st.text(question)
    waiting = st.empty()
    pending = {'sel': captured, 'future': request_pool().submit(client.ask, captured[0], captured[1],
                                                                 question, style)}
    st.session_state.pending = pending  # Survives an interrupted run.
    api_client.wait_with_notice(pending['future'], lambda: waiting.info(NOTICE_TEXT))
    waiting.empty()
    if st.session_state.pending is pending:
        collect(pending)
    st.rerun()  # Render from stored items, so only the current selection's results appear.


# Flow: server config -> password gate -> profile (stored binding) -> new/reopen conversation
# -> explicit question with 3 s notice -> bound result -> optional handoff form -> simulated receipt.
