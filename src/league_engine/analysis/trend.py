"""Weekly trend analysis: scoring x form x scheme fit x schedule difficulty.

Per fantasy-relevant player (z-scores are *within position*, so a TE and a WR are comparable):

  scoring   forecast PPG in league points (shrunk, see review.py)
  form      last-2 PPG vs season baseline, finishing luck (actual - xFP, sign flipped:
            over-performance is fragile), snap-share trend
  scheme    (a) how much the team's offence feeds the position: pass rate over the last 3 games
            for QB/WR/TE, rush rate for RB, vs the league; (b) the player's share of that volume
            (targets or carries) now vs last season -> role change
  schedule  next 4 opponents' points allowed to the position (2025 + 2026 to date), z-scored;
            positive = softer

  trend_score = 0.35 scoring + 0.25 form + 0.20 scheme + 0.20 schedule

Saved to ``derived/trend/week_<N>``; ``trend_delta`` / ``rank_delta`` compare with the previous
week's snapshot so risers and fallers are explicit.
"""

from __future__ import annotations

import polars as pl

from .. import store
from ..research import _fix_team, _nkey  # noqa: F401
from . import review as rv

W = {"scoring": 0.35, "form": 0.25, "scheme": 0.20, "schedule": 0.20}
HORIZON = 4
_POS = ["QB", "RB", "WR", "TE"]


def _zpos(col: str, out: str) -> pl.Expr:
    sd = pl.col(col).std().over("position")
    return ((pl.col(col) - pl.col(col).mean().over("position")) / pl.when(sd > 0).then(sd).otherwise(1.0)).alias(out)


def team_tendencies(week: int) -> pl.DataFrame:
    """Team pass/rush rates (last 3 games vs season) and player-volume shares by season."""
    o = store.read("raw/nflverse", "ff_opportunity").with_columns(pl.col("season").cast(pl.Int32))
    cur = int(o["season"].max())
    games = (
        o.filter(pl.col("season") == cur)
        .group_by(["posteam", "week"])
        .agg(pl.col("pass_attempt_team").max().alias("pa"), pl.col("rush_attempt_team").max().alias("ra"))
        .with_columns((pl.col("pa") / (pl.col("pa") + pl.col("ra"))).alias("pass_rate"))
        .sort(["posteam", "week"])
    )
    t = games.group_by("posteam").agg(
        pl.col("pass_rate").mean().alias("pass_rate_season"),
        pl.col("pass_rate").tail(3).mean().alias("pass_rate_l3"),
        pl.col("pa").tail(3).mean().alias("pass_att_l3"),
        pl.col("ra").tail(3).mean().alias("rush_att_l3"),
    ).rename({"posteam": "team"})
    t = t.with_columns(pl.col("team").replace({"LA": "LAR"}))
    return t.with_columns(
        (pl.col("pass_rate_l3") - pl.col("pass_rate_l3").mean()).alias("pass_lean"),
    )


def _shares() -> pl.DataFrame:
    o = store.read("raw/nflverse", "ff_opportunity").with_columns(pl.col("season").cast(pl.Int32))
    cur = int(o["season"].max())
    g = o.group_by(["season", "player_id"]).agg(
        pl.col("rec_attempt").sum().alias("tgt"), pl.col("rec_attempt_team").sum().alias("tgt_t"),
        pl.col("rush_attempt").sum().alias("car"), pl.col("rush_attempt_team").sum().alias("car_t"),
        pl.col("pass_attempt").sum().alias("pa"), pl.col("pass_attempt_team").sum().alias("pa_t"),
    ).with_columns(
        (pl.col("tgt") / pl.col("tgt_t")).alias("tgt_share"), (pl.col("car") / pl.col("car_t")).alias("car_share"),
    )
    n = g.filter(pl.col("season") == cur).select(
        "player_id", pl.col("tgt_share").alias("tgt_now"), pl.col("car_share").alias("car_now"))
    p = g.filter(pl.col("season") == cur - 1).select(
        "player_id", pl.col("tgt_share").alias("tgt_prev"), pl.col("car_share").alias("car_prev"))
    return n.join(p, on="player_id", how="left")


def opponent_softness(week: int) -> pl.DataFrame:
    """Per (team, position): mean z of points allowed by the next HORIZON opponents."""
    lp = store.read("derived", "league_points").filter(pl.col("season_type") == "REG")
    cur = int(lp["season"].max())
    lp = lp.with_columns(_fix_team("opponent_team").alias("opp"))
    allowed = (
        lp.filter(pl.col("position").is_in(_POS))
        .with_columns(pl.when(pl.col("season") == cur).then(2.0).otherwise(1.0).alias("w"))
        .group_by(["opp", "position"])
        .agg(((pl.col("points") * pl.col("w")).sum() / pl.col("w").sum()).alias("pa"))
        .with_columns(((pl.col("pa") - pl.col("pa").mean().over("position")) / pl.col("pa").std().over("position")).alias("soft"))
    )
    sc = store.read("raw/nflverse", "schedules").filter(
        (pl.col("season") == cur) & (pl.col("game_type") == "REG") & pl.col("week").is_between(week, week + HORIZON - 1)
    ).with_columns(_fix_team("home_team").alias("home_team"), _fix_team("away_team").alias("away_team"))
    games = pl.concat([
        sc.select(pl.col("home_team").alias("team"), pl.col("away_team").alias("opp"), "week"),
        sc.select(pl.col("away_team").alias("team"), pl.col("home_team").alias("opp"), "week"),
    ])
    j = games.join(allowed, on="opp", how="inner")
    out = j.group_by(["team", "position"]).agg(
        pl.col("soft").mean().alias("sched_soft"), pl.col("week").n_unique().alias("games_ahead"),
        pl.col("opp").sort_by("week").alias("opps"),
    ).with_columns(pl.col("opps").list.join("/").alias("next_opps"))
    return out.drop("opps")


def availability() -> pl.DataFrame:
    """Per player: share of available games not played (injury / scratch) + usage from nflverse."""
    if not store.exists("derived/league", "player_weekly_advanced"):
        return pl.DataFrame(schema={"player_id": pl.Utf8})
    a = store.read("derived/league", "player_weekly_advanced").filter(pl.col("status") != "bye")
    return a.group_by("player_id").agg(
        (pl.col("status") != "played").mean().alias("miss_rate"),
        pl.col("wopr").mean().alias("wopr_avg"),
        pl.col("air_yards_share").mean().alias("air_share"),
        pl.col("target_share").mean().alias("tgt_share_avg"),
        pl.col("status").sort_by("week").last().alias("last_status"),
    )


def build(week: int) -> pl.DataFrame:
    pt = rv.player_table()
    pt = pt.filter(pl.col("position").is_in(_POS))
    pt = pt.with_columns(pl.col("team").replace({"LA": "LAR"}))
    # namesakes (e.g. two "Antonio Williams"): keep the one with the larger 2026 sample
    pt = pt.sort(["g", "forecast"], descending=True).unique(subset=["name", "position"], keep="first", maintain_order=True)
    t = (
        pt.join(team_tendencies(week), on="team", how="left")
        .join(_shares(), on="player_id", how="left")
        .join(opponent_softness(week), on=["team", "position"], how="left")
        .join(availability(), on="player_id", how="left")
    )
    # ---- scheme: team lean toward the position + role change
    lean = (
        pl.when(pl.col("position") == "RB").then(-pl.col("pass_lean")).otherwise(pl.col("pass_lean")).fill_null(0.0)
    )
    share_now = pl.when(pl.col("position") == "RB").then(pl.col("car_now")).otherwise(pl.col("tgt_now"))
    share_prev = pl.when(pl.col("position") == "RB").then(pl.col("car_prev")).otherwise(pl.col("tgt_prev"))
    t = t.with_columns(
        (lean * 10).alias("team_lean"),                                    # +1 per 10 pp of lean
        ((share_now - share_prev).fill_null(0.0) * 10).alias("role_delta"),  # +1 per 10 pp share gain
    )
    t = t.with_columns((pl.col("team_lean") * 0.5 + pl.col("role_delta") * 0.5).alias("scheme_raw"))
    if "miss_rate" not in t.columns:
        t = t.with_columns(pl.lit(None, dtype=pl.Float64).alias("miss_rate"))
    # ---- form
    t = t.with_columns(
        ((pl.col("last2").fill_null(pl.col("forecast")) - pl.col("prior_ppg").fill_null(pl.col("forecast")))).alias("form_raw"),
        (-pl.col("finish_luck").fill_null(0.0)).alias("luck_adj"),
    ).with_columns(
        (pl.col("form_raw") * 0.5 + pl.col("luck_adj") * 0.3 + pl.col("snap_trend").fill_null(0.0) / 10.0 * 0.2
         - pl.col("miss_rate").fill_null(0.0) * 4.0).alias("form_x")  # each missed game costs form
    )
    t = t.filter(pl.col("forecast") >= 3.0).with_columns(
        _zpos("forecast", "z_scoring"), _zpos("form_x", "z_form"),
        _zpos("scheme_raw", "z_scheme"), pl.col("sched_soft").fill_null(0.0).alias("z_schedule"),
    ).with_columns(
        (W["scoring"] * pl.col("z_scoring") + W["form"] * pl.col("z_form")
         + W["scheme"] * pl.col("z_scheme") + W["schedule"] * pl.col("z_schedule")).alias("trend_score")
    )
    t = t.with_columns(pl.col("trend_score").rank("ordinal", descending=True).over("position").alias("pos_rank_trend"))
    prev = _prev_trend(week)
    if prev is not None:
        t = t.join(prev.select("name", "position", pl.col("trend_score").alias("prev_score"),
                               pl.col("pos_rank_trend").alias("prev_rank")), on=["name", "position"], how="left")
        t = t.with_columns((pl.col("trend_score") - pl.col("prev_score")).alias("trend_delta"),
                           (pl.col("prev_rank") - pl.col("pos_rank_trend")).alias("rank_delta"))
    else:
        t = t.with_columns(pl.lit(None, dtype=pl.Float64).alias("trend_delta"), pl.lit(None, dtype=pl.Int64).alias("rank_delta"))
    t = t.with_columns(pl.lit(week).alias("week"), _drivers().alias("drivers"))
    keep = ["week", "name", "position", "team", "g", "forecast", "ppg", "xfp_pg", "finish_luck", "snap_trend",
            "team_lean", "role_delta", "wopr_avg", "miss_rate", "last_status", "sched_soft", "next_opps", "z_scoring", "z_form", "z_scheme", "z_schedule",
            "trend_score", "pos_rank_trend", "trend_delta", "rank_delta", "drivers", "signal"]
    out = t.select(keep).sort("trend_score", descending=True)
    store.write(out, "derived/trend", f"week_{week}")
    return out


def _prev_trend(week: int) -> pl.DataFrame | None:
    d = store.path_for("derived/trend", "x").parent
    earlier = sorted(int(p.stem.split("_")[1]) for p in d.glob("week_*.parquet") if int(p.stem.split("_")[1]) < week)
    return store.read("derived/trend", f"week_{earlier[-1]}") if earlier else None


def _drivers() -> pl.Expr:
    """Top-2 components by |contribution| as a readable string."""
    comps = {k: W[k] * pl.col(f"z_{k}") for k in W}
    best = pl.concat_list([pl.struct(pl.lit(k).alias("k"), v.alias("v")) for k, v in comps.items()]).list.eval(
        pl.element().sort_by(pl.element().struct.field("v").abs(), descending=True)
    ).list.head(2)
    return best.list.eval(
        pl.element().struct.field("k") + pl.lit(" ")
        + pl.when(pl.element().struct.field("v") >= 0).then(pl.lit("+")).otherwise(pl.lit("-"))
    ).list.join(", ")


# ---------------------------------------------------------------- reporting
def league_view(week: int, trend: pl.DataFrame) -> dict[str, pl.DataFrame]:
    """Trend results sliced for decisions: my roster, waiver risers, other teams' trend."""
    ros = rv.load_rosters()
    me = rv.load_settings().league.team_name
    tr = trend.with_columns(pl.col("name").map_elements(rv._nk, return_dtype=pl.Utf8).alias("_k"))
    mine = tr.join(ros.filter(pl.col("team") == me).select("_k", "slot"), on="_k", how="inner").drop("_k")
    fa = tr.filter(~pl.col("_k").is_in(ros["_k"].to_list())).drop("_k")
    j = tr.join(ros.select("_k", "team", "slot").rename({"team": "fteam"}), on="_k", how="inner")
    starters = j.filter(pl.col("slot") != "IR").sort("trend_score", descending=True).group_by("fteam").head(8)
    teams = starters.group_by("fteam").agg(
        pl.col("trend_score").sum().alias("trend_total"),
        pl.col("z_form").mean().alias("form"), pl.col("z_schedule").mean().alias("sched"),
        pl.col("z_scheme").mean().alias("scheme"),
        (pl.col("trend_delta").sum()).alias("delta"),
    ).sort("trend_total", descending=True)
    return {"mine": mine.sort("trend_score", descending=True), "free_agents": fa.head(30), "teams": teams}
