# FastAPI CLI 0.0.34 entrypoint for the hosted backend (`[tool.fastapi] entrypoint`).
# `fastapi run` accepts no log configuration, so this host-only module installs the existing
# content-free server log policy with the standard library, then builds the app from the host environment.
import logging.config  # Standard library dictConfig; replaces default handlers that print paths and IPs.
import os
from shopunow.hosted import build_app, load_settings
from shopunow.server_logging import server_log_config

logging.config.dictConfig(server_log_config())  # Runs after the server's own logging setup, so it wins.
app = build_app(load_settings(os.environ))  # Missing configuration stops startup (fail closed).
