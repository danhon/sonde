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
