"""Is what's deployed still what's on the default branch?

`make deploy` builds from whatever is checked out on the host, stamping the
commit into the image. That stamp is the only thing the running container knows
about itself, and it cannot tell whether the branch has moved on since — so this
asks GitHub.

The repository is private, so this needs a token and degrades to silence without
one: the panel still says what is running and when it was built, which is useful
on its own, and simply cannot say whether that is current.

Deliberately on demand rather than polled. Nobody needs to know this except in
the moment they wonder, and a background poll would spend rate limit answering a
question nobody asked. A short cache covers double-clicks and reloads.
"""

from __future__ import annotations

import logging
import time

import httpx

from sonde.config import settings

log = logging.getLogger("sonde.version")

API = "https://api.github.com"
# Long enough that clicking twice costs one call, short enough that it still
# answers the question after you have just deployed.
CACHE_SECONDS = 60
_cache: dict[str, tuple[float, dict]] = {}


def _cached(key: str) -> dict | None:
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
        return hit[1]
    return None


async def build_status() -> dict:
    """What is running, and how far behind the branch it is.

    `status` is the single field a caller needs:

      current     deployed commit is the branch head
      behind      the branch has moved on; `behind` counts by how much
      ahead       deployed something not pushed — building from a dirty or
                  local-only checkout, which is worth knowing
      diverged    both, so the deploy came from a branch that was never merged
      unknown     no token, no network, or the commit is not on this repo
    """
    running = {
        "sha": settings.build_sha,
        "built_at": settings.build_time,
        "branch": settings.github_branch,
        "repo": settings.github_repo,
        "status": "unknown",
        "detail": "",
    }
    if settings.build_sha in ("", "dev"):
        running["detail"] = ("This build carries no commit stamp — it was not "
                             "built by `make deploy`.")
        return running
    if not settings.can_check_for_updates:
        running["detail"] = ("Set GITHUB_TOKEN to compare this against "
                             f"{settings.github_branch}. The repository is "
                             "private, so the check needs a credential.")
        return running

    cached = _cached(settings.build_sha)
    if cached is not None:
        return cached

    url = (f"{API}/repos/{settings.github_repo}/compare/"
           f"{settings.build_sha}...{settings.github_branch}")
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            response = await client.get(url, headers={
                "Authorization": f"Bearer {settings.github_token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            })
    except httpx.HTTPError as exc:
        running["detail"] = f"Could not reach GitHub: {exc.__class__.__name__}"
        return running

    if response.status_code == 404:
        # Either the token cannot see the repo, or the deployed commit is not in
        # it at all — which happens when a deploy is built from a local branch.
        running["detail"] = ("GitHub does not recognise this commit, or the "
                             "token cannot read the repository.")
        return running
    if response.status_code != 200:
        running["detail"] = f"GitHub returned {response.status_code}."
        return running

    body = response.json()
    behind = body.get("ahead_by") or 0      # commits branch has that we do not
    ahead = body.get("behind_by") or 0      # commits we have that branch lacks
    running.update({
        "behind": behind,
        "ahead": ahead,
        "branch_sha": (body.get("commits") or [{}])[-1].get("sha", "")[:7],
        "compare_url": body.get("html_url", ""),
    })

    if behind and ahead:
        running["status"] = "diverged"
    elif behind:
        running["status"] = "behind"
    elif ahead:
        running["status"] = "ahead"
    else:
        running["status"] = "current"

    running["commits"] = [
        {"sha": c.get("sha", "")[:7],
         "message": (c.get("commit", {}).get("message") or "").split("\n")[0],
         "date": (c.get("commit", {}).get("author", {}).get("date") or "")[:10]}
        for c in reversed(body.get("commits") or [])
    ][:10]

    _cache[settings.build_sha] = (time.monotonic(), running)
    return running
