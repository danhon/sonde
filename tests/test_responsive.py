"""M19 — structural invariants that keep the app usable on a phone.

**These cannot prove a page fits.** Only a browser can measure whether
`scrollWidth` exceeds the viewport, and that is `evals/mobile_check.py`. What
these do is catch the regressions that caused the reported breakage in the first
place — a table added without its wrapper, a nav link row that stops collapsing
— on every test run rather than whenever someone next picks up a phone.

The bug that prompted them: the nav was ten links in a `flex` row with no
`flex-wrap`, so it overflowed the viewport and dragged the whole page sideways.
Several unrelated pages looked broken because they were being pushed off-screen.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

TEMPLATES = Path(__file__).resolve().parents[1] / "sonde" / "web" / "templates"
PAGES = sorted(p for p in TEMPLATES.glob("*.html") if not p.name.startswith("_"))
BASE = (TEMPLATES / "base.html").read_text()


def source(name: str) -> str:
    return (TEMPLATES / name).read_text()


JINJA = re.compile(r"{%.*?%}|{#.*?#}|{{.*?}}", re.S)
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
        "meta", "source", "track", "wbr"}


def unwrapped_tables(text: str) -> list[int]:
    """Line numbers of `<table>` elements with no scrollable ancestor.

    Ancestry, not proximity. A first version looked for `overflow-x-auto`
    within the preceding 500 characters, which a *previous* table's wrapper
    satisfied — so deleting a real wrapper left the test green. It was caught by
    mutating groups.html and finding the suite still passed.
    """
    from html.parser import HTMLParser

    class Walker(HTMLParser):
        def __init__(self) -> None:
            super().__init__(convert_charrefs=True)
            self.stack: list[tuple[str, str]] = []
            self.bad: list[int] = []

        def handle_starttag(self, tag, attrs):
            classes = dict(attrs).get("class") or ""
            if tag == "table" and not any(
                "overflow-x-auto" in c or "overflow-auto" in c
                for _, c in self.stack
            ):
                self.bad.append(self.getpos()[0])
            if tag not in VOID:
                self.stack.append((tag, classes))

        def handle_startendtag(self, tag, attrs):
            pass

        def handle_endtag(self, tag):
            for index in range(len(self.stack) - 1, -1, -1):
                if self.stack[index][0] == tag:
                    del self.stack[index:]
                    break

    walker = Walker()
    # Jinja is stripped rather than rendered: this asks about the markup an
    # author wrote, and a branch that only sometimes emits a wrapper is exactly
    # the case worth failing on.
    walker.feed(JINJA.sub("", text))
    return walker.bad


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_every_table_can_scroll_itself(page):
    """A wide table inside a wrapper scrolls; without one it widens the page.

    Six tables had drifted from this pattern — detail.html x4, influential and
    institutions — while twelve others already followed it.
    """
    bad = unwrapped_tables(page.read_text())
    assert not bad, (
        f"{page.name}: <table> at line(s) {bad} has no overflow-x-auto "
        f"ancestor, so it will widen the page instead of scrolling itself"
    )


def test_the_table_check_actually_detects_a_missing_wrapper():
    """Guards the guard — the first version of it passed on broken markup."""
    assert unwrapped_tables('<div class="overflow-x-auto"><table></table></div>') == []
    assert unwrapped_tables("<div><table></table></div>") == [1]
    # The case that slipped through: a sibling's wrapper must not count.
    assert unwrapped_tables(
        '<div class="overflow-x-auto"><table></table></div><div><table></table></div>'
    ) == [1]


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_no_grid_without_a_mobile_fallback(page):
    """`grid-cols-4` with no 1- or 2-column base is unreadable at 320px."""
    for match in re.finditer(r'class="([^"]*\bgrid-cols-(\d)\b[^"]*)"',
                             page.read_text()):
        classes, columns = match.group(1), int(match.group(2))
        if columns >= 3:
            assert re.search(r"\bgrid-cols-[12]\b", classes), (
                f"{page.name}: grid-cols-{columns} with no mobile base: {classes}"
            )


def test_the_nav_collapses_on_small_screens():
    """The reported bug. Ten links in a non-wrapping row overflow the viewport."""
    nav = BASE[BASE.index("<nav"):BASE.index("</nav>")]
    assert "<details" in nav, "nav no longer collapses to a disclosure"
    assert "<summary" in nav


def test_the_two_nav_variants_are_mutually_exclusive():
    """The regression: one shared block, shown by overriding <details>.

    A closed `<details>` does not PAINT its slotted panel, whatever `display`
    the child is given, so the desktop nav had no visible links at all — they
    had bounding boxes and were never drawn. The wide row and the disclosure
    must be separate elements, each hidden at the other's widths, so neither
    depends on overriding the other's native behaviour.
    """
    nav = BASE[BASE.index("<nav"):BASE.index("</nav>")]
    # By id, not "the first <details> in the nav". The build-status panel is
    # also a <details> in this row, and matching positionally made this guard
    # silently inspect the wrong element the moment it was added.
    start = nav.index('id="nav"')
    start = nav.rindex("<details", 0, start)
    details = nav[start:nav.index("</details>", start)]

    assert "group-open:" not in details.split(">", 1)[1].split("</summary>")[-1], (
        "the disclosure panel is being revealed by a CSS override again"
    )
    # The disclosure hides at some breakpoint, and a separate row appears there.
    hide = re.search(r"<details[^>]*\b(sm|md|lg|xl):hidden", details)
    assert hide, "the disclosure never hides on wide screens"
    breakpoint_ = hide.group(1)
    assert f"{breakpoint_}:flex" in nav, (
        f"the disclosure hides at {breakpoint_}: but no row appears there"
    )


def test_both_nav_variants_carry_every_section():
    """One shared link list, so the two variants cannot drift apart."""
    nav = BASE[BASE.index("<nav"):BASE.index("</nav>")]
    assert nav.count("{%- for href, label in NAV %}") == 2, (
        "nav links are no longer generated from the shared NAV list"
    )


def test_the_nav_row_never_reverts_to_a_rigid_flex_row():
    """`flex` without `flex-wrap` or a breakpoint is what broke it originally."""
    nav = BASE[BASE.index("<nav"):BASE.index("</nav>")]
    for match in re.finditer(r'class="([^"]*)"', nav):
        classes = " ".join(match.group(1).split())
        if not re.search(r"(?<![\w:])flex(?![\w-])", classes):
            continue
        assert ("flex-wrap" in classes or "flex-col" in classes
                or "justify-between" in classes or "items-center" in classes), (
            f"rigid flex row in nav, will overflow on a phone: {classes}"
        )


def test_the_body_never_hides_horizontal_overflow():
    """`overflow-x: hidden` would make every one of these bugs invisible.

    It hides the symptom, keeps the broken layout, and defeats the browser eval
    by clipping the evidence it measures.
    """
    body = BASE[BASE.index("<body"):BASE.index("<body") + 300]
    assert "overflow-x-hidden" not in body
    assert "overflow-hidden" not in body


def test_the_viewport_meta_is_present():
    assert 'name="viewport"' in BASE
    assert "width=device-width" in BASE


def test_sort_headers_are_tappable():
    """text-[10px] gives a ~12px target; phones need something nearer 44."""
    sort = source("_sort.html")
    header = sort[sort.index("macro th("):sort.index("endmacro", sort.index("macro th("))]
    assert "min-h-" in header or re.search(r"\bpy-[2-9]\b", header), (
        "sortable column headers have no touch-sized hit area"
    )


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_no_fixed_pixel_widths_on_containers(page):
    """A fixed px width wider than 320 cannot fit the narrowest phone."""
    for match in re.finditer(r"\bw-\[(\d+)px\]", page.read_text()):
        assert int(match.group(1)) <= 320, f"{page.name}: fixed {match.group(0)}"
