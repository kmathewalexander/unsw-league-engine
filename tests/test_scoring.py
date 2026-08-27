"""Scoring-engine tests.

`test_parse_and_score` uses a synthetic Yahoo settings payload (no network).
`test_reconcile_vs_nflverse_builtin` checks our PPR scoring reproduces nflverse's
own `fantasy_points_ppr` column for a sample of real player-weeks -- a strong proxy
for "does the stat map add up" until real Yahoo roster fixtures are dropped in
`tests/fixtures/`.
"""

from __future__ import annotations

import polars as pl
import pytest

from league_engine import scoring

# Minimal half-PPR-ish custom league: 6pt pass TD, -2 INT, 0.5 PPR, 0.1/yd, TE premium via bonus omitted.
SETTINGS_FIXTURE = {
    "stat_categories": {
        "stats": [
            {"stat": {"stat_id": 4, "display_name": "Pass Yds"}},
            {"stat": {"stat_id": 5, "display_name": "Pass TD"}},
            {"stat": {"stat_id": 6, "display_name": "Int"}},
            {"stat": {"stat_id": 9, "display_name": "Rush Yds"}},
            {"stat": {"stat_id": 10, "display_name": "Rush TD"}},
            {"stat": {"stat_id": 11, "display_name": "Rec"}},
            {"stat": {"stat_id": 12, "display_name": "Rec Yds"}},
            {"stat": {"stat_id": 13, "display_name": "Rec TD"}},
            {"stat": {"stat_id": 20, "display_name": "Fum Lost"}},
        ]
    },
    "stat_modifiers": {
        "stats": [
            {"stat": {"stat_id": 4, "value": "0.04"}},
            {"stat": {"stat_id": 5, "value": "6"}},
            {"stat": {"stat_id": 6, "value": "-2"}},
            {"stat": {"stat_id": 9, "value": "0.1"}},
            {"stat": {"stat_id": 10, "value": "6"}},
            {"stat": {"stat_id": 11, "value": "0.5"}},
            {"stat": {"stat_id": 12, "value": "0.1"}},
            {"stat": {"stat_id": 13, "value": "6"}},
            {"stat": {"stat_id": 20, "value": "-2"}},
        ]
    },
    "roster_positions": [
        {"roster_position": {"position": "QB", "count": 1}},
        {"roster_position": {"position": "RB", "count": 2}},
        {"roster_position": {"position": "WR", "count": 2}},
        {"roster_position": {"position": "TE", "count": 1}},
        {"roster_position": {"position": "W/R/T", "count": 1}},
        {"roster_position": {"position": "BN", "count": 6}},
    ],
}


def test_parse_and_score():
    rules = scoring.parse_settings(SETTINGS_FIXTURE, 2026)
    assert not rules.unmapped, f"unexpected unmapped stats: {rules.unmapped}"
    assert rules.is_ppr == 0.5
    assert len(rules.starting_slots) == 5

    # One synthetic stat line: 300 pass yds, 3 pass TD, 1 INT, 20 rush yds, 5 rec, 60 rec yds, 1 rec TD.
    row = {
        "passing_yards": 300, "passing_tds": 3, "passing_interceptions": 1,
        "carries": 3, "rushing_yards": 20, "rushing_tds": 0,
        "receptions": 5, "receiving_yards": 60, "receiving_tds": 1,
        "fumbles_lost_total": 0,
        "rushing_first_downs": 0, "passing_first_downs": 0, "receiving_first_downs": 0,
        "passing_2pt_conversions": 0, "rushing_2pt_conversions": 0, "receiving_2pt_conversions": 0,
        "special_teams_tds": 0, "punt_return_yards": 0, "kickoff_return_yards": 0,
        "fumble_recovery_tds": 0,
    }
    df = pl.DataFrame([row])
    scored = scoring.score_weekly(df, rules)
    pts = scored["points"][0]
    expected = 300 * 0.04 + 3 * 6 + 1 * -2 + 20 * 0.1 + 5 * 0.5 + 60 * 0.1 + 1 * 6
    assert pts == pytest.approx(expected, abs=1e-6)


def test_reconcile_vs_nflverse_ppr():
    nfl = pytest.importorskip("nflreadpy")
    stats = nfl.load_player_stats(2024).filter(
        (pl.col("season_type") == "REG") & (pl.col("position").is_in(["QB", "RB", "WR", "TE"]))
    )
    # Standard full-PPR rules expressed the way Yahoo would send them.
    ppr_settings = {
        "stat_categories": {"stats": [
            {"stat": {"stat_id": i, "display_name": n}} for i, n in [
                (4, "Pass Yds"), (5, "Pass TD"), (6, "Int"), (9, "Rush Yds"), (10, "Rush TD"),
                (11, "Rec"), (12, "Rec Yds"), (13, "Rec TD"), (20, "Fum Lost"),
                (18, "2-PT"),
            ]
        ]},
        "stat_modifiers": {"stats": [
            {"stat": {"stat_id": 4, "value": "0.04"}}, {"stat": {"stat_id": 5, "value": "4"}},
            {"stat": {"stat_id": 6, "value": "-2"}}, {"stat": {"stat_id": 9, "value": "0.1"}},
            {"stat": {"stat_id": 10, "value": "6"}}, {"stat": {"stat_id": 11, "value": "1"}},
            {"stat": {"stat_id": 12, "value": "0.1"}}, {"stat": {"stat_id": 13, "value": "6"}},
            {"stat": {"stat_id": 20, "value": "-2"}}, {"stat": {"stat_id": 18, "value": "2"}},
        ]},
        "roster_positions": [{"roster_position": {"position": "QB", "count": 1}}],
    }
    rules = scoring.parse_settings(ppr_settings, 2024)
    scored = scoring.score_weekly(stats, rules)
    # nflverse fantasy_points_ppr uses -2 fumble, 0.04 pass yd, 4 pass td, -2 int, 6 rush/rec td, 1 rec.
    diff = scored.select(
        (pl.col("points") - pl.col("fantasy_points_ppr")).abs().alias("d")
    ).filter(pl.col("d") > 0.5)
    # Remaining divergence is edge cases nflverse attributes differently (special-teams
    # return TDs, fumble-recovery TDs). Tolerate a small fraction, not systemic drift.
    assert diff.height / scored.height < 0.03, f"{diff.height}/{scored.height} rows diverge >0.5"
