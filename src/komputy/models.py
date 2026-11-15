"""Import every engine model so metadata is complete (Alembic, purge, tests)."""


def load_all() -> None:
    import komputy.apps.collateral.models
    import komputy.apps.leases.models
    import komputy.apps.results.models
    import komputy.apps.runs.models
    import komputy.apps.runs.validation_jobs
    import komputy.apps.segments.models
    import komputy.events.notify
    import komputy.platform.models  # noqa: F401
