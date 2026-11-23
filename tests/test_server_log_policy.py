# Python 3.12 logging; these checks exercise the sanitizer, not an actual server.
import importlib.util
import io
import json
import logging
import logging.config
import unittest
from uuid import UUID


class ServerLogPolicyTests(unittest.TestCase):
    def policy(self):
        self.assertIsNotNone(importlib.util.find_spec('shopunow.server_logging'),
                             'Missing local native server logging policy')
        from shopunow import server_logging
        return server_logging

    def test_native_handler_discards_access_query_ip_and_exception_payloads(self):
        policy = self.policy()
        sink = io.StringIO()
        handler = logging.StreamHandler(sink)
        handler.setFormatter(logging.Formatter('%(message)s'))
        handler.addFilter(policy.SafeServerRecord())
        records=[logging.LogRecord('uvicorn.access',logging.INFO,'private_path',1,'%s %s %s %s %s',
                    ('PRIVATE-IP','GET','/history?user_id=PRIVATE-USER&token=PRIVATE-TOKEN','1.1',200),None),
                 logging.LogRecord('uvicorn.error',logging.ERROR,'private_path',1,'PRIVATE exception %s',
                                   ('PRIVATE-CONTACT',),(RuntimeError,RuntimeError('PRIVATE body'),None))]
        records[-1].stack_info = "PRIVATE stack"
        records[-1].exc_text = "PRIVATE cached traceback"
        reference='ffffffff-ffff-4fff-8fff-ffffffffffff'
        token=policy.server_request_id.set(reference)
        try:
            for record in records:
                handler.handle(record)
        finally:
            policy.server_request_id.reset(token)
        events=[json.loads(row) for row in sink.getvalue().splitlines()]
        self.assertEqual([row['request_id'] for row in events],[reference,reference])
        self.assertEqual(events[0]['http_status'],200)
        self.assertEqual(events[1]['outcome'],'error')
        self.assertNotIn('PRIVATE',sink.getvalue())
        for event in events:
            self.assertLessEqual(set(event),{'utc_time','event','request_id','outcome','http_status'})

    def test_untrusted_message_and_non_numeric_status_never_become_log_fields(self):
        policy = self.policy()
        record = logging.LogRecord(
            "uvicorn.access",
            logging.INFO,
            "x",
            1,
            "PRIVATE format",
            ("PRIVATE", "PRIVATE", "PRIVATE", "PRIVATE", "PRIVATE"),
            None,
        )
        self.assertTrue(policy.SafeServerRecord().filter(record))
        event = json.loads(record.getMessage())
        UUID(event["request_id"])
        self.assertNotIn('http_status',event)
        self.assertNotIn('PRIVATE',record.getMessage())
        self.assertIsNone(record.exc_info)
        self.assertIsNone(record.stack_info)

    def test_native_configuration_sanitizes_both_actual_logger_paths(self):
        import subprocess
        import sys
        self.policy()
        # A child isolates native dictConfig's global logger mutations from the test runner.
        code="""import logging,logging.config
from shopunow.server_logging import server_log_config
logging.config.dictConfig(server_log_config())
logging.getLogger('uvicorn.access').info('PRIVATE query=%s', 'PRIVATE token')
try:raise RuntimeError('PRIVATE exception')
except RuntimeError:logging.getLogger('uvicorn.error').exception('PRIVATE body')
"""
        result=subprocess.run([sys.executable,'-c',code],capture_output=True,text=True,timeout=5,
                              env={'PYTHONPATH':'src','PYTHONDONTWRITEBYTECODE':'1'})
        self.assertEqual(result.returncode,0)
        events=[json.loads(row) for row in result.stderr.splitlines()]
        self.assertEqual([row['event'] for row in events],['server_access','server_log'])
        self.assertNotIn('PRIVATE',result.stderr)
        self.assertNotIn('Traceback',result.stderr)


class ServerCorrelationTests(unittest.IsolatedAsyncioTestCase):
    async def test_server_task_can_correlate_native_error_after_app_reraises(self):
        import asyncio
        from shopunow import api,server_logging
        self.assertTrue(hasattr(server_logging,'CorrelatedServerApp'),'Missing server-task correlation')
        sink = io.StringIO()
        handler = logging.StreamHandler(sink)
        handler.setFormatter(logging.Formatter("%(message)s"))
        handler.addFilter(server_logging.SafeServerRecord())
        logger = logging.getLogger("uvicorn.error")
        old = list(logger.handlers)
        propagate = logger.propagate
        logger.handlers = [handler]
        logger.propagate = False
        self.addCleanup(setattr, logger, "handlers", old)
        self.addCleanup(setattr, logger, "propagate", propagate)
        app=api.create_app()
        @app.get('/failed')
        async def failed():raise RuntimeError('PRIVATE native error')
        scope={'type':'http','asgi':{'version':'3.0'},'method':'GET','scheme':'http','path':'/failed',
               'raw_path':b'/failed','query_string':b'user_id=PRIVATE','headers':[],
               'server':('test',80),'client':('PRIVATE-IP',123),'http_version':'1.1'}
        sent=[]
        async def receive():return {'type':'http.request','body':b'','more_body':False}
        async def send(message):sent.append(message)
        async def native_request_task():
            try:
                await server_logging.CorrelatedServerApp(app)(scope, receive, send)
            except RuntimeError:
                logger.exception("PRIVATE native exception wrapper")

        await asyncio.create_task(native_request_task())
        body=json.loads(b''.join(row.get('body',b'') for row in sent))
        event=json.loads(sink.getvalue())
        self.assertEqual(event['request_id'],body['error']['request_id'])
        self.assertNotIn('PRIVATE',sink.getvalue())
