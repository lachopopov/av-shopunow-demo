# Python 3.12 / Pydantic 2.14.1; hosted configuration fails closed and reuses the existing storage app.
import unittest
from shopunow.hosted import build_app, load_settings
from shopunow.server_logging import CorrelatedServerApp

ENV = {'CHROMA_HOST': 'api.example.test', 'CHROMA_API_KEY': 'k', 'CHROMA_TENANT': 't',
       'CHROMA_DATABASE': 'd', 'SHOPUNOW_API_TOKEN': 'token',
       'SHOPUNOW_COLLECTIONS': 'profiles-x,conversations-x,escalations-x',
       'SHOPUNOW_KNOWLEDGE_COLLECTION': 'knowledge-x', 'OPENAI_API_KEY': 'ok', 'TYPESAFE_API_KEY': 'tk'}


class HostedSettingsTests(unittest.TestCase):
    def test_complete_environment_gives_explicit_settings(self):
        # Break caught: a configuration value is silently dropped or renamed between host and app.
        settings = load_settings(ENV)
        self.assertEqual(settings.collections, ('profiles-x', 'conversations-x', 'escalations-x'))
        self.assertEqual(settings.knowledge_collection, 'knowledge-x')
        self.assertEqual(settings.api_token.get_secret_value(), 'token')
        self.assertEqual((settings.openai_api_key.get_secret_value(), settings.typesafe_api_key.get_secret_value()),
                         ('ok', 'tk'))  # Slice 6: the served answer route needs both provider keys.

    def test_missing_or_blank_values_fail_closed_without_echoing_secrets(self):
        # Break caught: the host starts with no token or no database, or an error message leaks a secret.
        for name in ENV:
            for broken in ({k: v for k, v in ENV.items() if k != name}, dict(ENV, **{name: '  '})):
                with self.subTest(name=name), self.assertRaises(ValueError) as caught:
                    load_settings(broken)
                self.assertNotIn('token', str(caught.exception).replace('API_TOKEN', ''))

    def test_provider_endpoint_overrides_stop_startup(self):
        # Break caught (review M3, CD-099): a host variable silently redirects model calls or adds headers.
        for name in ('OPENAI_BASE_URL', 'OPENAI_CUSTOM_HEADERS', 'OPENAI_ORG_ID', 'OPENAI_PROJECT_ID',
                     'TYPESAFE_BASE_URL'):
            with self.subTest(name), self.assertRaises(ValueError):
                load_settings(dict(ENV, **{name: 'https://elsewhere.invalid'}))

    def test_the_openai_client_uses_the_fixed_public_endpoint(self):
        from unittest import mock
        with mock.patch('openai.OpenAI') as built:
            build_app(load_settings(ENV))
        self.assertEqual(built.call_args.kwargs['base_url'], 'https://api.openai.com/v1')

    def test_collections_must_be_three_distinct_names(self):
        # Break caught: two record types share one collection, mixing owners' records.
        for value in ('a,b', 'a,a,b', 'a,b,c,d'):
            with self.subTest(value), self.assertRaises(ValueError):
                load_settings(dict(ENV, SHOPUNOW_COLLECTIONS=value))

    def test_app_is_the_existing_storage_app_behind_the_log_correlator(self):
        # Break caught: hosted traffic bypasses the content-free server log correlation.
        application = build_app(load_settings(ENV))
        self.assertIsInstance(application, CorrelatedServerApp)
        self.assertIsNotNone(application.application.answering)  # Slice 6: /interactions is served.



class TelemetryTests(unittest.TestCase):
    def test_platform_cannot_switch_on_automatic_telemetry_export(self):
        # Break caught: a host setting (seen on FastAPI Cloud) makes FastAPI configure OpenTelemetry
        # export at startup, which could send request data to a collector nobody approved.
        import os
        from unittest import mock
        from starlette.testclient import TestClient  # Native lifespan runner; no network.
        from shopunow.api import create_app
        with mock.patch.dict(os.environ, {'FASTAPI_OTEL_AUTO_CONFIGURE': 'true',
                                           'OTEL_EXPORTER_OTLP_ENDPOINT': 'http://collector.invalid:4318'}):
            with self.assertNoLogs('fastapi', level='WARNING'), TestClient(create_app()):
                pass


if __name__ == '__main__':
    unittest.main()
