"""Waiver-target analysis: availability x projected points added x schedule -> FAAB bid."""

from __future__ import annotations

import polars as pl

from .. import store
from ..config import load_settings


def _rostered_names() -> set[str]:
    """Player names currently rostered league-wide, from the my_roster + rosters snapshots.

    Yahoo per-team rosters are snapshotted as ``rosters`` (name-keyed) by ``sync``;
    absent that, we lean entirely on ownership%% filtering below.
    """
    settings = load_settings()
    season = str(settings.league.current_season)
    if store.json_exists("raw/yahoo", season, "rosters"):
        data = store.read_json("raw/yahoo", season, "rosters")
        names: set[str] = set()
        for team in data.values() if isinstance(data, dict) else []:
            for p in team if isinstance(team, list) else []:
                nm = p.get("name") if isinstance(p, dict) else None
                if isinstance(nm, dict):
                    nm = nm.get("full")
                if nm:
                    names.add(nm)
        return names
    return set()


def candidates(week: int, my_starter_baseline: dict[str, float], own_threshold: float = 50.0) -> pl.DataFrame:
    """Rank waiver adds.

    Args:
        week: upcoming week.
        my_starter_baseline: {position: projected pts of my current worst starter}.
        own_threshold: only consider players rostered in < this %% of leagues.

    Returns ranked frame: name, position, proj_next, proj_next4, pts_added, faab_pct.
    """
    settings = load_settings()
    proj = store.read("derived", f"proj_week_{week}") if store.exists("derived", f"proj_week_{week}") else None
    if proj is None:
        raise FileNotFoundError(f"Run projections for week {week} first.")

    # ownership from nflverse ff_rankings (has 'ecr'/'pos') or Yahoo pct_owned if pulled
    pool = proj
    if store.exists("raw/nflverse", "ff_rankings_week"):
        own = store.read("raw/nflverse", "ff_rankings_week")
        ocol = next(
            (c for c in ("owned", "pct_owned", "ownership", "player_owned_avg") if c in own.columns), None
        )
        ncol = next((c for c in ("player", "player_name", "name") if c in own.columns), None)
        if ocol and ncol:
            pool = pool.join(
                own.select(pl.col(ncol).alias("name"), pl.col(ocol).cast(pl.Float64).alias("owned")),
                on="name",
                how="left",
            ).filter(pl.col("owned").fill_null(0.0) < own_threshold)

    base = pl.col("position").replace_strict(my_starter_baseline, default=0.0)
    ranked = (
        pool.with_columns(
            (pl.col("mean") - base).alias("pts_added"),
        )
        .filter(pl.col("pts_added") > 0)
        .sort("pts_added", descending=True)
    )

    budget = settings.faab.season_budget
    ranked = ranked.with_columns(
        (
            (pl.col("pts_added") / pl.col("pts_added").max()).pow(1.5) * 0.6 * budget
        ).round(0).cast(pl.Int64).alias("faab_bid")
    )
    return ranked.select(
        "name", "position", "mean", "floor", "ceiling", "pts_added", "faab_bid"
    ).head(25)


def trending() -> pl.DataFrame:
    """nflverse/Yahoo trending adds as a secondary signal, if available."""
    if store.exists("raw/nflverse", "ff_rankings_week"):
        return store.read("raw/nflverse", "ff_rankings_week").head(25)
    return pl.DataFrame()
