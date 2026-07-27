import pytest

from sonde.db import store


@pytest.fixture
async def db(tmp_path):
    """A fresh, migrated database per test."""
    await store.connect(str(tmp_path / "test.db"))
    yield store
    await store.close()
