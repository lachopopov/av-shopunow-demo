# ShopUNow support assistant (demo)

## 1. What the application is
ShopUNow is a fictional retail company. This demo is its support assistant: employees ask about HR, IT and facilities topics, and customers ask about billing, payments, shipping and delivery. The assistant uses approved FAQs and, when relevant, eligible earlier conversations from the selected profile. It says when it cannot answer and can offer a **simulated** handoff to a support team.

Everything in the demo is synthetic: the company, the profiles, the Knowledge and the support teams. No email is sent, no person is contacted and no real order, payment or ticket exists.

> **Temporary demo.** This demo may be taken offline at any time, and its stored records (conversations and simulated support requests) deleted then.

### The Knowledge set
54 synthetic FAQ entries. Each has a question, one or more short answer facts, a department, an audience and an access scope.

| Kind | Entries | Audience |
|---|---|---|
| Department general FAQ | 35 (7 each: HR, IT, Facilities & Admin, Billing & Payments, Shipping & Delivery) | employees (HR, IT, Facilities), customers (Billing, Shipping) |
| Company-wide | 4 | 2 for everyone, 1 employees only, 1 customers only |
| Personal case (e.g. a specific invoice or a desk move) | 12 (2 per employee department, 3 per customer department) | as the department |
| Department-internal | 3 (one each: HR, IT, Facilities) | employees |

**Used when answering (retrieval).** Only the 39 general entries are searched, filtered by the user's role:
- **Employees** search the general FAQ of all three employee departments (HR, IT, Facilities & Admin), not only their own, plus the company-wide entries for employees. A billing or shipping question from an employee finds nothing, because those entries are for customers, so the assistant says it has no source for it.
- **Customers** search the Billing & Payments and Shipping & Delivery general FAQ plus the company-wide entries for customers. A question about an employee department is refused before any search.

Personal-case and internal entries are never retrieved or shown to anyone, including employees of the same department. A personal case gets a simulated support offer because of how the question is judged, not because an entry was found.

## 2. Architecture
```
Browser ──> Streamlit UI (ui/) ──HTTPS + API token──> FastAPI backend (src/shopunow/) ──> Chroma Cloud
                                                              │                    (Knowledge, profiles, conversations,
                                                              │                     simulated support requests)
                                                              └──> Jev (TypeSafe) and OpenAI models
```
- **UI** (`ui/streamlit_app.py`, Streamlit): password sign-in, profile choice, conversations, questions, a progress bar while an answer is prepared, and the handoff form. The backend token stays on the server and never reaches the browser.
- **Backend** (`src/shopunow/`, FastAPI): verifies conversation ownership before classification and enforces access rules before retrieval. It runs the answer workflow (LangGraph) and stores records in Chroma Cloud.

**How a question is answered**

1. **Understand** (Jev)  
   Reads the question: what is asked, the tone, which departments it concerns,
   whether its parts depend on each other, and any sign of an attack.
2. **Security check** (`gpt-6-luna`; backup `gpt-6.1-terra`)  
   Only when an attack looks likely. A confirmed attack is blocked.
3. **Rules** (code)  
   Decides the type of reply: an upset user gets a support offer, a question
   outside the user's role is refused, an unclear one gets a clarification
   question, and a personal case or action gets a support offer.
4. **Routing** (Jev; `gpt-6.2-sol` when Jev is unsure)  
   Picks the departments. The same rules apply to the model's choice.
5. **Search** (Chroma Cloud, `text-embedding-3-small`; Jev, `gpt-6-luna`)  
   Finds up to 4 Knowledge entries per department the user may see and checks
   each for relevance and safety. `gpt-6.2-sol` decides entries Jev is unsure about.
6. **Write** (`gpt-6.2-sol`)  
   Uses cleared FAQ excerpts and, when needed, eligible earlier conversations
   from the selected profile. A central writer combines several departments or
   answers company-wide questions.
7. **Review** (2 × `gpt-6-luna`)  
   A security reviewer and a policy reviewer must both approve the full draft
   (at most three correction rounds). The answer is saved, checked, and only then shown.

Support sentences, offers, refusals and error messages are fixed text written by code. The reviewers check safety and policy. The writer uses cleared evidence for factual claims, and the app checks that citations refer to evidence supplied to the writer.

## 3. How to use it
1. Open the demo link and sign in with the demo password you were given.
2. Pick a synthetic profile: six employees (HR, IT, Facilities & Admin) and three customers.
3. Ask a question. You get one of: a reviewed answer, a partial answer with a pointer to support, a short clarification request, a refusal, or an offer to hand the question to a simulated support team.
4. If you accept an offer, fill in the short form. The demo saves a simulated support request, reads it back and only then shows a receipt.
5. Earlier conversations of the profile can be reopened from the sidebar.

**Use fictitious details only**, including in questions and support forms.

### Shared profiles: what it means and why
The demo has a fixed set of synthetic profiles and **no personal accounts**. Visitors who choose the same profile can see its saved conversations and support offers.

Why: the demo shows how the assistant handles different roles and their permissions without collecting sign-ups or real personal data.

What the app enforces: every conversation, history read and support request is bound to the profile it belongs to. One profile can never read or continue another profile's conversations or requests, and the assistant only uses the selected profile's own history. The app separates **profiles**, not individual visitors, which is why only fictitious details should be entered.

## 4. Status (2026-11-24)
- In a local evaluation, 33 of 36 runs passed (92%).
- The median response time for answered questions was 9.7 seconds.

## 5. Assumptions and limits
- **No streaming, by design.** An answer is shown only after both output reviewers have approved the complete draft, so you never see text that has not been checked. While you wait, the page shows which step is running.
- The demo handles at most 5 requests at the same time and at most 10 questions per minute from all visitors together (a rolling one-minute window). Each conversation answers one question at a time. Beyond that it asks you to try again shortly.
- A question about another department's internal information gets a support offer rather than a refusal; no restricted information is read or shown.
- Answers can be incomplete or wrong. The demo is not a source of real HR, IT, billing or shipping information.

### Reliability
The demo is designed to fail safely: it never shows an unchecked answer and always tells you what to do next.
- **After a backend restart:** You can reopen earlier conversations to read them. Start a new conversation to ask another question.
- **If a question’s result is uncertain:** Reopen the conversation to check whether a reply was saved before sending the question again.
- **If an answer can't be completed:** you get a short message or a simulated support offer instead.
- **When the demo is busy:** you're asked to try again shortly.
- **If you need a support receipt again:** Submit the same details on the same support offer. If the request was already saved, you’ll get the original receipt.

## 6. For developers
Python 3.12 and [uv](https://docs.astral.sh/uv/). The backend needs Chroma Cloud, OpenAI and TypeSafe settings as environment variables and refuses to start if one is missing; the UI needs the backend address, its token and a password.

Before starting the backend, configure the four Chroma Cloud collections and load the synthetic profiles and FAQ entries.

```bash
uv sync && uv run fastapi run                                  # backend
cd ui && uv sync && uv run streamlit run streamlit_app.py      # UI
```

## License
MIT; see `LICENSE`.
