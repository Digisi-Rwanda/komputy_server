"""Integration tests use their own database (ledgerline_engine_test), never the web app's."""

import os

os.environ.setdefault("LOG_FORMAT", "console")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://ledgerline:ledgerline@localhost:5440/ledgerline_engine_test")
os.environ.setdefault("STORAGE_BUCKET_NAME", "ledgerline-test")
