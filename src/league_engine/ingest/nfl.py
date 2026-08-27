"""nflverse ingest via nflreadpy.

Pulls the tables the analysis layer needs and snapshots them to ``data/raw/nflverse/``:
  * player_stats   -- weekly box-score stat lines (input to the scoring engine)
  * schedules      -- games, byes, Vegas spread/total (game-environment adjustment)
  * snap_counts    -- usage / role trend
  * depth_charts   -- role / starter status
  * injuries       -- weekly injury designations
  * ff_playerids   -- cross-site id crosswalk (Yahoo <-> gsis/pfr/sleeper/...)
  * ff_rankings    -- FantasyPros-derived ECR from nflverse (backup to our own scrape)
  * ff_opportunity -- expected fantasy points from an nflverse usage model
"""

from __future__ import annotations

import contextlib

import polars as pl

from .. import store

# Seasons of history to pull for trend/variance work. 2016+ keeps files manageable.
DEFAULT_LOOKBACK_START = 2016


def _seasons(current_season: int, start: int = DEFAULT_LOOKBACK_START) -> list[int]:
    # nflreadpy has data through the most recently completed season; asking for a
    # not-yet-started season raises, so cap at current.
    import nflreadpy as nfl

    latest = min(current_season, nfl.get_current_season())
    return list(range(start, latest + 1))


def sync(current_season: int, full: bool = False) -> None:
    import nflreadpy as nfl

    seasons = _seasons(current_season)
    layer = "raw/nflverse"

    def snap(name: str, df: pl.DataFrame) -> None:
        store.write(df, layer, name)

    snap("player_stats", nfl.load_player_stats(seasons))
    snap("schedules", nfl.load_schedules(seasons + [current_season] if current_season not in seasons else seasons))
    snap("snap_counts", nfl.load_snap_counts(seasons))
    snap("depth_charts", nfl.load_depth_charts(seasons))
    snap("injuries", nfl.load_injuries(seasons))
    snap("players", nfl.load_players())
    snap("ff_playerids", nfl.load_ff_playerids())
    with contextlib.suppress(Exception):  # offseason weekly rankings may be empty
        snap("ff_rankings", nfl.load_ff_rankings("draft"))
        snap("ff_rankings_week", nfl.load_ff_rankings("week"))
    with contextlib.suppress(Exception):
        snap("ff_opportunity", nfl.load_ff_opportunity(seasons))
