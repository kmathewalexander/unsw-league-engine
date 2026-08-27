"""Projection model -> expected league points with floor / ceiling.

Free data only, so the model is deliberately simple and transparent:

* ``season_projection()`` -- draft / preseason. For every ranked player:
    1. Take the consensus ECR (nflverse ``ff_rankings``, ``redraft-overall`` +
       position boards) as the market's ordinal view.
    2. Convert *position rank* -> expected league points-per-game using a curve
       fit from our own historical league-points table (what the RB12 / WR24 / ...
       actually averaged, in THIS league's scoring, over recent seasons).
    3. Blend with the player's own prior-year league PPG when they have one.
   Output: name, position, ecr, pos_rank, points_per_game, season_points.

* ``weekly_projection(week)`` -- in season. Per player:
    mean  = blend(recent league PPG, season league PPG, FantasyPros weekly ECR tier)
    sd    = recent league-points std (fallback 40%% of mean)
    floor / ceiling = mean +/- z * sd  at the configured percentiles.

Both degrade gracefully; a missing source just drops out of the blend.
"""

from __future__ import annotations

import polars as pl
from scipy.stats import norm

from . import store
from .config import load_settings

# nflverse ff_rankings page_type values we use, by position.
_RANK_PAGES = {
    "QB": "redraft-qb", "RB": "redraft-rb", "WR": "redraft-wr",
    "TE": "redraft-te", "K": "redraft-k", "DST": "redraft-dst",
    "OVERALL": "redraft-overall",
}
_GAMES = 17


def _z(p: int) -> float:
    return float(norm.ppf(p / 100))


def _ecr_board() -> pl.DataFrame:
    """Consensus positional ranks from nflverse ff_rankings. Columns: name, position, ecr, sd, bye."""
    if not store.exists("raw/nflverse", "ff_rankings"):
        return pl.DataFrame(schema={"name": pl.Utf8, "position": pl.Utf8, "ecr": pl.Float64})
    r = store.read("raw/nflverse", "ff_rankings")
    pages = list(_RANK_PAGES.values())
    r = r.filter(pl.col("page_type").is_in(pages))
    out = (
        r.select(
            pl.col("player").alias("name"),
            pl.col("pos").alias("position"),
            pl.col("ecr").cast(pl.Float64),
            pl.col("sd").cast(pl.Float64, strict=False).alias("ecr_sd"),
            pl.col("bye").cast(pl.Int64, strict=False),
        )
        .drop_nulls("ecr")
        .sort("ecr")
        .unique(subset=["name", "position"], keep="first")
    )
    # position rank
    return out.with_columns(pl.col("ecr").rank("ordinal").over("position").alias("pos_rank"))


def _rank_to_ppg_curve(lp: pl.DataFrame) -> dict[str, pl.DataFrame]:
    """For each position, a table pos_finish -> mean league PPG, from recent seasons.

    Uses the last 3 completed seasons: rank players by season league points within
    each (season, position), then average PPG at each finish position.
    """
    reg = lp.filter(pl.col("season_type") == "REG")
    seasons = sorted(reg["season"].unique().to_list())[-3:]
    reg = reg.filter(pl.col("season").is_in(seasons))
    season_tot = (
        reg.group_by(["season", "position", "player_id"])
        .agg(pl.col("points").sum().alias("season_pts"), pl.col("points").len().alias("g"))
        .with_columns(
            pl.col("season_pts").rank("ordinal", descending=True).over(["season", "position"]).alias("finish"),
            (pl.col("season_pts") / _GAMES).alias("ppg"),
        )
    )
    curves = {}
    for pos in season_tot.drop_nulls("position")["position"].unique().to_list():
        curves[pos] = (
            season_tot.filter(pl.col("position") == pos)
            .group_by("finish")
            .agg(pl.col("ppg").mean().alias("ppg"))
            .sort("finish")
        )
    return curves


def season_projection() -> pl.DataFrame:
    settings = load_settings()
    pc = settings.projections
    board = _ecr_board()
    if board.is_empty():
        raise FileNotFoundError("No ECR board (nflverse ff_rankings). Run `playbook sync` first.")

    lp = store.read("derived", "league_points")
    curves = _rank_to_ppg_curve(lp)

    # market PPG = curve lookup at the player's position rank
    def market_ppg(pos: str, rank: int) -> float | None:
        c = curves.get(pos)
        if c is None or c.is_empty():
            return None
        exact = c.filter(pl.col("finish") == rank)
        if exact.height:
            return float(exact["ppg"][0])
        # extrapolate flat beyond the curve
        return float(c.sort("finish")["ppg"][-1])

    board = board.with_columns(
        pl.struct(["position", "pos_rank"])
        .map_elements(lambda s: market_ppg(s["position"], s["pos_rank"]), return_dtype=pl.Float64)
        .alias("market_ppg")
    )

    # prior-year own PPG
    last_season = lp.filter(pl.col("season_type") == "REG")["season"].max()
    prior = (
        lp.filter((pl.col("season") == last_season) & (pl.col("season_type") == "REG"))
        .group_by("player_display_name")
        .agg(pl.col("points").mean().alias("prior_ppg"), pl.col("points").len().alias("prior_g"))
        .rename({"player_display_name": "name"})
    )
    board = board.join(prior, on="name", how="left")

    w_mkt = pc.w_fantasypros + pc.w_usage_model  # market view carries the FP + usage weight preseason
    w_prior = pc.w_prior_year
    ppg = (
        pl.when(pl.col("prior_ppg").is_not_null() & (pl.col("prior_g") >= 6))
        .then((pl.col("market_ppg") * w_mkt + pl.col("prior_ppg") * w_prior) / (w_mkt + w_prior))
        .otherwise(pl.col("market_ppg"))
    )
    out = board.with_columns(ppg.alias("points_per_game")).with_columns(
        (pl.col("points_per_game") * _GAMES).alias("season_points")
    )
    out = out.select(
        "name", "position", "ecr", "pos_rank", "bye", "prior_ppg", "market_ppg",
        "points_per_game", "season_points",
    ).sort("season_points", descending=True, nulls_last=True)
    store.write(out, "derived", "proj_season")
    return out


def weekly_projection(week: int) -> pl.DataFrame:
    settings = load_settings()
    pc = settings.projections
    lp = store.read("derived", "league_points")
    reg = lp.filter(pl.col("season_type") == "REG")
    cur_season = reg["season"].max()
    cur = reg.filter(pl.col("season") == cur_season)

    roll = (
        cur.sort(["player_id", "week"])
        .group_by("player_id")
        .agg(
            pl.col("player_display_name").last().alias("name"),
            pl.col("position").last().alias("position"),
            pl.col("points").tail(6).mean().alias("mean_recent"),
            pl.col("points").tail(6).std().alias("sd_recent"),
            pl.col("points").mean().alias("mean_season"),
            pl.col("points").len().alias("g"),
        )
    )

    # weekly ECR tier as a light prior for players with little/no current-season data
    prior_ppg = pl.col("mean_season")
    if store.exists("derived", "proj_season"):
        ps = store.read("derived", "proj_season").select("name", "points_per_game")
        roll = roll.join(ps, on="name", how="left")
        prior_ppg = pl.coalesce(["mean_season", "points_per_game"])

    w_recent, w_season = pc.w_usage_model + pc.w_fantasypros, pc.w_prior_year
    mean = (
        pl.when(pl.col("g") >= 2)
        .then((pl.col("mean_recent") * w_recent + prior_ppg * w_season) / (w_recent + w_season))
        .otherwise(prior_ppg)
    )
    out = roll.with_columns(mean.alias("mean"))
    out = out.with_columns(pl.col("sd_recent").fill_null(pl.col("mean") * 0.4).alias("sd"))
    z_lo, z_hi = _z(pc.floor_percentile), _z(pc.ceiling_percentile)
    out = out.with_columns(
        (pl.col("mean") + z_lo * pl.col("sd")).clip(lower_bound=0).alias("floor"),
        (pl.col("mean") + z_hi * pl.col("sd")).alias("ceiling"),
        pl.lit(week).alias("week"),
    ).sort("mean", descending=True, nulls_last=True)
    store.write(out, "derived", f"proj_week_{week}")
    return out
