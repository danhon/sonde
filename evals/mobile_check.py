"""Live eval for M19 — does sonde actually fit on a phone?

`tests/test_responsive.py` asserts structure: tables are wrapped, the nav
collapses, grids have a mobile base. It cannot prove a page *fits*, because
fitting is a measurement a browser makes and a regex cannot. This does that
measurement.

The single check that matters is `scrollWidth <= innerWidth` on the document
element. If it fails, something on the page is wider than the screen and the
whole page scrolls sideways — which is what made the nav bug feel like several
unrelated bugs.

    uv run --with playwright python -m evals.mobile_check
    # first run only:
    uv run --with playwright playwright install chromium

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
VIEWPORTS = [(320, 568), (375, 667), (390, 844), (768, 1024)]

ROUTES = ["/", "/followers", "/influential", "/verified", "/groups",
          "/groups/discover", "/institutions", "/relationships", "/changes",
          "/ignored", "/settings",
          # Sorted and filtered variants: the widest tables in the app, and the
          # state a phone is most likely to be left in.
          "/followers?order=followers&direction=desc",
          "/relationships?order=attention&direction=desc",
          "/groups?slug=game-industry"]

# Apple and WCAG both land near this for a touch target.
MIN_TAP_PX = 44


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
        browser = await pw.chromium.launch()
        for width, height in VIEWPORTS:
            context = await browser.new_context(
                viewport={"width": width, "height": height},
                device_scale_factor=2, is_mobile=width < 768,
                has_touch=width < 768,
            )
            page = await context.new_page()
            for route in ROUTES:
                try:
                    response = await page.goto(f"{BASE_URL}{route}",
                                               wait_until="domcontentloaded",
                                               timeout=15000)
                except Exception as exc:  # noqa: BLE001
                    failures.append(f"{width}px {route}: unreachable ({exc})")
                    continue
                if response is not None and response.status >= 400:
                    failures.append(f"{width}px {route}: HTTP {response.status}")
                    continue
                checked += 1

                overflow = await page.evaluate("""() => {
                    const d = document.documentElement;
                    const over = d.scrollWidth - d.clientWidth;
                    if (over <= 0) return null;
                    // Name the widest offender, so the failure is actionable
                    // rather than just "something is too wide".
                    let worst = null, worstRight = 0;
                    for (const el of document.querySelectorAll("body *")) {
                        const r = el.getBoundingClientRect();
                        if (r.width === 0) continue;
                        if (r.right > worstRight) {
                            worstRight = r.right;
                            worst = el.tagName.toLowerCase()
                                  + (el.className && typeof el.className === "string"
                                     ? "." + el.className.trim().split(/\\s+/).slice(0, 3).join(".")
                                     : "");
                        }
                    }
                    return {over, worst, worstRight: Math.round(worstRight)};
                }""")
                if overflow:
                    failures.append(
                        f"{width}px {route}: scrolls {overflow['over']}px sideways "
                        f"— widest is {overflow['worst']} reaching "
                        f"{overflow['worstRight']}px"
                    )

                if width < 768:
                    small = await page.evaluate(f"""() => {{
                        const out = [];
                        for (const a of document.querySelectorAll("nav a, nav summary")) {{
                            const r = a.getBoundingClientRect();
                            if (r.height > 0 && r.height < {MIN_TAP_PX})
                                out.push((a.textContent || "").trim().slice(0, 18)
                                         + " " + Math.round(r.height) + "px");
                        }}
                        return out.slice(0, 4);
                    }}""")
                    if small:
                        failures.append(
                            f"{width}px {route}: nav targets under {MIN_TAP_PX}px "
                            f"— {', '.join(small)}"
                        )
            await context.close()
        await browser.close()

    print(f"\n  checked {checked} page renders across "
          f"{len(VIEWPORTS)} viewports\n")
    if failures:
        for line in failures:
            print(f"    FAIL  {line}")
        print(f"\n  {len(failures)} problems\n")
        return 1
    print("    no page scrolls sideways; every nav target is tappable\n")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
