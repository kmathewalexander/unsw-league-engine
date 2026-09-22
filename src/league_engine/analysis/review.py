"""Evidence-based player, team and league review in THIS league's scoring.

Pipeline (each step is a pure function over the derived/raw tables):

1. ``player_table``   per player: 2026 league PPG, opportunity-based expected PPG (xFP),
                      finishing luck (actual - expected), 2025 baseline + volatility, usage
                      trend (snap %), and a *shrunk* rest-of-season forecast.
2. ``team_table``     each Yahoo roster -> optimal lineup -> expected weekly score + sd,
                      slot-by-slot strength vs the league average.
3. ``power_table``    forecast strength, all-play record (luck-free results) vs real record.
4. ``odds_table``     Monte Carlo playoff odds (uses config/schedule.csv if present, else an
                      approximated round-robin).
5. ``waiver_table``   free agents ranked by marginal lineup gain for a given team.

Forecast (why it is not just "last week's score")::

    evidence  = 0.6 * xFP_pg + 0.4 * actual_pg            (xFP is stickier than TDs)
    prior     = blend(preseason market PPG, 2025 PPG)
    forecast  = (n * evidence + k * prior) / (n + k)      n = 2026 games, k = pseudo-games

so a single 34-point game barely moves an established RB, while a role change that shows up
in usage (xFP) moves the number even before the touchdowns arrive.
"""

from __future__ import annotations

import numpy as np
import polars as pl
from scipy.stats import norm

from .. import scoring, store
from ..config import load_settings
from ..research import _nkey
from . import lineup as lu
from .standings import simulate

# pseudo-games of prior belief; bigger = slower to trust the new season's sample
_K = {"QB": 4.0, "RB": 5.0, "WR": 5.0, "TE": 6.0, "K": 8.0}
_DEF_DEFAULT = 7.0  # no DST scoring model yet; flat placeholder so the slot is not empty
_K_DEFAULT = 8.0
_POS = ["QB", "RB", "WR", "TE"]


# ---------------------------------------------------------------- player layer
def _components(o: pl.DataFrame) -> pl.DataFrame:
    """League-scoring expected vs actual points from nflverse opportunity components."""
    f = lambda c: pl.col(c).fill_null(0.0)  # noqa: E731
    exp = (
        0.04 * f("pass_yards_gained_exp")
        + 0.10 * (f("rush_yards_gained_exp") + f("rec_yards_gained_exp"))
        + 4.0 * f("pass_touchdown_exp")
        + 6.0 * (f("rush_touchdown_exp") + f("rec_touchdown_exp"))
        - 2.0 * f("pass_interception_exp")
    )
    act = (
        0.04 * f("pass_yards_gained")
        + 0.10 * (f("rush_yards_gained") + f("rec_yards_gained"))
        + 4.0 * f("pass_touchdown")
        + 6.0 * (f("rush_touchdown") + f("rec_touchdown"))
        - 2.0 * f("pass_interception")
    )
    return o.with_columns(exp.alias("xfp"), act.alias("fp_comp"))


def player_table(week: int | None = None) -> pl.DataFrame:
    """One row per fantasy-relevant player with usage, luck, trend and a shrunk forecast."""
    lp = store.read("derived", "league_points").filter(pl.col("season_type") == "REG")
    cur = int(lp["season"].max())
    prev = cur - 1
    pos_of = (
        lp.filter(pl.col("position").is_in(_POS + ["K"]))
        .sort(["season", "week"])
        .group_by("player_id")
        .agg(
            pl.col("player_display_name").last().alias("name"),
            pl.col("position").last().alias("position"),
            pl.col("team").last().alias("team"),
        )
    )
    if week is not None:
        lp = lp.filter(~((pl.col("season") == cur) & (pl.col("week") >= week)))

    now = lp.filter(pl.col("season") == cur).group_by("player_id").agg(
        pl.col("points").len().alias("g"),
        pl.col("points").mean().alias("ppg"),
        pl.col("points").sort_by(pl.col("week")).tail(2).mean().alias("last2"),
        pl.col("points").sort_by(pl.col("week")).head(1).first().alias("first"),
    )
    old = lp.filter(pl.col("season") == prev).group_by("player_id").agg(
        pl.col("points").len().alias("g25"),
        pl.col("points").mean().alias("ppg25"),
        pl.col("points").std().alias("sd25"),
        (pl.col("points") >= 15).mean().alias("boom25"),
        (pl.col("points") <= 6).mean().alias("bust25"),
    )

    o = _components(store.read("raw/nflverse", "ff_opportunity").with_columns(pl.col("season").cast(pl.Int32)))
    if week is not None:
        o = o.filter(~((pl.col("season") == cur) & (pl.col("week") >= week)))
    ox = (
        o.filter(pl.col("season") == cur)
        .group_by("player_id")
        .agg(pl.col("xfp").mean().alias("xfp_pg"), pl.col("fp_comp").mean().alias("fp_comp_pg"))
        .with_columns((pl.col("fp_comp_pg") - pl.col("xfp_pg")).alias("finish_luck"))
        .select("player_id", "xfp_pg", "finish_luck")
    )
    ox25 = (
        o.filter(pl.col("season") == prev)
        .group_by("player_id")
        .agg(pl.col("xfp").mean().alias("xfp25"))
    )

    sn = store.read("raw/nflverse", "snap_counts").filter(pl.col("game_type") == "REG")
    snap = (
        sn.filter(pl.col("season") == cur)
        .with_columns(pl.col("player").map_elements(lambda s: _nk(s), return_dtype=pl.Utf8).alias("_k"))
        .group_by("_k")
        .agg(pl.col("offense_pct").sort_by(pl.col("week")).last().alias("snap_last"),
             pl.col("offense_pct").mean().alias("snap_avg"))
    )
    snap = snap.with_columns((pl.col("snap_last") - pl.col("snap_avg")).alias("snap_trend"))

    prior_mkt = (
        store.read("derived", "proj_season").select("name", pl.col("points_per_game").alias("mkt_ppg"))
        if store.exists("derived", "proj_season")
        else pl.DataFrame(schema={"name": pl.Utf8, "mkt_ppg": pl.Float64})
    )

    t = (
        pos_of.join(now, on="player_id", how="left")
        .join(old, on="player_id", how="left")
        .join(ox, on="player_id", how="left")
        .join(ox25, on="player_id", how="left")
        .join(prior_mkt, on="name", how="left")
        .with_columns(pl.col("name").map_elements(_nk, return_dtype=pl.Utf8).alias("_k"))
        .join(snap, on="_k", how="left")
        .with_columns(pl.col("g").fill_null(0))
    )

    prior = (
        pl.when(pl.col("g25").fill_null(0) >= 6)
        .then(0.5 * pl.col("ppg25") + 0.5 * pl.coalesce(["mkt_ppg", "ppg25"]))
        .otherwise(pl.coalesce(["mkt_ppg", "ppg25"]))
    )
    evidence = pl.when(pl.col("xfp_pg").is_not_null()).then(
        0.6 * pl.col("xfp_pg") + 0.4 * pl.col("ppg")
    ).otherwise(pl.col("ppg"))
    k = pl.col("position").replace_strict(_K, default=_K_DEFAULT, return_dtype=pl.Float64)
    # rookies have no NFL baseline (the "prior" is only a draft-rank guess), so trust 2026 evidence sooner
    k = pl.when(pl.col("g25").is_null()).then(k * 0.5).otherwise(k)
    n = pl.col("g").cast(pl.Float64)
    t = t.with_columns(prior.alias("prior_ppg"), evidence.alias("evidence_ppg"), k.alias("_k_pg"))
    t = t.with_columns(
        pl.when(pl.col("prior_ppg").is_null() & (n == 0))
        .then(None)
        .when(pl.col("prior_ppg").is_null())
        .then(pl.col("evidence_ppg"))
        .when(n == 0)
        .then(pl.col("prior_ppg"))
        .otherwise((n * pl.col("evidence_ppg") + pl.col("_k_pg") * pl.col("prior_ppg")) / (n + pl.col("_k_pg")))
        .alias("forecast")
    )
    t = t.with_columns(
        pl.coalesce(["sd25", pl.col("forecast") * 0.45]).clip(lower_bound=1.0).alias("sd"),
        (pl.col("ppg") - pl.col("prior_ppg")).alias("vs_prior"),
        (pl.col("xfp_pg") - pl.col("xfp25")).alias("usage_delta"),
    )
    z_lo, z_hi = norm.ppf(0.2), norm.ppf(0.8)
    t = t.with_columns(
        (pl.col("forecast") + z_lo * pl.col("sd")).clip(lower_bound=0).alias("floor"),
        (pl.col("forecast") + z_hi * pl.col("sd")).alias("ceiling"),
        _signal_expr().alias("signal"),
    ).drop("_k", "_k_pg")
    return t.filter(pl.col("forecast").is_not_null()).sort("forecast", descending=True)


def _nk(s: str) -> str:
    import re

    return re.sub(r"[^a-z]", "", re.sub(r"\s+(jr|sr|ii|iii|iv|v)\.?$", "", s.lower()))


def _signal_expr() -> pl.Expr:
    """Plain-English read of the trend evidence."""
    return (
        pl.when(pl.col("g") < 1)
        .then(pl.lit("no 2026 sample"))
        .when(pl.col("finish_luck") >= 4)
        .then(pl.lit("SELL: scoring well above usage (TD luck)"))
        .when(pl.col("finish_luck") <= -4)
        .then(pl.lit("BUY: usage far above results"))
        .when(pl.col("snap_trend") >= 10)
        .then(pl.lit("RISING role (snaps up)"))
        .when(pl.col("snap_trend") <= -10)
        .then(pl.lit("FALLING role (snaps down)"))
        .otherwise(pl.lit("stable"))
    )


# ----------------------------------------------------------------- team layer
def load_rosters() -> pl.DataFrame:
    path = load_settings().root / "config" / "league_rosters.csv"
    if not path.exists():
        raise FileNotFoundError("config/league_rosters.csv missing (snapshot of every Yahoo roster).")
    return pl.read_csv(path).with_columns(pl.col("name").map_elements(_nk, return_dtype=pl.Utf8).alias("_k"))


def _attach(rosters: pl.DataFrame, pt: pl.DataFrame) -> pl.DataFrame:
    p = pt.with_columns(pl.col("name").map_elements(_nk, return_dtype=pl.Utf8).alias("_k"))
    p = p.sort("forecast", descending=True).unique(subset=["_k", "position"], keep="first")
    # a name can exist under two positions (rare); keep the higher-scoring one
    p = p.sort("forecast", descending=True).unique(subset=["_k"], keep="first")
    return rosters.join(p.drop("name"), on="_k", how="left")


def _players(df: pl.DataFrame) -> list[lu.Player]:
    out = []
    for r in df.iter_rows(named=True):
        pos = r["position"]
        if pos is None:
            continue
        out.append(lu.Player(r["name"], pos, float(r["forecast"] or 0), float(r["floor"] or 0), float(r["ceiling"] or 0)))
    return out


def _fill_static(players: list[lu.Player], teams_have_k: bool) -> list[lu.Player]:
    ps = list(players)
    if not any(p.position == "K" for p in ps):
        ps.append(lu.Player("K(avg)", "K", 8.0, 5.0, 11.0))
    ps.append(lu.Player("DEF(avg)", "DEF", _DEF_DEFAULT, 3.0, 11.0))
    return ps


def team_table(pt: pl.DataFrame) -> tuple[pl.DataFrame, dict[str, dict], pl.DataFrame]:
    """Returns (team summary, {team: lineup}, unmatched roster names)."""
    rules = scoring.load_rules(load_settings().league.current_season)
    ros = _attach(load_rosters(), pt)
    ros = ros.filter(~pl.col("slot").is_in(["IR"]) | pl.col("forecast").is_not_null())
    unmatched = ros.filter(pl.col("position").is_null() & ~pl.col("slot").is_in(["DEF"]) & (pl.col("name").str.len_chars() > 0)
                           & ~pl.col("name").is_in(_TEAM_DEFENCES)).select("team", "name")
    rows, lineups = [], {}
    for team in ros["team"].unique(maintain_order=True).to_list():
        roster = ros.filter(pl.col("team") == team)
        active = roster.filter(pl.col("slot") != "IR")
        players = _fill_static(_players(active), True)
        best = lu.optimize(players, rules, "proj")
        lineups[team] = best
        mean = sum(p.proj for p in best.values())
        sd = float(np.sqrt(sum(((p.ceiling - p.floor) / (2 * 0.8416)) ** 2 for p in best.values())))
        starters = set(best.values())
        bench = [p for p in players if p not in starters]
        by_pos = {}
        for sid, p in best.items():
            by_pos.setdefault(sid.split("#")[0], 0.0)
            by_pos[sid.split("#")[0]] += p.proj
        rows.append({"team": team, "proj_week": mean, "sd": sd,
                     "bench_pts": sum(sorted((b.proj for b in bench), reverse=True)[:3]), **by_pos})
    return pl.DataFrame(rows), lineups, unmatched


_TEAM_DEFENCES = {
    "Texans", "Eagles", "Ravens", "Seahawks", "Broncos", "Vikings", "Lions", "49ers", "Chargers",
    "Patriots", "Rams", "Steelers", "Buccaneers", "Jaguars",
}


def load_standings() -> pl.DataFrame:
    path = load_settings().root / "config" / "standings.csv"
    if not path.exists():
        raise FileNotFoundError("config/standings.csv missing (team,wins,losses,pf,pa,week_scores...).")
    return pl.read_csv(path)


def power_table(team_df: pl.DataFrame, standings: pl.DataFrame) -> pl.DataFrame:
    """Forecast strength + all-play (each week's score vs every other team) vs actual record."""
    weeks = [c for c in standings.columns if c.startswith("w") and c[1:].isdigit()]
    s = standings.clone()
    allplay_w = {t: 0 for t in s["team"]}
    allplay_g = {t: 0 for t in s["team"]}
    for w in weeks:
        sc = dict(zip(s["team"], s[w], strict=True))
        played = {t: v for t, v in sc.items() if v is not None}
        for t, v in played.items():
            for o, ov in played.items():
                if o != t:
                    allplay_g[t] += 1
                    allplay_w[t] += 1 if v > ov else 0
    s = s.with_columns(
        pl.col("team").replace_strict(allplay_w, return_dtype=pl.Int64).alias("ap_w"),
        pl.col("team").replace_strict(allplay_g, return_dtype=pl.Int64).alias("ap_g"),
    ).with_columns((pl.col("ap_w") / pl.col("ap_g").clip(lower_bound=1)).alias("allplay_pct"))
    out = team_df.join(s.select("team", "wins", "losses", "pf", "pa", "allplay_pct"), on="team", how="left")
    lg = out["proj_week"].mean()
    out = out.with_columns((pl.col("proj_week") - lg).alias("vs_avg"))
    return out.sort("proj_week", descending=True).with_row_index("power_rank", offset=1)


# ----------------------------------------------------------------- odds layer
def _schedule(teams: list[str], weeks: list[int], seed: int = 7) -> list[tuple[int, str, str]]:
    """config/schedule.csv (week,home,away) if present, else circle-method round robin."""
    path = load_settings().root / "config" / "schedule.csv"
    if path.exists():
        df = pl.read_csv(path).filter(pl.col("week").is_in(weeks))
        return [(int(r["week"]), r["home"], r["away"]) for r in df.iter_rows(named=True)]
    rng = np.random.default_rng(seed)
    ts = list(teams)
    rng.shuffle(ts)
    n = len(ts)
    rounds = []
    arr = ts[:]
    for _ in range(n - 1):
        rounds.append([(arr[i], arr[n - 1 - i]) for i in range(n // 2)])
        arr = [arr[0]] + [arr[-1]] + arr[1:-1]
    sched = []
    for i, w in enumerate(weeks):
        for a, b in rounds[i % len(rounds)]:
            sched.append((w, a, b))
    return sched


def odds_table(power: pl.DataFrame, first_week: int, last_week: int = 14, iterations: int = 4000) -> pl.DataFrame:
    settings = load_settings()
    teams = power["team"].to_list()
    weeks = list(range(first_week, last_week + 1))
    sched = _schedule(teams, weeks)
    mean = {(r["team"], w): r["proj_week"] for r in power.iter_rows(named=True) for w in weeks}
    sd = {(r["team"], w): r["sd"] for r in power.iter_rows(named=True) for w in weeks}
    wins = {r["team"]: int(r["wins"] or 0) for r in power.iter_rows(named=True)}
    pf = {r["team"]: float(r["pf"] or 0) for r in power.iter_rows(named=True)}
    return simulate(sched, mean, sd, wins, pf, settings.standings.playoff_teams, iterations)


# --------------------------------------------------------------- waiver layer
def waiver_table(team: str, pt: pl.DataFrame, top_n: int = 60) -> pl.DataFrame:
    """Free agents ranked by *marginal optimal-lineup gain* for `team` (not a positional guess)."""
    rules = scoring.load_rules(load_settings().league.current_season)
    ros = load_rosters()
    rostered = set(ros["_k"].to_list())
    mine = _attach(ros.filter(pl.col("team") == team).filter(pl.col("slot") != "IR"), pt)
    base_players = _fill_static(_players(mine), True)
    base = lu.optimize(base_players, rules, "proj")
    base_pts = sum(p.proj for p in base.values())
    starters = {p.name for p in base.values()}

    pool = (
        pt.with_columns(pl.col("name").map_elements(_nk, return_dtype=pl.Utf8).alias("_k"))
        .filter(~pl.col("_k").is_in(list(rostered)) & pl.col("position").is_in(_POS + ["K"]))
        .sort("forecast", descending=True)
        .group_by("position", maintain_order=True)
        .head(15)
    )
    rows = []
    for r in pool.iter_rows(named=True):
        cand = lu.Player(r["name"], r["position"], float(r["forecast"]), float(r["floor"]), float(r["ceiling"]))
        new = lu.optimize(base_players + [cand], rules, "proj")
        gain = sum(p.proj for p in new.values()) - base_pts
        displaced = sorted(starters - {p.name for p in new.values()})
        rows.append({
            "name": r["name"], "pos": r["position"], "team": r["team"], "g": r["g"],
            "forecast": round(r["forecast"], 1), "ppg26": None if r["ppg"] is None else round(r["ppg"], 1),
            "xfp": None if r["xfp_pg"] is None else round(r["xfp_pg"], 1),
            "luck": None if r["finish_luck"] is None else round(r["finish_luck"], 1),
            "snap_trend": None if r["snap_trend"] is None else round(r["snap_trend"] * 1.0, 0),
            "lineup_gain": round(gain, 2), "replaces": ", ".join(displaced), "signal": r["signal"],
        })
    out = pl.DataFrame(rows).sort("lineup_gain", descending=True)
    return out.head(top_n)


def profile(names: list[str], pt: pl.DataFrame) -> pl.DataFrame:
    keys = {_nk(n) for n in names}
    p = pt.with_columns(pl.col("name").map_elements(_nk, return_dtype=pl.Utf8).alias("_k"))
    return p.filter(pl.col("_k").is_in(list(keys))).select(
        "name", "position", "team", "g", "ppg", "last2", "xfp_pg", "finish_luck", "snap_avg", "snap_trend",
        "ppg25", "sd25", "boom25", "bust25", "prior_ppg", "forecast", "floor", "ceiling", "signal",
    )
