"""Research layer: per-player context that sharpens draft decisions.

Produces ``data/derived/research.parquet`` keyed by player ``name`` with:

  usage        -- snap %, target share, carry share (prior season)
  xfp_pg       -- expected fantasy points / game (nflverse ff_opportunity model)
  fp_oe_pg     -- fantasy points over expected / game  (+ = rode TD luck, likely to regress)
  usage_grade  -- A-F, percentile blend of snap% + opportunity within position
  draft_round / draft_pick / rookie / age
  team_total   -- 2026 Vegas implied points/game (from posted lines, else win-total proxy)
  win_total    -- 2026 Vegas season win total (config/team_context.toml)
  sos_season / sos_playoffs -- positional strength of schedule (higher = easier);
                              playoffs = NFL weeks 16-17 (this league's fantasy playoffs)
  injury       -- current status flag (players.status + config/injuries.csv override)

Everything is prior-season (2025) actuals + 2026 schedule/lines. No paid sources.
"""

from __future__ import annotations

import re
import tomllib

import polars as pl

from . import store
from .config import load_settings

_SUFFIX_RE = re.compile(r"\s+(jr|sr|ii|iii|iv|v)\.?$", re.IGNORECASE)


def _nkey(expr: pl.Expr) -> pl.Expr:
    """Normalised match key: lowercase, drop suffix + punctuation/spaces."""
    return (
        expr.str.to_lowercase()
        .str.replace_all(r"\s+(jr|sr|ii|iii|iv|v)\.?$", "")
        .str.replace_all(r"[^a-z]", "")
    )

_SKILL = ["QB", "RB", "WR", "TE"]

# team-code inconsistencies across nflverse tables -> our canonical (config) codes
_TEAM_FIX = {"LA": "LAR", "STL": "LAR", "SD": "LAC", "OAK": "LV", "WSH": "WAS", "ARZ": "ARI"}

# players.status codes worth surfacing at the draft (skip CUT / DEV / RET nobodies)
_STATUS_FLAG = {
    "RES": "IR / Reserve", "PUP": "PUP", "SUS": "Suspended",
    "RSN": "Reserve (non-football)", "NWT": "Not with team", "NFI": "Non-football injury",
    "RSR": "Reserve (retired)", "EXE": "Exempt",
}


def _fix_team(col: str = "team") -> pl.Expr:
    return pl.col(col).replace(_TEAM_FIX)


# --------------------------------------------------------------------------
def _players_meta() -> pl.DataFrame:
    import nflreadpy as nfl

    p = nfl.load_players()
    cs = load_settings().league.current_season
    bd = pl.col("birth_date").cast(pl.Utf8).str.to_date("%Y-%m-%d", strict=False)
    return (
        p.filter(pl.col("position").is_in(_SKILL))
        .select(
            pl.col("display_name").alias("name"),
            pl.col("position"),
            _fix_team("latest_team").alias("team"),
            pl.col("status"),
            pl.col("draft_round").cast(pl.Int64, strict=False),
            pl.col("draft_pick").cast(pl.Int64, strict=False),
            pl.col("rookie_season").cast(pl.Int64, strict=False),
            (cs - bd.dt.year() - (bd.dt.month() > 8).cast(pl.Int64)).alias("age"),
            # prefer the currently-active entry when a name repeats
            (pl.col("status") == "ACT").alias("_active"),
        )
        .with_columns((pl.col("rookie_season") == cs).alias("rookie"))
        .sort(["name", "position", "_active"], descending=[False, False, True])
        .unique(subset=["name", "position"], keep="first")
        .drop("_active")
    )


def _usage(prior_season: int) -> pl.DataFrame:
    import nflreadpy as nfl

    ps = nfl.load_player_stats([prior_season]).filter(
        (pl.col("season_type") == "REG") & pl.col("position").is_in(_SKILL)
    )
    usage = ps.group_by("player_display_name").agg(
        pl.col("position").last().alias("position"),
        pl.col("target_share").mean().alias("tgt_share"),
        (pl.col("carries").sum()).alias("_carries"),
        pl.len().alias("g"),
        pl.col("fantasy_points_ppr").sum().alias("_fp_ppr"),
    )

    snaps = (
        nfl.load_snap_counts([prior_season])
        .filter(pl.col("game_type") == "REG")
        .group_by("player")
        .agg(pl.col("offense_pct").mean().alias("snap_pct"))
    )

    try:
        opp = nfl.load_ff_opportunity([prior_season])
        opp_agg = opp.group_by("full_name").agg(
            (pl.col("total_fantasy_points_exp").sum()).alias("_xfp"),
            (pl.col("total_fantasy_points").sum()).alias("_fp"),
            (pl.col("rush_attempt").sum()).alias("_rush"),
            (pl.col("rush_attempt_team").sum()).alias("_rush_team"),
            pl.len().alias("_gopp"),
        ).with_columns(
            (pl.col("_xfp") / pl.col("_gopp")).alias("xfp_pg"),
            ((pl.col("_fp") - pl.col("_xfp")) / pl.col("_gopp")).alias("fp_oe_pg"),
            (pl.col("_rush") / pl.col("_rush_team")).alias("carry_share"),
        ).rename({"full_name": "player_display_name"})
    except Exception:  # noqa: BLE001
        opp_agg = pl.DataFrame(
            schema={"player_display_name": pl.Utf8, "xfp_pg": pl.Float64,
                    "fp_oe_pg": pl.Float64, "carry_share": pl.Float64}
        )

    out = (
        usage.join(snaps, left_on="player_display_name", right_on="player", how="left")
        .join(opp_agg.select("player_display_name", "xfp_pg", "fp_oe_pg", "carry_share"),
              on="player_display_name", how="left")
        .rename({"player_display_name": "name"})
    )

    # usage grade: percentile of (snap% + opportunity) within position -> A-F
    score = (
        pl.col("snap_pct").fill_null(0).rank("average").over("position")
        + pl.coalesce(["xfp_pg", pl.lit(0.0)]).rank("average").over("position")
    )
    pct = (score.rank("average").over("position") / pl.len().over("position"))
    grade = (
        pl.when(pct >= 0.85).then(pl.lit("A"))
        .when(pct >= 0.65).then(pl.lit("B"))
        .when(pct >= 0.40).then(pl.lit("C"))
        .when(pct >= 0.20).then(pl.lit("D"))
        .otherwise(pl.lit("F"))
    )
    return out.with_columns(grade.alias("usage_grade")).select(
        "name", "snap_pct", "tgt_share", "carry_share", "xfp_pg", "fp_oe_pg", "usage_grade", "g"
    )


def _team_totals(current_season: int) -> pl.DataFrame:
    """2026 implied points/game per team.

    Primary = win-total-derived (stable, all 32 teams): ~22.5 + (win_total - 8.5) * 1.1.
    ``team_total_early`` = mean implied total from the ~weeks-1-4 lines that are posted
    (a directional early-season read, noisy for teams with few games priced).
    """
    wt = _win_totals()
    est = wt.with_columns(
        (22.5 + (pl.col("win_total") - 8.5) * 1.1).round(1).alias("team_total")
    ).select("team", "team_total")

    sched = store.read("raw/nflverse", "schedules").filter(
        (pl.col("season") == current_season) & (pl.col("game_type") == "REG")
    ).with_columns(_fix_team("home_team").alias("home_team"), _fix_team("away_team").alias("away_team"))
    rows = []
    for r in sched.iter_rows(named=True):
        tl, sl = r.get("total_line"), r.get("spread_line")
        if tl is None or sl is None:
            continue
        rows.append({"team": r["home_team"], "implied": tl / 2 - sl / 2})
        rows.append({"team": r["away_team"], "implied": tl / 2 + sl / 2})
    if rows:
        early = (
            pl.DataFrame(rows).group_by("team")
            .agg(pl.col("implied").mean().round(1).alias("team_total_early"), pl.len().alias("_n"))
            .filter(pl.col("_n") >= 4).drop("_n")
        )
        est = est.join(early, on="team", how="left")
    return est


def _win_totals() -> pl.DataFrame:
    path = load_settings().root / "config" / "team_context.toml"
    if not path.exists():
        return pl.DataFrame(schema={"team": pl.Utf8, "win_total": pl.Float64})
    with path.open("rb") as fh:
        data = tomllib.load(fh)
    wt = data.get("win_total", {})
    return pl.DataFrame({"team": list(wt), "win_total": [float(v) for v in wt.values()]})


def _positional_sos(current_season: int) -> pl.DataFrame:
    """For each NFL team: fantasy points its 2026 opponents allowed to each position
    in 2025 (this league's scoring). Higher = softer schedule.
    Returns team x position with sos_season (wk1-17) and sos_playoffs (wk16-17) z-scores.
    """
    lp = store.read("derived", "league_points").filter(
        (pl.col("season") == current_season - 1) & (pl.col("season_type") == "REG")
    ).with_columns(_fix_team("opponent_team").alias("opponent_team"))
    # points allowed by each defense to each position, per game
    allowed = (
        lp.group_by(["opponent_team", "position"])
        .agg((pl.col("points").sum() / pl.col("points").count()).alias("pa_pg"))
        .rename({"opponent_team": "def_team"})
    )

    sched = store.read("raw/nflverse", "schedules").filter(
        (pl.col("season") == current_season) & (pl.col("game_type") == "REG")
    ).with_columns(_fix_team("home_team").alias("home_team"), _fix_team("away_team").alias("away_team"))
    games = pl.concat([
        sched.select(pl.col("home_team").alias("team"), pl.col("away_team").alias("opp"), "week"),
        sched.select(pl.col("away_team").alias("team"), pl.col("home_team").alias("opp"), "week"),
    ])

    def agg(weeks: range | None, label: str) -> pl.DataFrame:
        g = games if weeks is None else games.filter(pl.col("week").is_in(list(weeks)))
        j = g.join(allowed, left_on="opp", right_on="def_team", how="inner")
        m = j.group_by(["team", "position"]).agg(pl.col("pa_pg").mean().alias(label))
        z = ((pl.col(label) - pl.col(label).mean().over("position"))
             / pl.col(label).std().over("position"))
        return m.with_columns(z.round(2).alias(label))

    season = agg(None, "sos_season")
    playoffs = agg(range(16, 18), "sos_playoffs")
    return season.join(playoffs, on=["team", "position"], how="outer", coalesce=True)


def _injury_overrides() -> pl.DataFrame:
    path = load_settings().root / "config" / "injuries.csv"
    if path.exists():
        return pl.read_csv(path)  # columns: name,injury
    return pl.DataFrame(schema={"name": pl.Utf8, "injury": pl.Utf8})


# --------------------------------------------------------------------------
def build() -> pl.DataFrame:
    settings = load_settings()
    cs = settings.league.current_season

    # spine = players on the value board (name + position), so no name collisions
    spine = store.read("derived", "value_board").select("name", "position").with_columns(
        _nkey(pl.col("name")).alias("_k")
    )

    meta = _players_meta().drop("position").with_columns(_nkey(pl.col("name")).alias("_k")).drop("name")
    usage = _usage(cs - 1).with_columns(_nkey(pl.col("name")).alias("_k")).drop("name")
    tt = _team_totals(cs)
    wt = _win_totals()
    sos = _positional_sos(cs)
    inj = _injury_overrides().with_columns(_nkey(pl.col("name")).alias("_k")).drop("name")

    df = (
        spine.join(meta, on="_k", how="left")
        .join(usage, on="_k", how="left")
        .join(tt, on="team", how="left")
        .join(wt, on="team", how="left")
        .join(sos, on=["team", "position"], how="left")
        .join(inj, on="_k", how="left")
        .drop("_k")
    )
    # status flag: config override wins, else a meaningful players.status code
    df = df.with_columns(
        pl.coalesce([
            pl.col("injury"),
            pl.col("status").replace_strict(_STATUS_FLAG, default=None),
        ]).alias("flag")
    )
    keep = [
        "name", "position", "team", "flag", "age", "rookie", "draft_round", "draft_pick",
        "snap_pct", "tgt_share", "carry_share", "xfp_pg", "fp_oe_pg", "usage_grade",
        "team_total", "team_total_early", "win_total", "sos_season", "sos_playoffs",
    ]
    out = df.select([c for c in keep if c in df.columns]).unique(subset=["name", "position"], keep="first")
    store.write(out, "derived", "research")
    return out


def enriched() -> pl.DataFrame:
    """value_board joined with the research table. Falls back to the bare board."""
    board = store.read("derived", "value_board")
    if not store.exists("derived", "research"):
        return board
    res = store.read("derived", "research")
    dup = [c for c in res.columns if c in board.columns and c not in ("name", "position")]
    return board.join(res.drop(dup), on=["name", "position"], how="left")


def notes_for(row: dict) -> str:
    """One-line research callout for a player row from ``enriched()``."""
    bits: list[str] = []
    if row.get("flag"):
        bits.append(f"⚠ {row['flag']}")
    tt = row.get("team_total")
    if tt is not None:
        rank = "high" if tt >= 24.5 else ("low" if tt <= 21 else "mid")
        bits.append(f"team {tt:.1f} ({rank})")
    ug = row.get("usage_grade")
    if ug:
        bits.append(f"usage {ug}")
    oe = row.get("fp_oe_pg")
    if oe is not None and abs(oe) >= 2.5:
        bits.append("TD-lucky '25, regression risk" if oe > 0 else "unlucky '25, bounce-back")
    sp = row.get("sos_playoffs")
    if sp is not None and abs(sp) >= 0.8:
        bits.append(f"{'soft' if sp > 0 else 'tough'} playoff SoS")
    if row.get("rookie"):
        bits.append("rookie")
    age = row.get("age")
    if age is not None and row.get("position") == "RB" and age >= 28:
        bits.append(f"age {age}")
    return " · ".join(bits)
