"""The influence score.

The governing requirement, from SCORING.md: a verified NYT columnist and a
same-size engagement farm have identical reach and must not score alike.
"""

import pytest

from sonde import scoring
from sonde.scoring import WEIGHTS, score_actor


def component(score, key):
    return next(c for c in score.components if c.key == key)


# ------------------------------------------------------------- structure

def test_weights_sum_to_100():
    assert sum(WEIGHTS.values()) == 100


def test_every_weight_has_a_component():
    score = score_actor({"followers_count": 100})
    assert {c.key for c in score.components} == set(WEIGHTS)


def test_components_serialise_for_the_ui():
    score = score_actor({"followers_count": 5000, "follows_count": 100})
    payload = score.as_json()
    assert '"points"' in payload and '"source"' in payload
    # Every component must be able to explain itself.
    for c in score.components:
        assert c.source, f"{c.key} has no stated source"


# ---------------------------------------------------------------- reach

@pytest.mark.parametrize(
    "followers,expected",
    [(10, 0.2), (1_000, 0.6), (100_000, 1.0), (1_000_000, 1.0)],
)
def test_reach_is_log_scaled(followers, expected):
    c = component(score_actor({"followers_count": followers}), "reach")
    assert c.value == pytest.approx(expected, abs=0.01)


def test_reach_is_unavailable_before_hydration():
    c = component(score_actor({}), "reach")
    assert c.available is False
    assert c.points == 0.0


# ---------------------------------------------------------- selectivity

def test_selectivity_gate_is_load_bearing():
    """The case the gate exists for, stated in SCORING.md.

    Ungated, an account with 5 followers that follows 1 out-scores a working
    journalist with 20k followers who follows 8k.
    """
    tiny = score_actor({"followers_count": 5, "follows_count": 1})
    journalist = score_actor({"followers_count": 20_000, "follows_count": 8_000})

    assert component(tiny, "selectivity").value == 0.0
    assert "noise" in component(tiny, "selectivity").detail
    assert journalist.normalised > tiny.normalised


def test_selectivity_separates_curated_from_follow_back():
    curated = score_actor({"followers_count": 5_000, "follows_count": 200})
    farmer = score_actor({"followers_count": 5_000, "follows_count": 5_000})

    assert component(curated, "selectivity").value > component(farmer, "selectivity").value
    assert component(farmer, "selectivity").value == pytest.approx(0.0, abs=0.01)


def test_selectivity_at_the_gate_boundary():
    below = score_actor({"followers_count": 499, "follows_count": 1})
    at = score_actor({"followers_count": 500, "follows_count": 1})

    assert component(below, "selectivity").value == 0.0
    assert component(at, "selectivity").value > 0.0


# --------------------------------------------------------- verification

@pytest.mark.parametrize(
    "verified,trusted,expected",
    [("valid", "valid", 1.0), ("valid", "none", 0.7), ("none", "none", 0.0), (None, None, 0.0)],
)
def test_verification_tiers(verified, trusted, expected):
    c = component(
        score_actor({"verified_status": verified, "trusted_verifier_status": trusted}),
        "verification",
    )
    assert c.value == expected


# ------------------------------------------------------------- affinity

def test_affinity_prefers_the_exact_count_and_says_so():
    c = component(score_actor({"affinity_sampled": 10, "affinity_exact": 125}), "affinity")
    assert c.value == pytest.approx(0.5)
    assert "exact" in c.source


def test_affinity_falls_back_to_the_sample_and_labels_it():
    c = component(score_actor({"affinity_sampled": 20}), "affinity")
    assert c.value == pytest.approx(0.5)
    assert c.source == "sampled index"


def test_affinity_is_unavailable_before_the_index_exists():
    c = component(score_actor({}), "affinity")
    assert c.available is False


# ------------------------------------------------------------- liveness

def test_liveness_uses_real_recency_when_available():
    c = component(
        score_actor(
            {"last_post_at": "2026-07-25T00:00:00Z"}, now_iso="2026-07-26T00:00:00Z"
        ),
        "liveness",
    )
    assert c.source == "last post"
    assert c.value > 0.9


def test_liveness_decays_with_silence():
    fresh = component(
        score_actor({"last_post_at": "2026-07-25T00:00:00Z"}, now_iso="2026-07-26T00:00:00Z"),
        "liveness",
    )
    stale = component(
        score_actor({"last_post_at": "2026-01-01T00:00:00Z"}, now_iso="2026-07-26T00:00:00Z"),
        "liveness",
    )
    assert stale.value < 0.2 < fresh.value


def test_lifetime_average_cannot_max_out_liveness():
    """Found by the live eval: a farm posting 19.5/day took full liveness marks.

    A lifetime average cannot prove recency, so it must not be able to score
    what a real last-post date scores.
    """
    firehose = component(
        score_actor(
            {"posts_count": 20_000, "account_created_at": "2023-10-14T00:00:00Z"},
            now_iso="2026-07-26T00:00:00Z",
        ),
        "liveness",
    )
    real = component(
        score_actor({"last_post_at": "2026-07-26T00:00:00Z"}, now_iso="2026-07-26T00:00:00Z"),
        "liveness",
    )
    assert firehose.value <= scoring.LIVENESS_PROXY_CEILING
    assert real.value > firehose.value


def test_extreme_posting_volume_is_not_extra_alive():
    """20/day and 4/day are equally 'alive'; volume beyond that is just louder."""
    now = "2026-07-26T00:00:00Z"
    created = "2024-07-26T00:00:00Z"
    busy = component(
        score_actor({"posts_count": 2_920, "account_created_at": created}, now_iso=now),
        "liveness",
    )
    firehose = component(
        score_actor({"posts_count": 14_600, "account_created_at": created}, now_iso=now),
        "liveness",
    )
    assert firehose.value == pytest.approx(busy.value, abs=0.01)


def test_lifetime_average_is_labelled_as_such():
    """An account that posted furiously and died in 2024 still scores here.

    That's why the fallback says so out loud rather than passing as recency.
    """
    c = component(
        score_actor(
            {"posts_count": 20_000, "account_created_at": "2023-01-01T00:00:00Z"},
            now_iso="2026-07-26T00:00:00Z",
        ),
        "liveness",
    )
    assert c.source == "lifetime average"
    assert "not recency" in c.detail


# --------------------------------------------------- graceful degradation

def test_missing_data_shrinks_the_denominator_rather_than_penalising():
    bare = score_actor({"followers_count": 1_000, "follows_count": 100})
    assert bare.out_of < 100
    assert bare.out_of == WEIGHTS["reach"] + WEIGHTS["selectivity"] + WEIGHTS["verification"]


def test_normalised_score_is_comparable_across_enrichment_levels():
    """A fully-enriched actor and a bare one must both be judged out of 100."""
    bare = score_actor({"followers_count": 500_000, "follows_count": 300})
    assert 0 <= bare.normalised <= 100


def test_fully_unavailable_actor_scores_zero_not_an_error():
    score = score_actor({})
    assert score.normalised >= 0.0
    assert score.out_of == WEIGHTS["verification"]  # always computable


# ------------------------------------------ global component availability

def test_known_zero_beats_never_measured_only_when_the_signal_exists():
    """Availability is global, not per-actor.

    Once the affinity index exists, an actor with zero hits must score 0 on it
    rather than being excluded from the denominator — otherwise not having been
    indexed beats being indexed and found wanting, and the accounts we know
    least about float to the top.
    """
    base = {"followers_count": 100_000, "follows_count": 500}
    active = {"reach", "selectivity", "verification", "affinity"}

    no_hits = score_actor({**base, "affinity_sampled": 0}, active=active)
    indexed = score_actor({**base, "affinity_sampled": 40}, active=active)

    assert component(no_hits, "affinity").available is True
    assert component(no_hits, "affinity").value == 0.0
    assert indexed.normalised > no_hits.normalised
    assert no_hits.out_of == indexed.out_of, "same denominator for a fair comparison"


def test_an_inactive_component_is_excluded_for_everyone_equally():
    base = {"followers_count": 100_000, "follows_count": 500}
    active = {"reach", "selectivity", "verification"}  # affinity not built yet

    score = score_actor({**base, "affinity_sampled": 12}, active=active)

    assert component(score, "affinity").available is False
    assert WEIGHTS["affinity"] not in (score.out_of,)
    assert score.out_of == WEIGHTS["reach"] + WEIGHTS["selectivity"] + WEIGHTS["verification"]


def test_active_components_derives_from_coverage():
    assert scoring.active_components({}) == {"verification"}
    assert "reach" in scoring.active_components({"reach": 1500})
    assert "affinity" not in scoring.active_components({"reach": 1500, "affinity": 0})


# ------------------------------------------------- the motivating case

def _columnist() -> dict:
    """Jamelle Bouie, as measured: 741,720 followers, verified by bsky.app, NYT."""
    return {
        "followers_count": 741_720,
        "follows_count": 3_000,
        "verified_status": "valid",
        "trusted_verifier_status": "none",
        "institution_score": 0.95,
        "institution_name": "The New York Times",
        "institution_confidence": 0.95,
        "institution_method": "corroborated",
        "affinity_sampled": 32,
        "verified_affinity": 24,
        "wikidata_sitelinks": 3,
        "wikipedia_views_30d": 6_726,
        "last_post_at": "2026-07-25T00:00:00Z",
    }


def _engagement_farm() -> dict:
    """Same reach, nothing else: follows 600k back, no institution, no profile."""
    return {
        "followers_count": 741_720,
        "follows_count": 600_000,
        "verified_status": "none",
        "trusted_verifier_status": "none",
        "affinity_sampled": 0,
        "verified_affinity": 0,
        "last_post_at": "2026-07-25T00:00:00Z",
    }


def test_columnist_and_farm_have_identical_reach():
    now = "2026-07-26T00:00:00Z"
    a = component(score_actor(_columnist(), now_iso=now), "reach")
    b = component(score_actor(_engagement_farm(), now_iso=now), "reach")
    assert a.value == b.value == 1.0


def test_columnist_massively_outscores_the_farm():
    now = "2026-07-26T00:00:00Z"
    columnist = score_actor(_columnist(), verified_source_total=147, now_iso=now)
    farm = score_actor(_engagement_farm(), verified_source_total=147, now_iso=now)

    assert columnist.normalised > 80, f"columnist scored {columnist.normalised}"
    assert farm.normalised < 45, f"farm scored {farm.normalised}"
    assert columnist.normalised - farm.normalised > 35


def test_the_farm_can_score_higher_on_liveness():
    """Activity is not virtue — which is why liveness is weighted 7, not 20."""
    now = "2026-07-26T00:00:00Z"
    farm = _engagement_farm()
    farm["last_post_at"] = "2026-07-26T00:00:00Z"
    columnist = _columnist()
    columnist["last_post_at"] = "2026-07-24T00:00:00Z"

    assert (
        component(score_actor(farm, now_iso=now), "liveness").value
        > component(score_actor(columnist, now_iso=now), "liveness").value
    )
    # …and still loses overall by a wide margin.
    assert (
        score_actor(columnist, verified_source_total=147, now_iso=now).normalised
        > score_actor(farm, verified_source_total=147, now_iso=now).normalised + 35
    )


def test_affinity_scale_is_calibrated_not_constant():
    """Weighted overlap grows with the source count, so a fixed ceiling would
    silently rescale everyone whenever the index size changed."""
    small = score_actor({"affinity_sampled": 10, "affinity_scale": 20})
    large = score_actor({"affinity_sampled": 10, "affinity_scale": 100})
    assert component(small, "affinity").value == pytest.approx(0.5)
    assert component(large, "affinity").value == pytest.approx(0.1)


def test_affinity_falls_back_to_the_constant_without_a_scale():
    c = component(score_actor({"affinity_sampled": 20}), "affinity")
    assert c.value == pytest.approx(20 / scoring.AFFINITY_SAMPLED_FULL)
