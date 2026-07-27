"""Live eval for M3 — hydrate real followers and inspect the ranking.

Scores are a judgement call, so the only honest test is to look at the output.
This hydrates a slice of the real follower list, prints the top 25 with their
decomposition, and asserts the properties that must hold whatever the weights.

    uv run python -m evals.live_sweep          # first, to build evals/eval.db
    uv run python -m evals.score_check --limit 2000
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

from sonde.api.client import BlueskyClient
from sonde.db import store
from sonde.scoring import SELECTIVITY_FLOOR
from sonde.sync import profiles


async def main(limit: int) -> int:
    db_path = Path("evals/eval.db")
    if not db_path.exists():
        print("no evals/eval.db — run `python -m evals.live_sweep` first")
        return 2
    store.set_db_path(str(db_path))
    await store.connect()

    pending = len(await store.stale_actor_dids(limit=limit))
    if pending:
        print(f"hydrating {pending} actors (~{-(-pending // 25)} calls) …")
        client = BlueskyClient()
        started = time.monotonic()
        try:
            result = await profiles.hydrate(client, limit=limit)
        finally:
            await client.aclose()
        print(
            f"  {result['hydrated']} hydrated, {result['unservable']} unservable, "
            f"{result['api_calls']} calls in {time.monotonic() - started:.1f}s\n"
        )

    progress = await store.hydration_progress()
    rows = await store.ranked_followers(limit=25)

    print(f"  coverage: {progress['hydrated']:,}/{progress['total']:,} "
          f"({progress['pct']}%), {progress['unservable']} unservable\n")
    print("  rank  score  followers   follows  ✓  handle")
    print("  " + "─" * 68)
    for i, r in enumerate(rows, 1):
        tick = "✓" if r["verified_status"] == "valid" else " "
        print(
            f"  {i:4d}  {r['influence_score'] or 0:5.1f}  "
            f"{(r['followers_count'] or 0):9,}  {(r['follows_count'] or 0):8,}  {tick}  "
            f"{r['handle']}"
        )

    top = rows[0] if rows else None
    if top:
        print(f"\n  decomposition for {top['handle']}:")
        for c in top["components"]:
            state = "" if c["available"] else "  (unavailable)"
            print(f"    {c['key']:<18} {c['points']:5.2f}/{c['weight']:<3} "
                  f"{c['detail']}{state}")

    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str) -> None:
        checks.append((name, ok, detail))

    hydrated = [r for r in rows if r["followers_count"] is not None]
    check("top rows are hydrated", len(hydrated) == len(rows), f"{len(hydrated)}/{len(rows)}")
    check(
        "scores are ordered",
        all(
            (rows[i]["influence_score"] or 0) >= (rows[i + 1]["influence_score"] or 0)
            for i in range(len(rows) - 1)
        ),
        "monotonically non-increasing",
    )
    check(
        "scores are in range",
        all(0 <= (r["influence_score"] or 0) <= 100 for r in rows),
        f"{min((r['influence_score'] or 0) for r in rows):.1f}–"
        f"{max((r['influence_score'] or 0) for r in rows):.1f}",
    )
    check(
        "every row can explain itself",
        all(r["components"] for r in rows),
        "decomposition stored for all",
    )

    # The gate: nobody under the floor may earn selectivity points.
    db = await store._db()
    async with db.execute(
        "SELECT COUNT(*) FROM actors WHERE followers_count < ? AND influence_score > 60",
        (SELECTIVITY_FLOOR,),
    ) as cur:
        tiny_but_high = (await cur.fetchone())[0]
    check(
        "small accounts don't crack the top band",
        tiny_but_high == 0,
        f"{tiny_but_high} accounts under {SELECTIVITY_FLOOR:,} followers scored >60",
    )

    # Follow-back farms must rank below curated accounts of comparable reach.
    # An absolute threshold is the wrong test: until Institution and Affinity
    # are built, reach is a large share of the achievable score, so a big farm
    # legitimately scores mid-table. The property that must hold at every
    # milestone is the RELATIVE one.
    async with db.execute(
        """SELECT AVG(influence_score) FROM actors
            WHERE followers_count > 5000 AND follows_count > followers_count * 0.8"""
    ) as cur:
        farm_avg = (await cur.fetchone())[0]
    async with db.execute(
        """SELECT AVG(influence_score) FROM actors
            WHERE followers_count > 5000 AND follows_count < followers_count * 0.2"""
    ) as cur:
        curated_avg = (await cur.fetchone())[0]
    if farm_avg is not None and curated_avg is not None:
        check(
            "follow-back farms rank below curated peers",
            curated_avg > farm_avg,
            f"curated {curated_avg:.1f} vs follow-back {farm_avg:.1f} "
            f"(same reach band, >5k followers)",
        )

    async with db.execute(
        """SELECT handle, followers_count, follows_count, influence_score FROM actors
            WHERE followers_count > 5000 AND follows_count > followers_count * 0.8
            ORDER BY influence_score DESC LIMIT 1"""
    ) as cur:
        worst = await cur.fetchone()
    if worst:
        async with db.execute(
            "SELECT COUNT(*) FROM actors WHERE influence_score > ?", (worst["influence_score"],)
        ) as cur:
            above = (await cur.fetchone())[0]
        check(
            "top follow-back account is not near the top",
            above >= 25,
            f"{worst['handle']} at {worst['influence_score']:.1f} sits below "
            f"{above} other accounts",
        )

    async with db.execute(
        "SELECT AVG(influence_score) FROM actors WHERE verified_status = 'valid' "
        "AND influence_score IS NOT NULL"
    ) as cur:
        avg_verified = (await cur.fetchone())[0] or 0
    async with db.execute(
        "SELECT AVG(influence_score) FROM actors WHERE verified_status != 'valid' "
        "AND influence_score IS NOT NULL"
    ) as cur:
        avg_other = (await cur.fetchone())[0] or 0
    check("verified accounts rank above the rest on average",
          avg_verified > avg_other,
          f"verified {avg_verified:.1f} vs {avg_other:.1f}")

    print()
    failed = 0
    width = max(len(n) for n, _, _ in checks)
    for name, ok, detail in checks:
        if not ok:
            failed += 1
        print(f"  [{'PASS' if ok else 'FAIL'}] {name.ljust(width)}  {detail}")
    print(f"\n  {len(checks) - failed}/{len(checks)} checks passed")

    await store.close()
    return 1 if failed else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=1000, help="actors to hydrate")
    args = ap.parse_args()
    sys.exit(asyncio.run(main(args.limit)))
