# Python standard library v3.12; native logging policy, not a hosted retention guarantee.
import json  # Emit a fixed content-free record instead of formatting server arguments.
import logging  # Use the supported Filter and dictConfig facilities.
from contextvars import ContextVar  # Keep opaque correlation local to the server request task.
from datetime import datetime,timezone
from uuid import uuid4

server_request_id=ContextVar('shopunow_server_request_id',default=None)


class SafeServerRecord(logging.Filter):
    """Discard original text/arguments/diagnostics before any handler formats them."""

    def filter(self,record):
        event={'utc_time':datetime.now(timezone.utc).isoformat(),
               'event':'server_access' if record.name=='uvicorn.access' else 'server_log',
               'request_id':server_request_id.get() or uuid4().hex,
               'outcome':'error' if record.levelno>=logging.ERROR else 'status'}
        # Uvicorn's native access tuple may supply only a safe numeric status.
        if (record.name=='uvicorn.access' and isinstance(record.args,tuple) and len(record.args)==5
                and type(record.args[4]) is int and 100<=record.args[4]<=599):
            event['http_status']=record.args[4]
        record.msg=json.dumps(event)
        record.args=()
        record.exc_info=record.exc_text=record.stack_info=None
        return True


def server_log_config():
    """Explicit native configuration for local server checks; no global setup at import."""
    return {'version':1,'disable_existing_loggers':False,
        'filters':{'safe_server':{'()':SafeServerRecord}},
        'formatters':{'default':{'format':'%(message)s'},'access':{'format':'%(message)s'}},
        'handlers':{'server':{'class':'logging.StreamHandler','stream':'ext://sys.stderr',
                              'formatter':'default','filters':['safe_server']}},
        'loggers':{name:{'handlers':['server'],'level':'INFO','propagate':False}
                   for name in ['uvicorn','uvicorn.access','uvicorn.error']}}


class CorrelatedServerApp:
    """Keep one opaque reference through ASGI sends and the server's exception handler.

    Pure ASGI wrapping keeps correlation outside inner middleware/task groups. The
    native server owns a separate request task. On failure, leave its opaque context
    until that task finishes so the server's later error log retains correlation.
    This wrapper performs no admission, storage, response rewriting or exception logging.
    """
    def __init__(self,application):
        self.application=application

    async def __call__(self,scope,receive,send):
        if scope["type"] != "http":
            return await self.application(scope, receive, send)
        reference=uuid4().hex
        scope.setdefault('state',{})['request_id']=reference
        token=server_request_id.set(reference)
        try:
            await self.application(scope,receive,send)
        except BaseException:
            raise  # Preserve cancellation and native server error ownership.
        else:
            server_request_id.reset(token)  # Ordinary requests leave their caller's context intact.
