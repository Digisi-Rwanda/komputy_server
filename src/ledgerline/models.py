"""Import every engine model so metadata is complete (Alembic, purge, tests)."""


def load_all() -> None:
    import ledgerline.apps.collateral.models
    import ledgerline.apps.leases.models
    import ledgerline.apps.results.models
    import ledgerline.apps.runs.models
    import ledgerline.apps.runs.validation_jobs
    import ledgerline.apps.segments.models
    import ledgerline.events.notify
    import ledgerline.platform.models  # noqa: F401
