"""Live eval for M2 — does the /verified view agree with the manual probe?

Reuses the database written by `evals.live_sweep`, so run that first.
Expectations from the 2026-07-26 probe: 147 verified, 0 trusted verifiers, and
14 verified by an institution across 7 distinct issuers (Wired 6, WaPo 3, FT 2,
NBC 1, ms.now 1, NYT 1).

    uv run python -m evals.live_sweep && uv run python -m evals.verified_check
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from sonde.db import store

BASELINE = {"verified": 147, "institutional": 14, "issuers": 7, "trusted": 0}
EXPECTED_INSTITUTIONS = {
    "wired.com", "washingtonpost.com", "financialtimes.com",
    "nbcnews.com", "ms.now", "nytimes.com",
}


async def main(db_path: str) -> int:
    store.set_db_path(db_path)
    await store.connect()
    summary = await store.verified_summary()

    print(f"\n  verified: {summary['total']}   "
          f"institutional: {summary['institutional']}   "
          f"issuers: {summary['issuer_count']}   "
          f"trusted verifiers: {summary['trusted_verifiers']}\n")

    for g in summary["groups"]:
        tag = "bluesky" if g.get("is_bluesky") else "institution"
        print(f"    {g['count']:4d}  {g['issuer']:<24} ({tag})")

    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str) -> None:
        checks.append((name, ok, detail))

    check("verified total", summary["total"] == BASELINE["verified"],
          f"{summary['total']} vs {BASELINE['verified']}")
    check("institutional total", summary["institutional"] == BASELINE["institutional"],
          f"{summary['institutional']} vs {BASELINE['institutional']}")
    check("distinct issuers", summary["issuer_count"] == BASELINE["issuers"],
          f"{summary['issuer_count']} vs {BASELINE['issuers']}")
    check("no trusted verifiers", summary["trusted_verifiers"] == BASELINE["trusted"],
          f"{summary['trusted_verifiers']}")

    found = {g["issuer"] for g in summary["groups"] if not g.get("is_bluesky")}
    check("expected institutions present", EXPECTED_INSTITUTIONS <= found,
          f"missing {sorted(EXPECTED_INSTITUTIONS - found) or 'none'}")

    bluesky = next((g for g in summary["groups"] if g.get("is_bluesky")), None)
    share = (bluesky["count"] / summary["total"] * 100) if bluesky and summary["total"] else 0
    check("Bluesky issues the large majority", share > 85,
          f"{share:.0f}% verified by bsky.app — this is why Institution "
          f"scoring cannot rely on the issuer alone")

    check("institutions sort before Bluesky",
          not summary["groups"][0].get("is_bluesky"),
          "institutional issuers lead the page")

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
    path = sys.argv[1] if len(sys.argv) > 1 else str(Path("evals/eval.db"))
    sys.exit(asyncio.run(main(path)))
