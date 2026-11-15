"""Background jobs on Postgres (Procrastinate). One table, no broker.

Queues: `io` (ingest, validation, exports, purge, outbox housekeeping) and `calc` (PD/LGD/EAD/ECL compute,
lease schedules and close). Run `worker-io` and `worker-calc` as separate processes so a huge calculation
never starves validation. Locks give one compute per run and stop duplicate work on retries.
"""

from __future__ import annotations

import procrastinate

from komputy.db import sync_dsn

app = procrastinate.App(
    connector=procrastinate.PsycopgConnector(conninfo=sync_dsn()),
    import_paths=["komputy.apps.runs.jobs", "komputy.apps.leases.jobs", "komputy.platform.jobs"],
)

RETRY = procrastinate.RetryStrategy(max_attempts=3, exponential_wait=5)
