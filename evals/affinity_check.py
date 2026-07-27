"""Live eval for M6 — does affinity and institution matching add real signal?

Runs against the database built by the other evals; makes no API calls itself.

    uv run python -m evals.affinity_check
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from sonde.db import store


async def main() -> int:
    db_path = Path("evals/eval.db")
    if not db_path.exists():
        print("no evals/eval.db — run evals.live_sweep first")
        return 2
    store.set_db_path(str(db_path))
    await store.connect()
    db = await store._db()

    async def scalar(sql: str, params: tuple = ()) -> float:
        async with db.execute(sql, params) as cur:
            return (await cur.fetchone())[0] or 0

    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str) -> None:
        checks.append((name, ok, detail))

    reached = await scalar("SELECT COUNT(*) FROM actors WHERE affinity_sampled > 0")
    tracked = await scalar("SELECT COUNT(*) FROM follower_state WHERE is_current = 1")
    sources = await store.affinity_source_count()

    print(f"\n  index: {sources} sources, {reached:,.0f} of {tracked:,.0f} followers reached "
          f"({reached / max(tracked, 1) * 100:.1f}%)")

    check("index reaches a meaningful share", reached / max(tracked, 1) > 0.10,
          f"{reached / max(tracked, 1) * 100:.1f}% — the pilot's cheapest-source "
          f"rule managed 0.8%")

    # The signal must separate, or the 4,300 calls are wasted.
    hi = await scalar("SELECT AVG(followers_count) FROM actors WHERE affinity_sampled >= 5")
    lo = await scalar("SELECT AVG(followers_count) FROM actors WHERE affinity_sampled = 0")
    check("affinity correlates with standing", hi > lo * 5,
          f"mean followers {hi:,.0f} (affinity>=5) vs {lo:,.0f} (none)")

    # …and must not be a mere proxy for reach, or it adds nothing.
    hidden = await scalar(
        "SELECT COUNT(*) FROM actors WHERE affinity_sampled >= 3 "
        "AND (followers_count IS NULL OR followers_count < 5000)"
    )
    check("affinity surfaces what reach misses", hidden > 0,
          f"{hidden:,.0f} high-affinity accounts with little or no measured reach")

    scale = await store.affinity_scale()
    check("affinity scale is calibrated, not constant", scale is not None,
          f"p99 weighted overlap = {scale}")

    # Institutions
    matched = await scalar(
        "SELECT COUNT(*) FROM actors a JOIN follower_state fs USING (did) "
        "WHERE fs.is_current = 1 AND institution_name IS NOT NULL"
    )
    attested = await scalar(
        "SELECT COUNT(*) FROM actors a JOIN follower_state fs USING (did) "
        "WHERE fs.is_current = 1 AND institution_method = 'attested'"
    )
    print(f"  institutions: {matched:,.0f} matched, {attested:,.0f} attested")

    check("attested matches equal the institutional verifications", attested == 14,
          f"{attested:,.0f} — the 14 followers verified BY an institution")
    check("bio matching is filtered, not a substring test", matched < 120,
          f"{matched:,.0f} matched; an unfiltered substring test produced 152, "
          f"including 'NYT bestselling' and 'Ex-Microsoft'")

    roster = await scalar("SELECT COUNT(*) FROM institution_roster")
    check("rosters are enumerable", roster > 500,
          f"{roster:,.0f} verification records across institutions")

    # Nobody should rank themselves.
    me = await scalar(
        "SELECT COUNT(*) FROM actors a JOIN follower_state fs USING (did) "
        "WHERE fs.is_current = 1 AND a.handle = ? AND influence_score > 0",
        (store.settings.actor,),
    )
    check("the subject is excluded from its own ranking", me == 0,
          "self would otherwise top the affinity index")

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
    sys.exit(asyncio.run(main()))
