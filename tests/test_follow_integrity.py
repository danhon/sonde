"""The follow list must survive a sweep that comes back wrong.

`replace_my_follows` rewrites the table wholesale, which is correct — see its
docstring for the two timestamp-diffing versions that were not. What it lacked
was any check that the list it was handed is plausible. A `200` carrying an
empty `follows` array and no cursor emptied the table, and with it every follow
URI sonde had created: `getFollows` returns subjects, not record keys, so the
undo button died and the follow-back button offered to follow people sonde
already followed, writing a second record.

Departures have had a rail against this shape of accident since M12. These are
the same rail on the other list, plus the recovery path that makes the URI
durable rather than merely carried.
"""

from __future__ import annotations

import pytest

from sonde.config import settings
from sonde.db import store
from sonde.db.store import ImplausibleFollowSweep


async def _follows(dids):
    """Seed the table the way a normal sweep would."""
    await store.replace_my_follows(dids)


async def test_an_empty_sweep_is_refused_rather_than_applied(db):
    await _follows([f"did:plc:{n}" for n in range(10)])

    with pytest.raises(ImplausibleFollowSweep):
        await store.replace_my_follows([])

    assert await store._scalar("SELECT COUNT(*) FROM my_follows") == 10


async def test_the_first_sweep_is_never_refused(db):
    """Nothing on record means nothing to lose — bootstrap must not trip."""
    await store.replace_my_follows([])
    assert await store._scalar("SELECT COUNT(*) FROM my_follows") == 0

    await store.replace_my_follows(["did:plc:a", "did:plc:b"])
    assert await store._scalar("SELECT COUNT(*) FROM my_follows") == 2


async def test_ordinary_unfollowing_is_not_policed(db):
    """The rail exists for wipes, not for a normal afternoon."""
    await _follows([f"did:plc:{n}" for n in range(100)])

    # Ten of a hundred, under the 25% default.
    await store.replace_my_follows([f"did:plc:{n}" for n in range(90)])
    assert await store._scalar("SELECT COUNT(*) FROM my_follows") == 90


async def test_a_wholesale_drop_is_refused(db):
    await _follows([f"did:plc:{n}" for n in range(100)])

    with pytest.raises(ImplausibleFollowSweep):
        await store.replace_my_follows([f"did:plc:{n}" for n in range(50)])

    assert await store._scalar("SELECT COUNT(*) FROM my_follows") == 100


async def test_a_short_list_is_guarded_only_against_an_empty_sweep(db):
    """A percentage of three people carries no information.

    Replacing a two-name list wholesale is how the unfollow-detection tests in
    test_depth.py are written, and they are not wrong — below the floor, a
    quarter is one person. The empty-sweep guard still applies at every size.
    """
    await _follows(["did:plc:a", "did:plc:b"])

    await store.replace_my_follows(["did:plc:c"])
    assert await store._scalar("SELECT COUNT(*) FROM my_follows") == 1

    with pytest.raises(ImplausibleFollowSweep):
        await store.replace_my_follows([])


async def test_the_floor_is_where_the_rail_starts(db):
    await _follows([f"did:plc:{n}" for n in range(store.MASS_UNFOLLOW_FLOOR)])

    with pytest.raises(ImplausibleFollowSweep):
        await store.replace_my_follows(["did:plc:0"])


async def test_the_operator_can_override_a_real_mass_unfollow(db):
    await _follows([f"did:plc:{n}" for n in range(100)])
    await store.replace_my_follows([f"did:plc:{n}" for n in range(5)], force=True)
    assert await store._scalar("SELECT COUNT(*) FROM my_follows") == 5


async def test_a_follow_uri_survives_an_ordinary_sweep(db):
    """The behaviour that already worked, pinned so the rail cannot break it."""
    await store.upsert_actor({"did": "did:plc:alice", "handle": "alice.test"})
    await store.record_my_follow("did:plc:alice", "at://me/app.bsky.graph.follow/abc")

    await store.replace_my_follows(["did:plc:alice", "did:plc:bob"])

    target = await store.follow_back_target("did:plc:alice")
    assert target["follow_uri"] == "at://me/app.bsky.graph.follow/abc"


async def test_a_lost_follow_uri_is_recovered_from_the_event_log(db):
    """Self-healing for a database this bug has already damaged.

    `record_my_follow` writes the URI into `follow_events.detail`, which is the
    one table that cannot be rebuilt from Bluesky. A row whose URI was wiped
    before the rail existed gets it back on the next sweep.
    """
    await store.upsert_actor({"did": "did:plc:alice", "handle": "alice.test"})
    await store.record_my_follow("did:plc:alice", "at://me/app.bsky.graph.follow/abc")

    # Simulate the damage the old code did: the row survives, the URI does not.
    db_conn = await store._db()
    await db_conn.execute("UPDATE my_follows SET follow_uri = NULL")
    await db_conn.commit()
    assert (await store.follow_back_target("did:plc:alice"))["follow_uri"] is None

    await store.replace_my_follows(["did:plc:alice"])

    target = await store.follow_back_target("did:plc:alice")
    assert target["follow_uri"] == "at://me/app.bsky.graph.follow/abc"


async def test_an_undone_follow_is_not_resurrected(db):
    """`unfollowed_back` is later than `followed_back`, so nothing comes back.

    Otherwise every sweep would re-offer an undo for a record that no longer
    exists, and deleteRecord on a dead rkey is a confusing way to find out.
    """
    await store.upsert_actor({"did": "did:plc:alice", "handle": "alice.test"})
    await store.record_my_follow("did:plc:alice", "at://me/app.bsky.graph.follow/abc")
    await store.forget_my_follow("did:plc:alice")

    assert await store.recoverable_follow_uris() == {}

    await store.replace_my_follows(["did:plc:alice"])
    target = await store.follow_back_target("did:plc:alice")
    assert target["follow_uri"] is None


async def test_a_refused_sweep_leaves_the_uris_alone(db):
    """The whole point: the wipe must not happen on the way to the exception."""
    await store.upsert_actor({"did": "did:plc:alice", "handle": "alice.test"})
    await store.record_my_follow("did:plc:alice", "at://me/app.bsky.graph.follow/abc")
    await store.replace_my_follows([f"did:plc:{n}" for n in range(20)]
                                   + ["did:plc:alice"])

    with pytest.raises(ImplausibleFollowSweep):
        await store.replace_my_follows([])

    target = await store.follow_back_target("did:plc:alice")
    assert target["follow_uri"] == "at://me/app.bsky.graph.follow/abc"
    assert target["already"]
