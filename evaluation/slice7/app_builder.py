# Python 3.12 / openai 3.46.0 / typesafe_sdk 0.7.5 / httpx2 2.16.0 / chromadb 1.5.9
# Slice 7 LOCAL evaluation — builds the served app exactly as hosted.build_app does (design v3 §6, plan D14),
# except: provider calls go through the recording transport, and the three operational collections are fresh
# local stand-ins. Knowledge is read from Cloud through the read-only routing client. Credentials come from the
# process environment (the provider starts each case with `uv run --env-file .env`); nothing here prints them.
import asyncio
import os

from ledger import Ledger
from routing import RoutingClient
from stand_ins import LocalCollection, seed_profiles
from wire import WireLog, recording_http, recording_openai

LOCAL_NAMES = ('profiles', 'conversations', 'escalations')  # Local only: the Cloud operational names are unused.


async def cloud_connect_from(settings):
    """The same native Chroma Cloud client as hosted.build_app's connect (copied: that one is not reusable)."""
    import chromadb
    from chromadb.config import Settings
    return await chromadb.AsyncHttpClient(
        host=settings.chroma_host, port=443, ssl=True,
        headers={'x-chroma-token': settings.chroma_api_key.get_secret_value()},
        tenant=settings.chroma_tenant, database=settings.chroma_database,
        settings=Settings(_env_file=None, anonymized_telemetry=False, chroma_server_ssl_verify=True))


def real_build(spec, *, environ=None, cloud_connect=None, inner=None):
    """Case builder for case_runner: returns (storage app, local stand-ins, evidence callable)."""
    from typesafe_sdk import RetryPolicy, TypeSafeClient

    from shopunow.hosted import load_settings  # Same validation as hosting: fail closed, overrides refused.
    from shopunow.profiles import load_profiles
    from shopunow.storage_app import create_storage_app
    from shopunow.wiring import answering

    settings = load_settings(os.environ if environ is None else environ)
    local = {name: LocalCollection(name) for name in LOCAL_NAMES}  # Fresh, empty per case (one process each).
    asyncio.run(seed_profiles(local['profiles'], load_profiles()))  # Approved synthetic profiles only.
    holder = []  # The routing client is created inside the app's lifespan; kept for its knowledge_reads.

    async def connect():  # Awaited once by the app's worker lifespan, like hosted.build_app's connect.
        cloud = await (cloud_connect() if cloud_connect is not None else cloud_connect_from(settings))
        holder.append(RoutingClient(cloud, knowledge_name=settings.knowledge_collection, local=local))
        return holder[-1]

    wire = WireLog(spec['wire_dir'], ledger=Ledger(spec['ledger']))  # Private per-case call log + shared ledger.

    # Same fixed endpoints and retry settings as hosted.build_app (MLD §11: no blind retry).
    openai_client = recording_openai(wire, api_key=settings.openai_api_key.get_secret_value(), inner=inner)
    jev_client = TypeSafeClient(api_key=settings.typesafe_api_key.get_secret_value(),
                                base_url='https://api.typesafe.ai', compression=False,
                                retry=RetryPolicy(max_retries=0), http_client=recording_http(wire, inner))
    app = create_storage_app(connect=connect, api_token=settings.api_token.get_secret_value(),
                             collection_names=LOCAL_NAMES, knowledge_collection_name=settings.knowledge_collection,
                             answering=answering(openai_client=openai_client, jev_client=jev_client))

    def evidence():
        return {'wire_calls': wire.calls, 'knowledge_reads': holder[-1].knowledge_reads if holder else []}
    return app, local, evidence


# Flow: environment -> hosted settings validation -> fresh seeded stand-ins -> provider clients on the recording
# transport -> create_storage_app(answering=wiring.answering) -> case_runner runs the lifespan and the case ->
# evidence() hands back the recorded provider calls and the Knowledge source_ids returned to the app.
