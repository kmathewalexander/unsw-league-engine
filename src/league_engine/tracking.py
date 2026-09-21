"""League state tracking: rosters, scoring rules, waiver wire, standings — one snapshot per week.

Uses the same store as everything else (``store.write`` -> Parquet under ``data/``)::

    data/raw/league/rosters/week_<N>.parquet       team, slot, name, week, captured_at
    data/raw/league/standings/week_<N>.parquet     team, wins, losses, pf, pa, w1..wN
    data/raw/league/scoring/week_<N>.parquet       kind (stat|slot), key, value, hash
    data/raw/league/waivers/week_<N>.parquet       free-agent pool with forecasts + evidence
    data/raw/league/transactions/week_<N>.parquet  (optional) Yahoo add/drop/trade log
    data/derived/league/roster_moves.parquet       adds/drops between consecutive snapshots
    data/derived/league/scoring_changes.parquet    rule changes between consecutive snapshots
    data/derived/league/waiver_moves.parquet       players newly available / newly rostered

Yahoo's API is gated, so the *inputs* are staging files in ``config/`` (refreshed from the Yahoo
pages by the ``league-analyst`` agent / ``league-track`` skill): ``league_rosters.csv``,
``standings.csv`` and optionally ``transactions.csv``. Scoring comes from ``config/scoring.toml``.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import polars as pl

from . import scoring, store
from .config import load_settings

LAYER = "raw/league"
DERIVED = "derived/league"


def current_week() -> int:
    """Upcoming NFL week = last week with 2026 league points + 1."""
    lp = store.read("derived", "league_points").filter(pl.col("season_type") == "REG")
    cur = lp["season"].max()
    return int(lp.filter(pl.col("season") == cur)["week"].max()) + 1


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _snapshots(kind: str) -> list[int]:
    d = store.path_for(LAYER, kind, "x").parent
    return sorted(int(p.stem.split("_")[1]) for p in d.glob("week_*.parquet"))


def _prev(kind: str, week: int) -> pl.DataFrame | None:
    earlier = [w for w in _snapshots(kind) if w < week]
    return store.read(LAYER, kind, f"week_{earlier[-1]}") if earlier else None


def _append(df: pl.DataFrame, name: str, week: int) -> None:
    """Replace this week's rows in a derived history table and append."""
    if df.is_empty():
        return
    if store.exists(DERIVED, name):
        old = store.read(DERIVED, name)
        if "week" in old.columns:
            old = old.filter(pl.col("week") != week)
            df = pl.concat([old, df], how="diagonal_relaxed")
    store.write(df, DERIVED, name)


# ------------------------------------------------------------------ rosters
def track_rosters(week: int) -> pl.DataFrame:
    path = load_settings().root / "config" / "league_rosters.csv"
    if not path.exists():
        raise FileNotFoundError("config/league_rosters.csv missing — refresh it from Yahoo first.")
    df = pl.read_csv(path).with_columns(pl.lit(week).alias("week"), pl.lit(_now()).alias("captured_at"))
    prev = _prev("rosters", week)
    store.write(df, LAYER, "rosters", f"week_{week}")
    if prev is not None:
        a = prev.select("team", "name").with_columns(pl.lit(True).alias("_p"))
        b = df.select("team", "name").with_columns(pl.lit(True).alias("_c"))
        dropped = a.join(b, on=["team", "name"], how="anti").with_columns(pl.lit("out").alias("move"))
        added = b.join(a, on=["team", "name"], how="anti").with_columns(pl.lit("in").alias("move"))
        moves = pl.concat([dropped.select("team", "name", "move"), added.select("team", "name", "move")])
        _append(moves.with_columns(pl.lit(week).alias("week")), "roster_moves", week)
    return df


# ------------------------------------------------------------------ scoring
def track_scoring(week: int) -> pl.DataFrame:
    rules = scoring.load_rules(load_settings().league.current_season)
    rows = [{"kind": "stat", "key": r.name, "value": float(r.multiplier)} for r in rules.rules]
    rows += [{"kind": "slot", "key": s.position, "value": float(s.count)} for s in rules.roster]
    df = pl.DataFrame(rows).sort("kind", "key")
    digest = hashlib.sha1(df.write_csv().encode()).hexdigest()[:10]
    df = df.with_columns(pl.lit(week).alias("week"), pl.lit(digest).alias("hash"), pl.lit(_now()).alias("captured_at"))
    prev = _prev("scoring", week)
    store.write(df, LAYER, "scoring", f"week_{week}")
    if prev is not None and prev["hash"][0] != digest:
        j = prev.select("kind", "key", pl.col("value").alias("old")).join(
            df.select("kind", "key", pl.col("value").alias("new")), on=["kind", "key"], how="full", coalesce=True
        ).filter(pl.col("old").is_null() | pl.col("new").is_null() | (pl.col("old") != pl.col("new")))
        _append(j.with_columns(pl.lit(week).alias("week")), "scoring_changes", week)
    return df


# ------------------------------------------------------------------ waiver wire
def track_waivers(week: int, per_pos: int = 40) -> pl.DataFrame:
    """Snapshot the free-agent pool: everyone with a forecast who is on no Yahoo roster."""
    from .analysis import review as rv

    pt = rv.player_table()
    rostered = set(rv.load_rosters()["_k"].to_list())
    pool = (
        pt.with_columns(pl.col("name").map_elements(rv._nk, return_dtype=pl.Utf8).alias("_k"))
        .filter(~pl.col("_k").is_in(list(rostered)) & pl.col("position").is_in(["QB", "RB", "WR", "TE", "K"]))
        .sort("forecast", descending=True)
        .group_by("position", maintain_order=True)
        .head(per_pos)
        .select("name", "position", "team", "g", "ppg", "xfp_pg", "finish_luck", "snap_trend",
                "forecast", "floor", "ceiling", "signal")
        .with_columns(pl.lit(week).alias("week"), pl.lit(_now()).alias("captured_at"))
    )
    prev = _prev("waivers", week)
    store.write(pool, LAYER, "waivers", f"week_{week}")
    if prev is not None:
        appeared = pool.join(prev.select("name"), on="name", how="anti").select("name", "position").with_columns(pl.lit("newly_available").alias("move"))
        gone = prev.join(pool.select("name"), on="name", how="anti").select("name", "position").with_columns(pl.lit("no_longer_available").alias("move"))
        _append(pl.concat([appeared, gone]).with_columns(pl.lit(week).alias("week")), "waiver_moves", week)
    tx = load_settings().root / "config" / "transactions.csv"
    if tx.exists():
        store.write(pl.read_csv(tx).with_columns(pl.lit(week).alias("week")), LAYER, "transactions", f"week_{week}")
    return pool


# ------------------------------------------------------------------ standings
def track_standings(week: int) -> pl.DataFrame | None:
    path = load_settings().root / "config" / "standings.csv"
    if not path.exists():
        return None
    df = pl.read_csv(path).with_columns(pl.lit(week).alias("week"), pl.lit(_now()).alias("captured_at"))
    store.write(df, LAYER, "standings", f"week_{week}")
    return df


# ------------------------------------------------------------------ nflverse availability + advanced
def track_advanced() -> pl.DataFrame:
    """Per skill player per played week: status + advanced usage (ported from the dynasty repo).

    status: bye | played (snaps>0) | injury (0 snaps + on report) | healthy_scratch | no_nflverse_data
    metrics: targets, target_share, air_yards_share, wopr (nflverse player_stats), offense snaps
    Stored at derived/league/player_weekly_advanced (owner = fantasy team or FA).
    """
    from .analysis import review as rv
    from .research import _fix_team

    pt = rv.player_table().filter(pl.col("position").is_in(["QB", "RB", "WR", "TE"]))
    pt = pt.with_columns(pl.col("team").replace({"LA": "LAR"}))
    ps = store.read("raw/nflverse", "player_stats").filter(pl.col("season_type") == "REG")
    cur = int(ps["season"].max())
    played_weeks = sorted(ps.filter(pl.col("season") == cur)["week"].unique().to_list())
    sc = store.read("raw/nflverse", "schedules").filter((pl.col("season") == cur) & (pl.col("game_type") == "REG"))
    plays = pl.concat([
        sc.select(pl.col("home_team").alias("team"), "week"), sc.select(pl.col("away_team").alias("team"), "week")
    ]).with_columns(_fix_team("team").alias("team")).with_columns(pl.lit(True).alias("_game"))

    grid = pt.select("player_id", "name", "position", "team").join(pl.DataFrame({"week": played_weeks}), how="cross")
    grid = grid.join(plays, on=["team", "week"], how="left")
    stats = ps.filter(pl.col("season") == cur).select(
        "player_id", "week", "targets", "target_share", "air_yards_share", "wopr")
    grid = grid.join(stats, on=["player_id", "week"], how="left")
    sn = store.read("raw/nflverse", "snap_counts").filter((pl.col("season") == cur) & (pl.col("game_type") == "REG"))
    sn = sn.with_columns(pl.col("player").map_elements(rv._nk, return_dtype=pl.Utf8).alias("_k"),
                         _fix_team("team").alias("team")).select("_k", "team", "week", "offense_snaps", "offense_pct")
    sn = sn.unique(subset=["_k", "team", "week"])
    grid = grid.with_columns(pl.col("name").map_elements(rv._nk, return_dtype=pl.Utf8).alias("_k")).join(
        sn, on=["_k", "team", "week"], how="left")
    inj = store.read("raw/nflverse", "injuries").filter(pl.col("season") == cur)
    inj = inj.filter(pl.col("report_status").is_not_null()).group_by(["gsis_id", "week"]).agg(
        pl.col("report_status").first().alias("inj_status")).with_columns(pl.col("week").cast(pl.Int64))
    grid = grid.with_columns(pl.col("week").cast(pl.Int64)).join(
        inj, left_on=["player_id", "week"], right_on=["gsis_id", "week"], how="left")
    snaps = pl.col("offense_snaps").fill_null(0)
    status = (
        pl.when(pl.col("_game").is_null()).then(pl.lit("bye"))
        .when(snaps > 0).then(pl.lit("played"))
        .when(pl.col("inj_status").is_not_null()).then(pl.lit("injury"))
        .when(pl.col("offense_snaps").is_null() & pl.col("targets").is_null()).then(pl.lit("no_nflverse_data"))
        .otherwise(pl.lit("healthy_scratch"))
    )
    out = grid.with_columns(status.alias("status")).drop("_game", "_k")
    ros = rv.load_rosters().filter(pl.col("slot") != "IR").select("_k", pl.col("team").alias("owner"))
    out = out.with_columns(pl.col("name").map_elements(rv._nk, return_dtype=pl.Utf8).alias("_k")).join(
        ros, on="_k", how="left").drop("_k").with_columns(pl.col("owner").fill_null("FA"))
    store.write(out, DERIVED, "player_weekly_advanced")
    return out


def track_draft() -> int:
    from .analysis import draft_review as dr

    return dr.build_picks().height


def track_all(week: int | None = None) -> dict[str, int]:
    week = week or current_week()
    out = {"week": week}
    out["rosters"] = track_rosters(week).height
    out["scoring_rows"] = track_scoring(week).height
    out["waiver_pool"] = track_waivers(week).height
    s = track_standings(week)
    out["standings"] = 0 if s is None else s.height
    out["advanced_rows"] = track_advanced().height
    try:
        out["draft_picks"] = track_draft()
    except FileNotFoundError:
        out["draft_picks"] = 0
    return out
