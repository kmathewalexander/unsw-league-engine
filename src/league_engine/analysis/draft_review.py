"""Draft review: how each manager drafts, and who appears to do their research.

Inputs
  config/draft_history/<season>.txt   `Player|POS` in overall pick order (Yahoo draft results)
  config/draft_history/orders.toml    round-1 order per season + franchise name mapping
  raw/nflverse/ff_rankings_draft_history   archived FantasyPros ECR (market price at draft time)
  derived/league_points                    what the player actually scored in THIS league's scoring

Stored (same store as the rest): ``raw/league/draft/season_<YYYY>`` (picks + market + outcome)
and ``derived/league/draft_manager_profiles``.

"Research" is inferred, not observed. Signals:
  * consensus_gap   how far (in picks) a manager strays from the market price at draft time, net of
                    the league-wide habit for that position (superflex makes everyone reach for QBs)
  * deviation payoff   did the picks where they strayed from the market outperform it (2025 outcome)?
  * hit / bust rate    finished at-or-better than the pick slot / >60 slots worse
  * rookie share, stacking, homer, positional timing  (tendencies to exploit)
  * engagement      in-season waiver/trade moves (config/standings.csv `moves`)
"""

from __future__ import annotations

import tomllib
from datetime import date

import polars as pl

from .. import store
from ..config import load_settings
from .review import _nk

LAYER = "raw/league"
DERIVED = "derived/league"
_CUTOFF = {2026: date(2026, 8, 29), 2025: date(2025, 9, 3)}  # last ECR snapshot on/before draft day
_OFF = ["QB", "RB", "WR", "TE"]


def _cfg() -> dict:
    return tomllib.loads((load_settings().root / "config" / "draft_history" / "orders.toml").read_text())


def _franchise_map(cfg: dict) -> dict[str, str]:
    return {f["name"]: f["franchise"] for f in cfg.get("franchise", [])}


def ingest_drafts() -> pl.DataFrame:
    """Parse the staged draft files into raw/league/draft/season_<YYYY>."""
    root = load_settings().root / "config" / "draft_history"
    cfg = _cfg()
    fmap = _franchise_map(cfg)
    frames = []
    for season_s, order in cfg["orders"].items():
        path = root / f"{season_s}.txt"
        if not path.exists():
            continue
        n = len(order)
        rows = []
        for i, line in enumerate(path.read_text().splitlines()):
            name, pos = line.rsplit("|", 1)
            rnd = i // n + 1
            slot = i % n if rnd % 2 == 1 else n - 1 - (i % n)
            team = order[slot]
            rows.append({"season": int(season_s), "pick": i + 1, "round": rnd, "team": team,
                         "franchise": fmap.get(team, team), "player": name, "pos": pos})
        df = pl.DataFrame(rows)
        store.write(df, LAYER, "draft", f"season_{season_s}")
        frames.append(df)
    return pl.concat(frames) if frames else pl.DataFrame()


def _market(season: int) -> pl.DataFrame:
    """Market rank at draft time: superflex-aware `redraft-op` for QB/RB/WR/TE."""
    h = store.read("raw/nflverse", "ff_rankings_draft_history")
    h = h.filter(pl.col("scrape_date").dt.year() == season)
    cut = _CUTOFF.get(season, date(season, 9, 3))
    h = h.filter(pl.col("scrape_date") <= cut)
    if h.is_empty():
        return pl.DataFrame(schema={"_k": pl.Utf8, "market_rank": pl.Float64})
    latest = h.filter(pl.col("page_type") == "redraft-op")
    if latest.is_empty():
        latest = h.filter(pl.col("page_type") == "redraft-overall")
    day = latest["scrape_date"].max()
    latest = latest.filter(pl.col("scrape_date") == day)
    return (
        latest.with_columns(pl.col("player").map_elements(_nk, return_dtype=pl.Utf8).alias("_k"),
                            pl.col("ecr").cast(pl.Float64).alias("market_rank"))
        .sort("market_rank").unique(subset=["_k"], keep="first").select("_k", "market_rank")
    )


def _outcomes(season: int) -> pl.DataFrame:
    """Season league points (this league's scoring) + prior-year games for a rookie flag."""
    lp = store.read("derived", "league_points").filter(pl.col("season_type") == "REG")
    cur = int(lp["season"].max())
    lp = lp.filter(pl.col("position").is_in(_OFF + ["K"]))  # rank among fantasy-relevant players only
    cy = lp.filter(pl.col("season") == season)
    prior = lp.filter(pl.col("season") == season - 1).group_by("player_display_name").agg(pl.len().alias("g_prior"))
    out = cy.group_by("player_display_name").agg(pl.col("points").sum().alias("pts"), pl.len().alias("g"))
    out = out.join(prior, on="player_display_name", how="left")
    out = out.with_columns(pl.col("player_display_name").map_elements(_nk, return_dtype=pl.Utf8).alias("_k"))
    out = out.with_columns(pl.col("pts").rank("ordinal", descending=True).alias("finish_rank"),
                           (pl.col("pts") / pl.col("g")).alias("ppg"))
    out = out.sort("pts", descending=True).unique(subset=["_k"], keep="first")
    return out.with_columns(pl.lit(season < cur).alias("complete")).select(
        "_k", "pts", "g", "ppg", "finish_rank", "g_prior", "complete")


def build_picks() -> pl.DataFrame:
    picks = ingest_drafts()
    if picks.is_empty():
        raise FileNotFoundError("No config/draft_history/*.txt found.")
    rosters_pos = picks.with_columns(pl.col("player").map_elements(_nk, return_dtype=pl.Utf8).alias("_k"))
    frames = []
    for season in sorted(picks["season"].unique().to_list()):
        p = rosters_pos.filter(pl.col("season") == season)
        try:
            p = p.join(_market(season), on="_k", how="left")
        except FileNotFoundError:
            p = p.with_columns(pl.lit(None, dtype=pl.Float64).alias("market_rank"))
        p = p.join(_outcomes(season), on="_k", how="left")
        frames.append(p)
    out = pl.concat(frames, how="diagonal_relaxed")
    off = pl.col("pos").is_in(_OFF)
    out = out.with_columns(
        pl.when(off).then(pl.col("pick") - pl.col("market_rank")).alias("reach"),  # + = took him LATER than market
    )
    # league habit per (season, position) so a superflex QB run is not "idiosyncratic"
    out = out.with_columns(
        (pl.col("reach") - pl.col("reach").mean().over(["season", "pos"])).alias("idio"),
        # outcome surplus: places gained vs the pick slot / vs market (only completed seasons)
        pl.when(off & pl.col("complete")).then(pl.col("pick") - pl.col("finish_rank")).alias("surplus_pick"),
        pl.when(off & pl.col("complete")).then(pl.col("market_rank") - pl.col("finish_rank")).alias("surplus_mkt"),
        pl.col("g_prior").is_null().alias("rookie_like"),
    )
    for s in out["season"].unique().to_list():
        store.write(out.filter(pl.col("season") == s).drop("_k"), LAYER, "draft", f"season_{s}")
    return out


def _z(c: str) -> pl.Expr:
    sd = pl.col(c).std()
    return (pl.col(c) - pl.col(c).mean()) / pl.when(sd > 0).then(sd).otherwise(1.0)


def manager_profiles(picks: pl.DataFrame) -> pl.DataFrame:
    early = picks.filter(pl.col("round") <= 8)
    g = picks.group_by("franchise").agg(
        pl.col("season").n_unique().alias("drafts"),
        pl.col("idio").mean().alias("avg_idio"),          # + later-than-market habit vs league norm
        pl.col("idio").abs().mean().alias("consensus_gap"),
        (pl.col("pos") == "QB").filter(pl.col("round") <= 3).sum().alias("qb_r1_3"),
        pl.col("round").filter(pl.col("pos") == "QB").min().alias("first_qb_rd"),
        pl.col("round").filter(pl.col("pos") == "TE").min().alias("first_te_rd"),
        pl.col("round").filter(pl.col("pos") == "K").min().alias("k_rd"),
        pl.col("round").filter(pl.col("pos") == "DEF").min().alias("def_rd"),
        pl.col("rookie_like").filter(pl.col("pos").is_in(_OFF) & (pl.col("round") <= 10)).mean().alias("rookie_share"),
        (pl.col("pos") == "RB").filter(pl.col("round") <= 5).sum().alias("rb_r1_5"),
        (pl.col("pos") == "WR").filter(pl.col("round") <= 5).sum().alias("wr_r1_5"),
    )
    o = early.filter(pl.col("surplus_pick").is_not_null()).group_by("franchise").agg(
        pl.col("surplus_mkt").mean().alias("surplus_vs_mkt"),
        (pl.col("surplus_pick") >= 0).mean().alias("hit_rate"),
        (pl.col("surplus_pick") < -60).mean().alias("bust_rate"),
        pl.col("pts").sum().alias("early_pts"),
    )
    dev = early.filter(pl.col("surplus_mkt").is_not_null() & (pl.col("idio").abs() >= 15)).group_by("franchise").agg(
        pl.col("surplus_mkt").mean().alias("payoff_when_deviating"), pl.len().alias("n_dev"))
    dev = dev.with_columns(pl.when(pl.col("n_dev") >= 3).then(pl.col("payoff_when_deviating")).alias("payoff_when_deviating"))
    cons = early.filter(pl.col("surplus_mkt").is_not_null() & (pl.col("idio").abs() < 15)).group_by("franchise").agg(
        pl.col("surplus_mkt").mean().alias("payoff_when_consensus"))
    # everyone under-delivers vs the market on average (injuries, waiver pickups, regression), so
    # score managers *relative to the league average* rather than in absolute places
    if o.height:
        o = o.with_columns((pl.col("surplus_vs_mkt") - pl.col("surplus_vs_mkt").mean()).alias("surplus_vs_mkt"))
    out = g.join(o, on="franchise", how="left").join(dev, on="franchise", how="left").join(cons, on="franchise", how="left")
    out = out.with_columns((pl.col("payoff_when_deviating") - pl.col("payoff_when_consensus")).alias("deviation_edge"))
    # engagement (in-season moves) if we have it
    sp = load_settings().root / "config" / "standings.csv"
    if sp.exists():
        st = pl.read_csv(sp)
        if "moves" in st.columns:
            out = out.join(st.select(pl.col("team").alias("franchise"), "moves"), on="franchise", how="left")
    # composite "research signal": did deviations pay, do picks hit, are busts rare
    comp = [c for c in ("deviation_edge", "hit_rate", "surplus_vs_mkt") if c in out.columns]
    out = out.with_columns(pl.sum_horizontal([_z(c).fill_null(0.0) for c in comp]).alias("research_signal"))
    out = out.with_columns(_style().alias("style")).sort("research_signal", descending=True)
    store.write(out, DERIVED, "draft_manager_profiles")
    return out


def _style() -> pl.Expr:
    return (
        pl.when(pl.col("consensus_gap") < 12).then(pl.lit("follows the market"))
        .when(pl.col("consensus_gap") > 25).then(pl.lit("highly idiosyncratic"))
        .otherwise(pl.lit("moderate deviation"))
    )


def tendencies(picks: pl.DataFrame) -> dict[str, pl.DataFrame]:
    pos_by_round = (
        picks.filter(pl.col("round") <= 8)
        .group_by(["season", "round", "pos"]).len().pivot(on="pos", index=["season", "round"], values="len")
        .fill_null(0).sort("season", "round")
    )
    league_pos = (
        picks.filter(pl.col("reach").is_not_null()).group_by(["season", "pos"])
        .agg(pl.col("reach").mean().alias("avg_reach_vs_mkt"), pl.len().alias("n")).sort("season", "pos")
    )
    value = (
        picks.filter(pl.col("surplus_pick").is_not_null())
        .with_columns(((pl.col("round") - 1) // 4 * 4 + 1).alias("rd_from"))
        .group_by(["pos", "rd_from"]).agg(pl.col("surplus_pick").mean().alias("avg_places_vs_slot"),
                                          (pl.col("surplus_pick") >= 0).mean().alias("hit_rate"), pl.len().alias("n"))
        .sort("pos", "rd_from")
    )
    value = value.with_columns(
        (pl.col("avg_places_vs_slot") - picks.filter(pl.col("surplus_pick").is_not_null())["surplus_pick"].mean()).alias("vs_league_avg")
    )
    stacks = (
        picks.filter((pl.col("round") <= 10))
        .join(_nfl_team_lookup(), on="player", how="left").drop_nulls("nfl")
        .group_by(["season", "franchise", "nfl"]).len().filter(pl.col("len") >= 3).sort("len", descending=True)
    )
    return {"pos_by_round": pos_by_round, "league_position_reach": league_pos, "where_value_was": value, "homer_stacks": stacks}


def _nfl_team_lookup() -> pl.DataFrame:
    lp = store.read("derived", "league_points").filter(pl.col("season_type") == "REG")
    return lp.sort(["season", "week"]).group_by("player_display_name").agg(pl.col("team").last().alias("nfl")).rename(
        {"player_display_name": "player"})
