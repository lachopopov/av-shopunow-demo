# Python 3.12 / httpx 0.28.1 / FastAPI 0.143.2; Slice 7 LOCAL evaluation — one case per process (design v3 §6)
# CD-044: one SDK client, loop and lifespan per process. The provider starts this file as a subprocess per case;
# the subprocess builds the app, runs its lifespan, sends the case's HTTP requests in-process, and prints JSON.
import importlib  # Load the app builder named in the case spec (real build or a test stub).
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx  # In-process ASGI transport: real FastAPI routing, no sockets.

HERE = Path(__file__).resolve().parent


def _json(response):
    """Response body as JSON, or None (a raw 5xx has none; it must still reach grading, challenger M4)."""
    try:
        return response.json()
    except ValueError:
        return None


async def run_case(storage_app, *, token, user_id, turns, handoff=None):
    """Lifespan -> create conversation -> each turn -> optional /handoffs; per-turn latency in ms."""
    from shopunow.server_logging import CorrelatedServerApp  # The hosted wrapper (hosted.build_app).
    asgi = CorrelatedServerApp(storage_app)
    headers = {'x-api-token': token}
    result = {'pid': os.getpid(), 'turns': []}
    # HTTPX's ASGI transport does not run the lifespan; without it /interactions answers execution_capacity.
    async with storage_app.application.router.lifespan_context(storage_app.application):
        transport = httpx.ASGITransport(app=asgi, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url='http://eval.local', headers=headers,
                                     timeout=None) as client:  # The app's own deadlines apply, not the client's.
            created = await client.post('/conversations', json={'user_id': user_id})
            result['conversation'] = {'status': created.status_code, 'body': _json(created)}
            if created.status_code != 200:
                result['setup_failed'] = True  # An invalid setup, not an app result: no question is sent.
                return result
            conversation_id = result['conversation']['body']['conversation_id']
            for question in turns:
                started = time.perf_counter()
                answered = await client.post('/interactions', json={'user_id': user_id,
                                             'conversation_id': conversation_id, 'question': question})
                latency_ms = (time.perf_counter() - started) * 1000  # POST /interactions only (design v3 §6).
                result['turns'].append({'status': answered.status_code, 'body': _json(answered),
                                        'latency_ms': latency_ms})
            if handoff is not None:
                offer_id = ((result['turns'][-1]['body'] or {}).get('outcome') or {}).get('offer_id')
                submitted = await client.post('/handoffs', json={'user_id': user_id,
                                              'conversation_id': conversation_id, 'offer_id': offer_id, **handoff})
                result['handoff'] = {'status': submitted.status_code, 'body': _json(submitted)}
    return result  # The lifespan has exited here: workers drained before the process ends.


def main():
    """Subprocess entry: JSON spec on stdin -> builder(spec) -> run_case -> trace -> JSON result on stdout."""
    import asyncio

    from case_trace import build_trace  # Layer-1 evidence needs the live stand-ins, so it is built here.
    spec = json.loads(sys.stdin.read())
    module, function = spec['builder'].split(':')
    storage_app, local, evidence = getattr(importlib.import_module(module), function)(spec)
    profiles_before = dict(local['profiles'].records)  # Seeded state, to detect any profile change.
    result = asyncio.run(run_case(storage_app, token=spec['token'], user_id=spec['user_id'],
                                  turns=spec['turns'], handoff=spec.get('handoff')))
    recorded = evidence()  # Provider calls and Knowledge source_ids the app received during this case.
    result['wire_calls'] = recorded['wire_calls']  # Cost, and the cleared/cited sources for the judge.
    result['trace'] = build_trace(recorded['wire_calls'], knowledge_reads=recorded['knowledge_reads'],
                                  local=local, profiles_before=profiles_before, turns=result['turns'])
    print(json.dumps(result))


def run_in_subprocess(spec, *, timeout):
    """Run one case in a fresh Python process (CD-044) and return its JSON result; stderr stays private."""
    completed = subprocess.run([sys.executable, str(HERE / 'case_runner.py')], input=json.dumps(spec),
                               capture_output=True, text=True, timeout=timeout, cwd=HERE, check=False)
    if completed.returncode != 0:
        raise RuntimeError('Case subprocess failed with exit code %d.' % completed.returncode)
    return json.loads(completed.stdout.strip().splitlines()[-1])


if __name__ == '__main__':
    main()


# Flow: provider -> run_in_subprocess(spec) -> new process: builder -> app lifespan start -> POST /conversations
# -> POST /interactions per turn (timed) -> optional /handoffs -> lifespan exit (drain) -> wire calls + layer-1
# trace -> JSON line -> provider.
