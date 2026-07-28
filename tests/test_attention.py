"""Attention scarcity (M17).

The component exists because the followers/follows *ratio* — which the influence
score already has as `selectivity` — cannot express the hypothesis. The first
test here is the one that justifies the whole module; if it ever fails, the
component has collapsed back into the ratio and should be deleted.
"""

from __future__ import annotations

import pytest

from sonde import attention


def test_the_ratio_maxes_out_on_accounts_this_scores_zero():
    """Why this is not `selectivity` rebuilt.

    A: 1,000 followers / 10 follows, and B: 500,000 / 5,000. Both have a ratio
    of exactly 100, so selectivity — which *is* that ratio — gives both its
    maximum. Neither is the thing being asked about: A is a tiny account whose
    attention says nothing about me, and B follows more accounts than anyone
    reads. The ratio's top score is this component's zero.
    """
    from sonde.scoring import _selectivity

    assert _selectivity(1_000, 10).value == 1.0
    assert _selectivity(500_000, 5_000).value == 1.0

    assert attention.raw(1_000, 10) == 0.0
    assert attention.raw(500_000, 5_000) == 0.0


def test_equal_ratios_are_distinguishable():
    """Holding the two facts apart means equal ratios need not score equally."""
    a = attention.raw(5_000, 50)        # ratio 100
    b = attention.raw(300_000, 3_000)   # ratio 100
    assert a != b, "attention has degenerated into the followers/follows ratio"
    assert a > 0 and b > 0


def test_both_conditions_must_hold():
    """Multiplicative, as the request was phrased: large reach AND few follows."""
    assert attention.raw(150_000, 1_000) > 0        # both
    assert attention.raw(150_000, 80_000) == 0.0    # reach, but follows everyone
    assert attention.raw(300, 20) == 0.0            # selective, but no reach


def test_gated_at_a_thousand_followers():
    """The bug the gate was added for.

    An ungated standing term ranked a 434-follower account following 45 people
    above Cory Doctorow. A small account following few people is an ordinary
    small account, not scarce attention.
    """
    small = attention.raw(434, 45)
    doctorow = attention.raw(23_492, 641)
    assert small == 0.0
    assert doctorow > 0.0
    assert doctorow > small


def test_ordering_matches_the_measured_list():
    """Real values from the live follower list, in the order they should rank."""
    ranked = [
        ("alt18f", 59_637, 126),
        ("mattbors", 56_030, 510),
        ("doublepulsar", 15_088, 202),
        ("whittaker", 150_735, 1_037),
        ("doctorow", 23_492, 641),
    ]
    scored = sorted(
        ((attention.raw(f, w), name) for name, f, w in ranked), reverse=True
    )
    assert [name for _, name in scored][0] == "alt18f"
    assert all(value > 0 for value, _ in scored)


@pytest.mark.parametrize("followers,follows", [
    (None, 100), (100_000, None), (None, None), (0, 0), (100_000, -1),
])
def test_missing_counts_are_absent_not_zero_scoring(followers, follows):
    """Unhydrated followers contribute nothing rather than crashing."""
    assert attention.raw(followers, follows) == 0.0
    assert attention.describe(followers, follows) is None


def test_scarcity_is_monotonic_and_bounded():
    previous = 1.1
    for follows in (10, 50, 100, 500, 1_000, 2_500, 5_000, 20_000):
        value = attention.scarcity(follows)
        assert 0.0 <= value <= 1.0
        assert value <= previous
        previous = value
    assert attention.scarcity(10) == attention.scarcity(50)   # floor
    assert attention.scarcity(20_000) == 0.0                  # ceiling


def test_standing_is_monotonic_and_bounded():
    previous = -0.1
    for followers in (1, 999, 1_000, 10_000, 100_000, 1_000_000):
        value = attention.standing(followers)
        assert 0.0 <= value <= 1.0
        assert value >= previous
        previous = value
    assert attention.standing(1_000) == 0.0
    assert attention.standing(100_000) == 1.0


def test_calibrate_ignores_zeros():
    """Most followers score zero by design; they must not set the scale."""
    values = [0.0] * 500 + [0.01 * i for i in range(1, 41)]
    assert attention.calibrate(values) == pytest.approx(0.40)


def test_calibration_leaves_no_ties_at_the_cap():
    """The bug a p99 scale caused: the whole top of the ranking collapsed.

    Real values from the live list. Under p99 the top four all came out at
    exactly 30.0 and their order was lost.
    """
    real = [(59_637, 126), (15_088, 202), (25_134, 417), (56_030, 510),
            (150_735, 1_037), (19_924, 524), (23_492, 641)]
    raws = [attention.raw(f, w) for f, w in real] + [0.01 * i for i in range(1, 30)]
    scale = attention.calibrate(raws)
    scored = [attention.points(f, w, scale) for f, w in real]

    at_cap = [s for s in scored if s >= attention.MAX_POINTS]
    assert len(at_cap) == 1, f"{len(at_cap)} tied at the cap: {scored}"
    assert len(set(scored)) == len(scored), "ordering lost to ties"


def test_calibrate_falls_back_on_thin_data():
    assert attention.calibrate([0.3, 0.5]) == attention.DEFAULT_SCALE
    assert attention.calibrate([]) == attention.DEFAULT_SCALE


def test_points_are_capped():
    """No amount of scarcity may outrank a real conversation."""
    huge = attention.points(10_000_000, 50, scale=0.001)
    assert huge == attention.MAX_POINTS
    assert attention.MAX_POINTS < 50


def test_describe_states_the_checkable_fact():
    note = attention.describe(150_735, 1_037)
    assert "1,037" in note and "150,735" in note
    assert "%" in note
