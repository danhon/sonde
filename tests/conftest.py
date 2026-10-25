from pathlib import Path

import pytest

from sonde.db import store


@pytest.fixture
async def db(tmp_path):
    """A fresh, migrated database per test.

    The override is set as well as the path passed, and cleared on the way out.
    Without it, the first `_db()` inside a test resolved back to
    `settings.db_path` and silently reopened the real `./sonde.db`.
    """
    store.set_db_path(str(tmp_path / "test.db"))
    await store.connect()
    yield store
    await store.close()
    store.set_db_path(None)


@pytest.fixture(autouse=True)
def _no_real_email(monkeypatch):
    """Fail loudly if a test ever sends a real email.

    `sonde/config.py` loads the real `.env` at import time, so a dev machine
    with SMTP configured for production makes `settings.smtp_configured` true
    inside pytest too — a test that exercises `digest.run_digest()` without
    mocking `digest.send()` then hits real SMTP with real credentials, and an
    actual email goes out to `NOTIFY_TO` built from whatever throwaway data
    that test's fixture left lying around. Reproduced 2026-08-18:
    `pytest tests/test_digest.py` sent a live "not authenticating" alarm
    built from a one-row test database. Forcing the fields `smtp_configured`
    reads to empty restores the behavior the tests already assume (SMTP not
    configured) regardless of the local `.env`; blocking `aiosmtplib.send`
    outright is the backstop in case something still calls it.
    """
    import aiosmtplib

    from sonde.config import settings

    # Settings is a frozen dataclass: smtp_host/smtp_username/notify_to are
    # per-instance fields already set from the real .env, so patching them
    # (even on the class) can't shadow the instance's own values. The
    # `smtp_configured` property has no such instance entry, so overriding it
    # directly is what actually takes effect.
    monkeypatch.setattr(settings.__class__, "smtp_configured", False, raising=False)

    async def _forbidden(*args, **kwargs):
        raise AssertionError(
            "a test tried to send real email via aiosmtplib.send() — "
            "mock sonde.notify.digest.send instead"
        )

    monkeypatch.setattr(aiosmtplib, "send", _forbidden)


@pytest.fixture(autouse=True)
def _no_writes_to_the_real_database(tmp_path_factory):
    """Fail loudly if a test ever opens the configured production database.

    This is not hypothetical: `connect(path)` used to set the path but not the
    override, so every `db`-fixture test reopened `./sonde.db` on its first
    query. Nine rows of test fixtures ended up in the working copy, and tests
    collided with each other through it.
    """
    from sonde.config import settings

    real = Path(settings.db_path).resolve()
    yield
    if store._db_path is not None:
        assert Path(store._db_path).resolve() != real, (
            f"a test connected to the real database at {real}"
        )
