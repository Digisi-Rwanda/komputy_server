"""Integration tests use their own database (komputy_engine_test), never the web app's."""

import os

os.environ.setdefault("LOG_FORMAT", "console")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://komputy:komputy@localhost:5440/komputy_engine_test")
os.environ.setdefault("STORAGE_BUCKET_NAME", "komputy-test")
