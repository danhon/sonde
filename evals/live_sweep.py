"""Live eval — run the real sweep against the real API and check the numbers.

Unit tests prove the logic is self-consistent. This proves it agrees with
Bluesky. Expectations come from a manual probe on 2026-07-26; drift beyond the
tolerances below means either the API changed or sonde did.

    uv run python -m evals.live_sweep            # full sweep, ~115 calls
    uv run python -m evals.live_sweep --head     # head sweep only, ~2 calls

Deliberately not part of `pytest`: it makes real network calls against a shared
rate limit.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

import time
from pathlib import Path

from sonde.api.client import BlueskyClient
from sonde.config import settings
from sonde.db import store
from sonde.sync import runner

# Measured 2026-07-26 against @danhon.com.
BASELINE = {
    "pages": 115,
    "enumerable": 10041,
    "reported": 11451,
    "verified": 147,
    "private": 1838,
    "handle_invalid": 37,
    "mean_page_yield": 87.3,
}

# Followers churn, so compare with tolerance rather than equality.
TOLERANCE_PCT = 12.0


class Check:
    def __init__(self) -> None:
        self.rows: list[tuple[str, bool, str]] = []

    def record(self, name: str, ok: bool, detail: str) -> None:
        self.rows.append((name, ok, detail))

    def near(self, name: str, got: float, expected: float, pct: float = TOLERANCE_PCT) -> None:
        drift = abs(got - expected) / max(expected, 1) * 100
        self.record(
            name,
            drift <= pct,
            f"got {got:,.1f}, expected ~{expected:,.1f} ({drift:.1f}% drift)",
        )

    def truth(self, name: str, ok: bool, detail: str) -> None:
        self.record(name, ok, detail)

    def report(self) -> int:
        width = max(len(n) for n, _, _ in self.rows)
        failed = 0
        print()
        for name, ok, detail in self.rows:
            flag = "PASS" if ok else "FAIL"
            if not ok:
                failed += 1
            print(f"  [{flag}] {name.ljust(width)}  {detail}")
        print()
        print(f"  {len(self.rows) - failed}/{len(self.rows)} checks passed")
        return failed


async def main(head_only: bool) -> int:
    check = Check()
    # A stable path so downstream evals (verified_check) can reuse the sweep
    # rather than paying 115 calls again.
    db_path = Path("evals/eval.db")
    db_path.parent.mkdir(exist_ok=True)
    if not head_only and db_path.exists():
        db_path.unlink()  # a full eval always starts from a clean backfill
    store.set_db_path(str(db_path))
    await store.connect()

    client = BlueskyClient()
    started = time.monotonic()
    print(f"sweeping {settings.actor} at {settings.rate_limit_per_second} req/s …")

    try:
        result = await (runner.head_sweep(client) if head_only else runner.full_sweep(client))
    finally:
        await client.aclose()

    elapsed = time.monotonic() - started
    counts = await store.counts()

    # --- structural invariants: these must hold regardless of churn ---
    check.truth(
        "sweep completed",
        result["status"] == "ok",
        f"status={result['status']}",
    )
    check.truth(
        "first sweep is backfill",
        result.get("backfilled") is True if not head_only else True,
        "arrivals suppressed on day one" if not head_only else "n/a for head sweep",
    )
    check.truth(
        "no departures on a backfill sweep",
        result.get("departures", 0) == 0,
        f"departures={result.get('departures', 0)}",
    )

    if head_only:
        check.truth(
            "head sweep is cheap",
            result["pages"] <= 3,
            f"{result['pages']} page(s), {result['api_calls']} calls in {elapsed:.1f}s",
        )
        failed = check.report()
        await store.close()
        return 1 if failed else 0

    # --- agreement with the measured baseline ---
    check.near("pages walked", result["pages"], BASELINE["pages"], 15)
    check.near("followers enumerable", counts["tracked"], BASELINE["enumerable"])
    check.near("verified followers", counts["verified"], BASELINE["verified"], 30)
    check.near("private (!no-unauthenticated)", counts["private"], BASELINE["private"], 30)

    if counts["reported"]:
        check.near("followersCount reported", counts["reported"], BASELINE["reported"])
        gap = counts["reported"] - counts["tracked"]
        check.truth(
            "reported exceeds enumerable",
            gap > 0,
            f"{gap:,} accounts counted but unservable — a permanent gap, not a bug",
        )

    # --- the pagination invariant, observed live ---
    yield_ = result["seen"] / max(result["pages"], 1)
    check.near("mean page yield", yield_, BASELINE["mean_page_yield"], 15)
    check.truth(
        "pages arrive short",
        yield_ < settings.page_size,
        f"mean {yield_:.1f} of {settings.page_size} requested — "
        "stopping on a short page would end the sweep early",
    )

    db = await store._db()
    async with db.execute(
        "SELECT COUNT(*) FROM actors WHERE handle = 'handle.invalid'"
    ) as cur:
        broken = (await cur.fetchone())[0]
    check.truth(
        "handle.invalid actors retained",
        broken > 0,
        f"{broken} kept (baseline {BASELINE['handle_invalid']}) — still followers",
    )

    # --- newest-first ordering, which the head sweep depends on ---
    async with db.execute(
        "SELECT account_created_at FROM actors a JOIN follower_state fs USING (did) "
        "WHERE account_created_at IS NOT NULL ORDER BY fs.list_rank LIMIT 200"
    ) as cur:
        head_dates = [r[0] for r in await cur.fetchall()]
    async with db.execute(
        "SELECT account_created_at FROM actors a JOIN follower_state fs USING (did) "
        "WHERE account_created_at IS NOT NULL ORDER BY fs.list_rank DESC LIMIT 200"
    ) as cur:
        tail_dates = [r[0] for r in await cur.fetchall()]
    if head_dates and tail_dates:
        check.truth(
            "list is newest-follow-first",
            max(head_dates) > max(tail_dates),
            f"newest at head {max(head_dates)[:10]} vs tail {max(tail_dates)[:10]} — "
            "the head sweep depends on this",
        )

    print(
        f"\n  {result['pages']} pages · {result['api_calls']} calls · "
        f"{elapsed:.1f}s · {counts['tracked']:,} followers"
    )
    failed = check.report()

    out = Path("evals/last_run.json")
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({"result": result, "counts": counts}, indent=2) + "\n")
    print(f"  wrote {out}")

    await store.close()
    return 1 if failed else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--head", action="store_true", help="head sweep only (~2 calls)")
    args = ap.parse_args()
    sys.exit(asyncio.run(main(args.head)))
