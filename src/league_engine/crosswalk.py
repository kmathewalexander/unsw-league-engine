"""Player identity crosswalk.

Maps Yahoo player ids/names to nflverse ``player_id`` (gsis) so Yahoo draft picks,
rosters and ownership can join to nflverse stats and our league-points table.

Strategy:
  1. Exact join on ``load_ff_playerids`` (has a ``yahoo_id`` column).
  2. Fall back to normalised (name, position) match against ``load_players``.
  3. Manual overrides from ``config/player_overrides.csv`` (yahoo_id,gsis_id) win.
"""

from __future__ import annotations

import re

import polars as pl

from . import store
from .config import load_settings

_SUFFIX = re.compile(r"\b(jr|sr|ii|iii|iv|v)\b")


def _norm_name(s: str) -> str:
    s = (s or "").lower()
    s = _SUFFIX.sub("", s)
    return re.sub(r"[^a-z]", "", s)


def _overrides() -> pl.DataFrame:
    path = load_settings().root / "config" / "player_overrides.csv"
    if path.exists():
        return pl.read_csv(path).with_columns(pl.col("yahoo_id").cast(pl.Utf8))
    return pl.DataFrame(schema={"yahoo_id": pl.Utf8, "gsis_id": pl.Utf8})


def build() -> pl.DataFrame:
    """Return a crosswalk frame: yahoo_id, gsis_id, name, position, team."""
    ids = store.read("raw/nflverse", "ff_playerids")

    ycol = "yahoo_id" if "yahoo_id" in ids.columns else None
    gcol = "gsis_id" if "gsis_id" in ids.columns else ("nfl_id" if "nfl_id" in ids.columns else None)
    base = pl.DataFrame(schema={"yahoo_id": pl.Utf8, "gsis_id": pl.Utf8})
    if ycol and gcol:
        base = (
            ids.select(
                pl.col(ycol).cast(pl.Utf8).alias("yahoo_id"),
                pl.col(gcol).cast(pl.Utf8).alias("gsis_id"),
                pl.col("name").alias("name") if "name" in ids.columns else pl.lit(None).alias("name"),
                pl.col("position").alias("position") if "position" in ids.columns else pl.lit(None).alias("position"),
                pl.col("team").alias("team") if "team" in ids.columns else pl.lit(None).alias("team"),
            )
            .filter(pl.col("yahoo_id").is_not_null() & pl.col("gsis_id").is_not_null())
        )

    ov = _overrides()
    if ov.height:
        base = pl.concat([ov.join(base, on="yahoo_id", how="left").select(base.columns), base]).unique(
            subset=["yahoo_id"], keep="first"
        )

    store.write(base, "derived", "crosswalk")
    return base


def resolve(yahoo_ids: list[str] | None = None, names: list[str] | None = None) -> pl.DataFrame:
    """Look up gsis ids for a batch of Yahoo ids and/or (name) strings."""
    xwalk = store.read("derived", "crosswalk") if store.exists("derived", "crosswalk") else build()
    out = pl.DataFrame()
    if yahoo_ids:
        out = xwalk.filter(pl.col("yahoo_id").is_in([str(y) for y in yahoo_ids]))
    if names:
        players = store.read("raw/nflverse", "players")
        name_col = "display_name" if "display_name" in players.columns else "player_name"
        want = {_norm_name(n) for n in names}
        pl_norm = players.with_columns(
            pl.col(name_col).map_elements(_norm_name, return_dtype=pl.Utf8).alias("_norm")
        ).filter(pl.col("_norm").is_in(list(want)))
        out = pl.concat([out, pl_norm], how="diagonal_relaxed")
    return out
