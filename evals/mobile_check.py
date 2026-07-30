"""Live eval for M19 — does sonde actually fit on a phone?

`tests/test_responsive.py` asserts structure: tables are wrapped, the nav
collapses, grids have a mobile base. It cannot prove a page *fits*, because
fitting is a measurement a browser makes and a regex cannot. This does that
measurement.

Three things are measured, on every route, at seven widths, in two engines:

  overflow  `scrollWidth <= innerWidth` — nothing wider than the screen
  reachable every section is visible, or reachable by opening the menu
  intact    nav links do not overlap the activity strip or escape the nav

The middle one exists because overflow and tap-size checks both passed a build
whose desktop nav had no visible links at all. Everything here asks
`checkVisibility()` rather than measuring boxes: the regression's links had
real bounding rects and were never painted.

    uv run --with playwright python -m evals.mobile_check
    # first run only:
    uv run --with playwright playwright install chromium webkit

Runs against a live server. Defaults to a locally started one; point it at the
deployed site with SONDE_BASE_URL.
"""

from __future__ import annotations

import asyncio
import os
import sys

BASE_URL = os.environ.get("SONDE_BASE_URL", "http://127.0.0.1:8000")

# Real device widths. 320 is the narrowest phone still in use (iPhone SE 1st
# gen); 390 is a current iPhone; 768 is the tablet breakpoint, included because
# it sits just above `sm:` and is where a collapse rule most often goes wrong.
VIEWPORTS = [(320, 568), (375, 667), (390, 844), (768, 1024),
             # Desktop widths too. The regression that made this necessary was
             # a *desktop* nav collapse, and a mobile-only eval passed it.
             (1024, 768), (1280, 800), (1440, 900)]

ROUTES = ["/", "/followers", "/influential", "/verified", "/circles",
          "/circles/discover", "/institutions", "/relationships", "/changes",
          "/ignored", "/settings",
          # Sorted and filtered variants: the widest tables in the app, and the
          # state a phone is most likely to be left in.
          "/followers?order=followers&direction=desc",
          "/relationships?order=attention&direction=desc",
          "/circles?slug=game-industry",
          # Gained bios under each handle in the Circles rename;
          # the widest table in the app and not previously covered.
          "/circles/discover/307",
          # A profile: never covered before, and now carries the
          # circle toggle grid — the most-tapped control in the app.
          "/followers/did:plc:cfcnluzwlof3qkppfmlmuxhm"]

PROFILE_ROUTE = "/followers/did:plc:cfcnluzwlof3qkppfmlmuxhm"

# Apple and WCAG both land near this for a touch target.
MIN_TAP_PX = 44

# Every section of the app. Being in the DOM is not enough — these must be
# visible and clickable.
EXPECTED_LINKS = ["/followers", "/influential", "/verified", "/circles",
                  "/institutions", "/relationships", "/changes", "/ignored",
                  "/settings"]

OVERFLOW_JS = """() => {
    const d = document.documentElement;
    const over = d.scrollWidth - d.clientWidth;
    if (over <= 0) return null;
    // Name the widest offender, so a failure is actionable rather than just
    // "something is too wide" — but skip anything already inside a horizontal
    // scroll container. A wide table in an overflow-x-auto box is clipped and
    // is NOT what is widening the page; reporting it sends you to fix the one
    // thing that was already correct.
    const clipped = (el) => {
        for (let p = el.parentElement; p; p = p.parentElement) {
            const o = getComputedStyle(p).overflowX;
            if (o === "auto" || o === "scroll" || o === "hidden") return true;
        }
        return false;
    };
    let worst = null, worstRight = 0;
    for (const el of document.querySelectorAll("body *")) {
        const r = el.getBoundingClientRect();
        if (r.width === 0 || clipped(el)) continue;
        if (r.right > worstRight) {
            worstRight = r.right;
            worst = el.tagName.toLowerCase()
                  + (el.className && typeof el.className === "string"
                     ? "." + el.className.trim().split(/\\s+/).slice(0, 3).join(".")
                     : "");
        }
    }
    return {over, worst, worstRight: Math.round(worstRight)};
}"""

# Chrome that collapses onto itself. Kept alongside the visibility check
# because the two catch different shapes of the same class of bug.
NAV_OVERLAP_JS = """() => {
    const inClosedMenu = (el) => {
        const d = el.closest("details");
        return !!d && !d.open;
    };
    const nav = document.querySelector("nav");
    const strip = document.getElementById("activity");
    if (!nav || !strip) return null;
    const s = strip.getBoundingClientRect();
    const bad = [];
    for (const a of nav.querySelectorAll("a[href]")) {
        if (a.checkVisibility && !a.checkVisibility({checkVisibilityCSS: true}))
            continue;
        const r = a.getBoundingClientRect();
        if (r.height === 0) continue;
        const overlaps = r.top < s.bottom && r.bottom > s.top;
        if (overlaps) bad.push((a.textContent || "").trim().slice(0, 14));
        if (r.bottom > nav.getBoundingClientRect().bottom + 1)
            bad.push((a.textContent || "").trim().slice(0, 14) + " outside nav");
    }
    return bad.length ? bad.slice(0, 5) : null;
}"""

# `checkVisibility` asks whether the element is actually PAINTED, which a
# bounding box does not. The regression laid its links out — non-zero rects,
# display:flex, visibility:visible — inside a closed <details>, so the browser
# never painted them. Every rect-based test called that "visible" and passed a
# nav the user could not see. This is the predicate that tells them apart.
VISIBLE_NAV_JS = """() => {
    const out = [];
    for (const a of document.querySelectorAll("nav a[href]")) {
        const painted = a.checkVisibility
            ? a.checkVisibility({checkVisibilityCSS: true, checkOpacity: true})
            : a.getBoundingClientRect().height > 0;
        if (painted) out.push(new URL(a.href, location.origin).pathname);
    }
    return out;
}"""

TAP_JS = f"""() => {{
    const out = [];
    for (const el of document.querySelectorAll(
            "nav a, nav summary, [data-circle-chip]")) {{
        if (el.checkVisibility && !el.checkVisibility({{checkVisibilityCSS: true}}))
            continue;
        const r = el.getBoundingClientRect();
        if (r.height > 0 && r.height < {MIN_TAP_PX})
            out.push((el.textContent || "").trim().slice(0, 18)
                     + " " + Math.round(r.height) + "px");
    }}
    return out.slice(0, 4);
}}"""


async def main() -> int:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("playwright not installed. Run:\n"
              "  uv run --with playwright python -m evals.mobile_check")
        return 2

    failures: list[str] = []
    checked = 0

    async with async_playwright() as pw:
        # Both engines, always. The nav regression was reported against
        # desktop Safari, and although it turned out to reproduce in Chromium
        # too, a single-engine eval is not something to rely on for layout.
        for engine in ("chromium", "webkit"):
            browser = await getattr(pw, engine).launch()
            for width, height in VIEWPORTS:
                context = await browser.new_context(
                    viewport={"width": width, "height": height},
                    device_scale_factor=2, is_mobile=width < 768,
                    has_touch=width < 768,
                )
                page = await context.new_page()
                for route in ROUTES:
                    where = f"{engine} {width}px {route}"
                    try:
                        response = await page.goto(
                            f"{BASE_URL}{route}", wait_until="domcontentloaded",
                            timeout=20000)
                    except Exception as exc:  # noqa: BLE001
                        failures.append(f"{where}: unreachable ({exc})")
                        continue
                    if response is not None and response.status >= 400:
                        failures.append(f"{where}: HTTP {response.status}")
                        continue
                    checked += 1

                    overflow = await page.evaluate(OVERFLOW_JS)
                    if overflow:
                        failures.append(
                            f"{where}: scrolls {overflow['over']}px sideways — "
                            f"widest is {overflow['worst']} reaching "
                            f"{overflow['worstRight']}px")

                    # Every section must be REACHABLE, and the chrome must not
                    # collapse onto itself. Overflow and tap-size checks alone
                    # passed a nav that had shrunk from 95px to 35px.
                    overlap = await page.evaluate(NAV_OVERLAP_JS)
                    if overlap:
                        failures.append(
                            f"{where}: nav links overlap the activity strip or "
                            f"escape the nav box — {overlap}")

                    # Reachability, asked the same way at every width. An earlier
                    # version assumed ">=768px means a full row is showing",
                    # which is a guess about the breakpoint rather than a
                    # question about the app — and it failed the moment the
                    # split moved from sm: to xl:. What matters is only whether
                    # a person can get to every section.
                    visible = set(await page.evaluate(VISIBLE_NAV_JS))
                    missing = set(EXPECTED_LINKS) - visible
                    opened_menu = False
                    if missing:
                        # `details#nav`, not "the first details in the nav".
                        # The build-status panel is also a <details> up there
                        # and, being inside the desktop-only row, is invisible
                        # at narrow widths — so matching positionally reported
                        # "no usable menu" on every page at 1024px.
                        summary = page.locator("nav details#nav summary")
                        # `is_visible` before clicking, and a short timeout: the
                        # regression left both the row and the menu button
                        # unpainted, and a blind click just hangs for 30s
                        # instead of saying so.
                        usable = (await summary.count() > 0
                                  and await summary.first.is_visible())
                        if not usable:
                            failures.append(
                                f"{where}: {len(missing)} sections not visible "
                                f"and no usable menu to reach them — "
                                f"{sorted(missing)}")
                        else:
                            try:
                                await summary.first.click(timeout=3000)
                                opened_menu = True
                            except Exception as exc:  # noqa: BLE001
                                failures.append(
                                    f"{where}: menu button will not open "
                                    f"({type(exc).__name__})")
                            if opened_menu:
                                visible = set(await page.evaluate(VISIBLE_NAV_JS))
                                missing = set(EXPECTED_LINKS) - visible
                                if missing:
                                    failures.append(
                                        f"{where}: {len(missing)} sections still "
                                        f"unreachable after opening the menu — "
                                        f"{sorted(missing)}")

                    # Touch sizing only matters where there is touch, but the
                    # menu is measured wherever it is the way in.
                    if width < 768 or opened_menu:
                        small = await page.evaluate(TAP_JS)
                        if small:
                            failures.append(
                                f"{where}: nav targets under {MIN_TAP_PX}px — "
                                f"{', '.join(small)}")
                await context.close()

            # One interaction check per engine, at a desktop width. Everything
            # above measures a rendered page; this drives the one control that
            # only works if its JavaScript is right.
            #
            # It earns its place: the chip handler read `form.action` to find
            # where to post, and because the form contains a control NAMED
            # "action", that property resolved to the <input> instead of the
            # URL. Every request went to /followers/[object HTMLInputElement]
            # and 405'd. Server-side tests all passed — the markup was correct
            # and the endpoint was correct; only the browser joined them up.
            #
            # The POST is intercepted and answered here rather than allowed
            # through, so this writes nothing: an eval must stay safe to point
            # at a populated database.
            context = await browser.new_context(viewport={"width": 1280, "height": 900})
            page = await context.new_page()
            posted: list[str] = []

            async def capture(route):
                posted.append(route.request.url)
                await route.fulfil(status=204) if hasattr(route, "fulfil") \
                    else await route.fulfill(status=204)

            await page.route("**/tags", capture)
            try:
                await page.goto(f"{BASE_URL}{PROFILE_ROUTE}",
                                wait_until="domcontentloaded", timeout=20000)
                chip = page.locator('[data-circle-chip]').first
                if await chip.count():
                    before = await chip.get_attribute("aria-pressed")
                    await chip.click()
                    await page.wait_for_timeout(400)
                    after = await chip.get_attribute("aria-pressed")
                    if not posted:
                        failures.append(
                            f"{engine} chip click posted nothing — the handler "
                            f"did not run")
                    elif "[object" in posted[0] or "/tags" not in posted[0]:
                        failures.append(
                            f"{engine} chip posted to a malformed URL: {posted[0]}")
                    elif before == after:
                        failures.append(
                            f"{engine} chip did not change state on click "
                            f"(stayed aria-pressed={before})")
                    checked += 1
            except Exception as exc:  # noqa: BLE001
                failures.append(f"{engine} chip check errored: {exc!r}")
            await context.close()
            await browser.close()

    print(f"\n  checked {checked} page renders "
          f"across 2 engines x {len(VIEWPORTS)} viewports\n")
    if failures:
        for line in failures:
            print(f"    FAIL  {line}")
        print(f"\n  {len(failures)} problems\n")
        return 1
    print("    no page scrolls sideways; every section reachable; "
          "every nav target tappable\n")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
