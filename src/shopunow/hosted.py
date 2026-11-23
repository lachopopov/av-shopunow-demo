# Python 3.12 / Pydantic 2.14.1 / chromadb 1.5.9; hosted configuration for the existing storage app.
# Reads only explicit environment values, validates them, and fails closed. No dotenv, no defaults for secrets.
from pydantic import BaseModel, ConfigDict, SecretStr, ValidationError  # Native validation; SecretStr hides values.
from shopunow.server_logging import CorrelatedServerApp  # Existing content-free log correlation.
from shopunow.storage_app import create_storage_app  # Existing five storage routes; reused unchanged.


class HostedSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid', hide_input_in_errors=True)  # Never echo values.
    chroma_host: str
    chroma_api_key: SecretStr
    chroma_tenant: str
    chroma_database: str
    api_token: SecretStr
    collections: tuple[str, str, str]
    knowledge_collection: str  # Slice 6: required, because the served answer route retrieves Knowledge.
    openai_api_key: SecretStr
    typesafe_api_key: SecretStr


def load_settings(environ):
    """Map host environment variables to validated settings; any missing or blank value is an error."""
    names = {'chroma_host': 'CHROMA_HOST', 'chroma_api_key': 'CHROMA_API_KEY', 'chroma_tenant': 'CHROMA_TENANT',
             'chroma_database': 'CHROMA_DATABASE', 'api_token': 'SHOPUNOW_API_TOKEN',
             'collections': 'SHOPUNOW_COLLECTIONS', 'knowledge_collection': 'SHOPUNOW_KNOWLEDGE_COLLECTION',
             'openai_api_key': 'OPENAI_API_KEY', 'typesafe_api_key': 'TYPESAFE_API_KEY'}
    values = {}
    for field, variable in names.items():
        value = (environ.get(variable) or '').strip()
        if not value:
            raise ValueError(f'Missing required setting {variable}.')  # Names only, never values.
        values[field] = value
    collections = tuple(part.strip() for part in values['collections'].split(','))
    if len(collections) != 3 or len(set(collections)) != 3 or not all(collections):
        raise ValueError('SHOPUNOW_COLLECTIONS needs three distinct names.')
    values['collections'] = collections
    # CD-099: only credentials come from the host. These SDK variables would redirect calls or add headers.
    for variable in ('OPENAI_BASE_URL', 'OPENAI_CUSTOM_HEADERS', 'OPENAI_ORG_ID', 'OPENAI_PROJECT_ID',
                     'TYPESAFE_BASE_URL'):
        if (environ.get(variable) or '').strip():
            raise ValueError(f'Unsupported provider override {variable}.')  # Name only, never the value.
    try:
        return HostedSettings(**values)
    except ValidationError:
        raise ValueError('Invalid hosted settings.') from None  # Drop details that could carry values.


def build_app(settings):
    """Existing storage app with a native Chroma Cloud client created lazily inside its own lifespan."""
    async def connect():  # Awaited once by the existing worker lifespan (sdk_worker.py).
        import chromadb  # Imported here so building the app never opens a connection.
        from chromadb.config import Settings
        return await chromadb.AsyncHttpClient(
            host=settings.chroma_host, port=443, ssl=True,
            headers={'x-chroma-token': settings.chroma_api_key.get_secret_value()},
            tenant=settings.chroma_tenant, database=settings.chroma_database,
            settings=Settings(_env_file=None, anonymized_telemetry=False, chroma_server_ssl_verify=True))
    from openai import OpenAI  # Clients connect lazily on first call; no retries (MLD §11: no blind retry).
    from typesafe_sdk import RetryPolicy, TypeSafeClient
    from shopunow.wiring import answering
    openai_client = OpenAI(api_key=settings.openai_api_key.get_secret_value(),
                           base_url='https://api.openai.com/v1', max_retries=0)  # Fixed public endpoint.
    jev_client = TypeSafeClient(api_key=settings.typesafe_api_key.get_secret_value(),
                                base_url='https://api.typesafe.ai', compression=False,
                                retry=RetryPolicy(max_retries=0))
    application = create_storage_app(connect=connect, api_token=settings.api_token.get_secret_value(),
                                     collection_names=settings.collections,
                                     knowledge_collection_name=settings.knowledge_collection,
                                     answering=answering(openai_client=openai_client, jev_client=jev_client))
    return CorrelatedServerApp(application)


# Flow: host environment -> validated settings (fail closed) -> provider clients + storage app with the
# served answer and handoff routes -> log correlation.
