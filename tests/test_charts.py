"""M24 — chart geometry.

The arithmetic lives in `sonde/charts.py` precisely so it can be tested without
rendering anything. What is checked here is the set of ways a chart lies:
an inverted scale, a zero silently dropped from a log axis, a bin that swallows
its neighbour, a panel that shares an axis it should not.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sonde import charts
from sonde.db import store

TEMPLATES = Path(__file__).resolve().parents[1] / "sonde" / "web" / "templates"


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from sonde.web.app import create_app

    with TestClient(create_app()) as c:
        yield c


# ------------------------------------------------- scales

def test_a_linear_scale_does_not_invert():
    size = 100.0
    assert charts.linear(0, 0, 10, size) == 0
    assert charts.linear(10, 0, 10, size) == size
    previous = -1.0
    for value in range(11):
        here = charts.linear(value, 0, 10, size)
        assert here >= previous
        previous = here


def test_a_flat_series_still_gets_an_axis():
    """One tick cannot be read as a scale."""
    assert len(charts.nice_ticks(5, 5)) >= 2
    assert len(charts.nice_ticks(0, 0)) >= 2


def test_ticks_cover_the_range():
    ticks = charts.nice_ticks(0, 2629)
    assert ticks[0] <= 0 and ticks[-1] >= 2629


def test_a_log_scale_pins_zero_rather_than_placing_it_wrongly():
    """log10(0) is undefined; a chart must not invent a position for it."""
    assert charts.log_scale(0, 1, 1000, 100) == 0.0
    assert charts.log_scale(None, 1, 1000, 100) == 0.0
    assert charts.log_scale(1000, 1, 1000, 100) == pytest.approx(100)


def test_log_ticks_are_decades_inside_the_data_range():
    assert charts.log_ticks(1, 1000) == [1, 10, 100, 1000]
    # 10 is below the smallest value, so it would sit off the axis. Ticks that
    # no data reaches are excluded rather than clamped onto the edge.
    assert charts.log_ticks(40, 60_000) == [100, 1000, 10_000]


@pytest.mark.parametrize("value,expected", [
    (999, "999"), (1000, "1.0k"), (2629, "2.6k"), (11451, "11k"),
    (1_500_000, "1.5m"),
])
def test_compact_labels(value, expected):
    assert charts.compact(value) == expected


# ------------------------------------------------- columns

def test_columns_leave_a_gap_between_bars():
    """Adjacent fills need a surface gap or they read as one shape."""
    plot = charts.columns([("a", 1), ("b", 2), ("c", 3)])
    first, second = plot["bars"][0], plot["bars"][1]
    assert second["x"] > first["x"] + first["width"]


def test_columns_are_anchored_to_the_baseline():
    plot = charts.columns([("a", 1), ("b", 10)])
    box = plot["box"]
    for bar in plot["bars"]:
        assert bar["y"] + bar["height"] == pytest.approx(
            box.top + box.inner_height, abs=0.05)


def test_the_tallest_column_fits_inside_the_plot():
    plot = charts.columns([("a", 2629), ("b", 400)])
    box = plot["box"]
    for bar in plot["bars"]:
        assert bar["y"] >= box.top - 0.01
        assert bar["height"] <= box.inner_height + 0.01


def test_columns_survive_an_empty_series():
    assert charts.columns([])["bars"] == []


def test_a_single_zero_does_not_divide_by_zero():
    plot = charts.columns([("a", 0)])
    assert plot["bars"][0]["height"] >= 0


# ------------------------------------------------- scatter

def test_a_scatter_reports_what_it_could_not_plot():
    """Silently dropping points misstates the denominator."""
    points = [{"x": 10, "y": 100}, {"x": 0, "y": 50}, {"x": 5, "y": 0}]
    plot = charts.scatter(points)
    assert len(plot["points"]) == 1
    assert plot["dropped"] == 2


def test_scatter_positions_are_inside_the_box():
    points = [{"x": 1, "y": 1}, {"x": 10_000, "y": 500_000}]
    plot = charts.scatter(points)
    box = plot["box"]
    for point in plot["points"]:
        assert box.left - 0.01 <= point["cx"] <= box.width - box.right + 0.01
        assert box.top - 0.01 <= point["cy"] <= box.top + box.inner_height + 0.01


def test_more_followers_plots_higher():
    points = [{"x": 100, "y": 10}, {"x": 100, "y": 100_000}]
    plot = charts.scatter(points)
    assert plot["points"][1]["cy"] < plot["points"][0]["cy"]


def test_scatter_survives_an_empty_set():
    assert charts.scatter([])["points"] == []


# ------------------------------------------------- small multiples

def test_each_panel_scales_to_itself():
    """The whole reason these are separate panels rather than one chart."""
    likes = charts.sparkline([4000, 5000, 6000])
    replies = charts.sparkline([4, 5, 6])
    assert likes["max"] == 6000 and replies["max"] == 6
    # Same shape, wildly different magnitudes — which is why each must print
    # its own peak rather than share an axis.
    assert likes["path"] == replies["path"]


def test_a_panel_with_one_point_is_not_a_line():
    assert charts.sparkline([5])["path"] == ""


def test_panels_survive_all_zeroes():
    spark = charts.sparkline([0, 0, 0])
    assert spark["path"] and spark["max"] == 1


# ------------------------------------------------- palette and markup

def test_categorical_hues_are_fixed_and_not_cycled():
    assert len(charts.SERIES) == len(set(charts.SERIES))
    assert charts.SERIES[0] == "#b4123c", "sonde's accent is slot 1"


def test_every_chart_ships_with_its_numbers():
    """The palette check WARNs on contrast, which obliges labels or a table.

    Each chart therefore carries either a table, a details/table disclosure, or
    direct labels on every mark.
    """
    source = (TEMPLATES / "_charts.html").read_text()
    assert "<title>" in source, "no hover labels on marks"
    for macro in ("column_chart", "scatter_chart", "small_multiples", "bar_list"):
        assert f"macro {macro}" in source


def test_charts_declare_their_coverage():
    """A chart that hides its denominator is the interaction-window bug again."""
    source = (TEMPLATES / "_charts.html").read_text()
    assert "coverage" in source


def test_chart_svgs_can_scroll_on_a_phone():
    """Same rule as tables: a wide mark must not widen the page."""
    source = (TEMPLATES / "_charts.html").read_text()
    for block in source.split("<svg")[1:]:
        preceding = source[:source.index(block)]
        assert "overflow-x-auto" in preceding[-400:] or "grid" in preceding[-400:], \
            "an SVG with no scrollable container"


def test_every_page_with_a_chart_imports_the_macros():
    """An unimported macro renders as nothing at all, silently.

    This is not hypothetical: index.html has no `{% block title %}`, so the
    first attempt inserted the import nowhere and every chart vanished with no
    error anywhere.
    """
    for name in ("index.html", "relationships.html", "interactions.html",
                 "groups.html", "verified.html"):
        source = (TEMPLATES / name).read_text()
        head = source.split("{% block content %}")[0]
        assert '_charts.html" import' in head, f"{name} never imports the macros"
        assert "with context" in head


# ------------------------------------- weekly arrivals on the homepage

async def test_arrivals_are_counted_in_rolling_weeks(db):
    """Rolling seven-day windows, not calendar weeks: a calendar bucket makes
    the current week look like a collapse every Monday."""
    from tests.test_groups import add

    await add("did:plc:now", is_verified=True)
    await add("did:plc:old", is_verified=True)
    conn = await store._db()
    await conn.execute(
        "UPDATE follower_state SET followed_at = date('now','-10 day') "
        "WHERE did = 'did:plc:old'")
    await conn.execute(
        "UPDATE follower_state SET followed_at = date('now','-1 day') "
        "WHERE did = 'did:plc:now'")
    await store.commit()

    weeks = await store.weekly_arrivals()

    assert [w["n"] for w in weeks] == [0, 0, 1, 1]
    assert weeks[-1]["label"] == "this week"


async def test_a_quiet_week_is_still_a_bucket(db):
    """A week with nobody is a fact about the week. Omitting it would make the
    chart misreport the trend by silently closing the gap."""
    weeks = await store.weekly_arrivals()

    assert len(weeks) == 4
    assert all(w["n"] == 0 for w in weeks)


async def test_arrivals_run_oldest_to_newest(db):
    weeks = await store.weekly_arrivals()
    assert [w["weeks_ago"] for w in weeks] == [3, 2, 1, 0]


async def test_hidden_followers_are_not_counted_as_arrivals(db):
    from tests.test_groups import add

    await add("did:plc:h", is_verified=True)
    await store.set_ignored("did:plc:h", True)

    assert sum(w["n"] for w in await store.weekly_arrivals()) == 0


async def test_the_homepage_shows_arrivals_and_links_the_full_history(db, client):
    from tests.test_groups import add

    await add("did:plc:a", is_verified=True)

    page = client.get("/")

    assert "Who found you lately" in page.text
    assert "When your audience joined Bluesky" not in page.text
    assert 'href="/followers"' in page.text


async def test_the_followers_page_carries_the_creation_history(db, client):
    from tests.test_groups import add

    await add("did:plc:a", is_verified=True)
    conn = await store._db()
    for month in ("2023-01", "2023-02", "2023-03"):
        await conn.execute(
            "INSERT INTO actors (did, handle, account_created_at, first_indexed_at) "
            "VALUES (?,?,?,?)",
            (f"did:plc:{month}", f"{month}.test", f"{month}-15T00:00:00Z",
             store.utcnow()))
        await conn.execute(
            "INSERT INTO follower_state (did, is_current, first_seen_at, last_seen_at) "
            "VALUES (?,1,?,?)", (f"did:plc:{month}", store.utcnow(), store.utcnow()))
    await store.commit()

    page = client.get("/followers")

    assert "When your audience joined Bluesky" in page.text
