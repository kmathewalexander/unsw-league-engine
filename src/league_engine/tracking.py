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


def track_all(week: int | None = None) -> dict[str, int]:
    week = week or current_week()
    out = {"week": week}
    out["rosters"] = track_rosters(week).height
    out["scoring_rows"] = track_scoring(week).height
    out["waiver_pool"] = track_waivers(week).height
    s = track_standings(week)
    out["standings"] = 0 if s is None else s.height
    return out
