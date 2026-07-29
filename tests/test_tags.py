"""M25 — groups as tags: hand decisions outrank every job."""

from sonde.db import store


async def columns(table: str) -> set[str]:
    conn = await store._db()
    async with conn.execute(f"PRAGMA table_info({table})") as cur:
        return {row[1] for row in await cur.fetchall()}


async def test_a_fresh_database_has_the_new_columns(db):
    assert "archived_at" in await columns("groups")
    assert "decided_at" in await columns("group_members")


def test_the_new_columns_are_also_in_the_migration_list():
    """schema.sql only shapes a *new* database. Production's already exists, so
    a column missing from MIGRATIONS is an OperationalError on the first
    request after deploy — passing tests and a broken site."""
    assert ("archived_at", "TEXT") in store.MIGRATIONS["groups"]
    assert ("decided_at", "TEXT") in store.MIGRATIONS["group_members"]
