# Uvicorn 0.54.0 / Python 3.12; one supervised process, no implicit credentials or SDK client.
import uvicorn
from shopunow.server_logging import CorrelatedServerApp, server_log_config


def server_config(application, *, port=8000):
    return uvicorn.Config(CorrelatedServerApp(application),host='127.0.0.1',port=port,
        workers=1,reload=False,log_config=server_log_config(),access_log=True,use_colors=False)


def run(application, *, port=8000):
    uvicorn.Server(server_config(application,port=port)).run()


# Flow: caller supplies the explicitly configured app -> one worker/loop -> existing safe logs.
# The operator must prevent a second instance/overlapping replacement; this is not a fencing service.
