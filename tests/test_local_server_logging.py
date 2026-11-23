# Actual loopback server check. Dependency absence is an explicit unmet requirement, never proof.
import importlib.util
import json
import subprocess
import sys
import unittest
from uuid import UUID


@unittest.skipIf(importlib.util.find_spec('uvicorn') is None,
                 'Actual server verification blocked: pinned Uvicorn dependency approval/install required')
class LocalServerLoggingTests(unittest.TestCase):
    def test_normal_rejected_and_native_failure_logs_are_sanitized_and_correlated(self):
        # Child process isolates native server logger configuration and owns all sockets/workers.
        code=r'''
import asyncio,io,json,logging,socket,threading
from concurrent.futures import ThreadPoolExecutor
from limits.storage import MemoryStorage
import httpx,uvicorn
from shopunow import admission,api
from shopunow.allowances import RollingAllowances
from shopunow.server_logging import CorrelatedServerApp,server_log_config
from tests.owned_http_app import OwnedRecordsHTTPApp
from tests.test_owned_operations import OwnedStorage,NOW
from tests.test_records import USER_ID,CONVERSATION_ID
from uuid import UUID
async def main():
 server_sink,app_sink=io.StringIO(),io.StringIO()
 tracking=admission.OwnedRecordTracking();storage=OwnedStorage();storage.tracking=tracking
 native=MemoryStorage()
 with ThreadPoolExecutor(max_workers=5) as executor:
  app=OwnedRecordsHTTPApp(api_token='PRIVATE-TOKEN',slots=admission.ExecutionSlots(),
   allowances=RollingAllowances(native,threading.Lock()),executor=executor,readers=storage,
   roster=frozenset({USER_ID}),projector=api.project_identity_read,clock=__import__('time').monotonic,
   tracking=tracking,reservations=admission.ResourceReservations(),utc_now=lambda:NOW,
   uuid_factory=lambda:UUID(CONVERSATION_ID))
  @app.application.get('/unexpected')
  async def unexpected():raise RuntimeError('PRIVATE contact=private@example.test body=PRIVATE')
  configuration=server_log_config();configuration['handlers']['server']['stream']=server_sink
  config=uvicorn.Config(CorrelatedServerApp(app),host='127.0.0.1',port=0,http='h11',loop='asyncio',
   lifespan='off',log_config=configuration,access_log=True,use_colors=False)
  handler=logging.StreamHandler(app_sink);handler.setFormatter(logging.Formatter('%(message)s'))
  logger=logging.getLogger('shopunow.entry');logger.handlers=[handler];logger.setLevel(logging.INFO);logger.propagate=False
  sock=socket.socket();sock.bind(('127.0.0.1',0));sock.listen(16);sock.setblocking(False)
  server=uvicorn.Server(config);task=asyncio.create_task(server.serve(sockets=[sock]));responses=[]
  try:
   async with asyncio.timeout(4):
    while not server.started:await asyncio.sleep(.01)
   async with httpx.AsyncClient(base_url='http://127.0.0.1:'+str(sock.getsockname()[1]),timeout=3) as client:
    for path,headers in [('/preferences?user_id='+USER_ID,{'x-api-token':'PRIVATE-TOKEN'}),
                          ('/preferences?user_id=PRIVATE-QUERY',{'x-api-token':'PRIVATE-WRONG'}),
                          ('/unexpected?user_id=PRIVATE-QUERY',{})]:
     response=await client.get(path,headers=headers)
     body=response.json()
     responses.append({'status':response.status_code,'request_id':body.get('request_id') or body['error']['request_id']})
  finally:
   server.should_exit=True
   await asyncio.wait_for(task,4);sock.close();native.reset()
 print(json.dumps({'responses':responses,
                   'server':[json.loads(row) for row in server_sink.getvalue().splitlines()],
                   'application':[json.loads(row) for row in app_sink.getvalue().splitlines()]}))
asyncio.run(main())
'''
        result=subprocess.run([sys.executable,'-c',code],capture_output=True,text=True,timeout=18,
                              env={'PYTHONPATH':'src','PYTHONDONTWRITEBYTECODE':'1'})
        self.assertEqual(result.returncode,0,result.stderr)
        captured=json.loads(result.stdout)
        self.assertEqual([row['status'] for row in captured['responses']],[200,401,500])
        access=[row for row in captured['server'] if row['event']=='server_access']
        for response in captured['responses']:
            UUID(response['request_id'])
            self.assertTrue(any(row['request_id']==response['request_id'] and
                                row.get('http_status')==response['status'] for row in access))
            self.assertTrue(any(row['request_id']==response['request_id'] for row in captured['application']))
        failed_id=captured['responses'][-1]['request_id']
        self.assertTrue(any(row['outcome']=='error' and row['request_id']==failed_id for row in captured['server']))
        for row in captured['server']+captured['application']:
            self.assertLessEqual(set(row),{'utc_time','event','request_id','outcome','http_status','elapsed_ms'})
        serialized=result.stdout+result.stderr
        for forbidden in ['PRIVATE','private@example.test','127.0.0.1','syn-cust-01','Traceback']:
            self.assertNotIn(forbidden,serialized)
