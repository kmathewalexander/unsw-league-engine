"""Draft analysis: historical league tendencies, a value board, and live pick assist."""

from __future__ import annotations

from dataclasses import dataclass

import polars as pl

from .. import scoring, store
from ..config import load_settings

SKILL_POS = ["QB", "RB", "WR", "TE"]


# --------------------------------------------------------------------------
# 1. Historical draft tendencies
# --------------------------------------------------------------------------

def _draft_frame() -> pl.DataFrame:
    """Union every season's Yahoo draft_results snapshot into a tidy frame."""
    settings = load_settings()
    rows: list[dict] = []
    for season in sorted(settings.league.history):
        if not store.json_exists("raw/yahoo", str(season), "draft_results"):
            continue
        data = store.read_json("raw/yahoo", str(season), "draft_results")
        for dr in data if isinstance(data, list) else []:
            dr = dr.get("draft_result", dr) if isinstance(dr, dict) else dr
            rows.append(
                {
                    "season": season,
                    "pick": _int(dr.get("pick")),
                    "round": _int(dr.get("round")),
                    "team_key": dr.get("team_key"),
                    "player_key": dr.get("player_key"),
                    "cost": _int(dr.get("cost")),
                }
            )
    return pl.DataFrame(rows) if rows else pl.DataFrame()


def _int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def manager_profiles() -> pl.DataFrame:
    """Per-manager draft behaviour across all seasons.

    Columns: team_key, drafts, avg_first_pos, rb_by_round3, wr_by_round3,
    qb_avg_round, te_avg_round, reach_index (avg pick minus positional ADP rank).
    """
    df = _draft_frame()
    if df.is_empty():
        return df
    players = _player_pos_lookup()
    df = df.join(players, on="player_key", how="left")

    prof = (
        df.group_by("team_key")
        .agg(
            pl.col("season").n_unique().alias("drafts"),
            pl.col("round").filter(pl.col("position") == "QB").mean().alias("qb_avg_round"),
            pl.col("round").filter(pl.col("position") == "TE").mean().alias("te_avg_round"),
            (pl.col("position").filter(pl.col("round") <= 3) == "RB").mean().alias("rb_share_r1_3"),
            (pl.col("position").filter(pl.col("round") <= 3) == "WR").mean().alias("wr_share_r1_3"),
        )
    )
    return prof


def positional_runs() -> pl.DataFrame:
    """How often each position goes on a run (3+ of same position in a 5-pick window)."""
    df = _draft_frame()
    if df.is_empty():
        return df
    df = df.join(_player_pos_lookup(), on="player_key", how="left").sort(["season", "pick"])
    return (
        df.with_columns(
            pl.col("position")
            .rolling_map(lambda s: (s == s.mode().first()).sum(), window_size=5)
            .over("season")
            .alias("cluster")
        )
        .group_by("position")
        .agg((pl.col("cluster") >= 3).mean().alias("run_rate"))
        .sort("run_rate", descending=True)
    )


def _player_pos_lookup() -> pl.DataFrame:
    """player_key -> (name, position) from the league_players snapshot(s).

    ``playbook sync`` writes ``league_players`` per season (name, position, player_key).
    Falls back to an empty frame so joins stay left-joins.
    """
    settings = load_settings()
    frames: list[pl.DataFrame] = []
    seasons = sorted(set(settings.league.history) | {settings.league.current_season}, reverse=True)
    for season in seasons:
        if not store.json_exists("raw/yahoo", str(season), "league_players"):
            continue
        data = store.read_json("raw/yahoo", str(season), "league_players")
        rows: list[dict] = []
        for p in data if isinstance(data, list) else []:
            p = p.get("player", p) if isinstance(p, dict) else p
            name = p.get("name")
            if isinstance(name, dict):
                name = name.get("full")
            rows.append(
                {
                    "player_key": p.get("player_key"),
                    "name": name,
                    "position": p.get("primary_position") or p.get("display_position"),
                }
            )
        if rows:
            frames.append(pl.DataFrame(rows))
    if not frames:
        return pl.DataFrame(schema={"player_key": pl.Utf8, "name": pl.Utf8, "position": pl.Utf8})
    return pl.concat(frames).unique(subset=["player_key"], keep="first")


# --------------------------------------------------------------------------
# 2. Value board
# --------------------------------------------------------------------------

@dataclass
class BoardConfig:
    teams: int = 12
    # starters demanded league-wide drives replacement level
    weeks: int = 17


def replacement_levels(rules: scoring.ScoringRules, teams: int) -> dict[str, int]:
    """Rank at each position where a replacement-level starter sits, from roster slots."""
    demand = {"QB": 0, "RB": 0, "WR": 0, "TE": 0, "K": 0, "DEF": 0}
    flex = 0
    for slot in rules.starting_slots:
        p = slot.position.upper()
        if p in demand:
            demand[p] += slot.count
        elif p in {"W/R/T", "W/R", "FLEX", "R/W/T"} or p == "Q/W/R/T":
            flex += slot.count
    # distribute flex 20/55/25 RB/WR/TE as a league-average assumption
    demand["RB"] += round(flex * 0.55)
    demand["WR"] += round(flex * 0.25)
    demand["TE"] += round(flex * 0.20)
    return {pos: max(1, n) * teams for pos, n in demand.items()}


def value_board(teams: int = 12) -> pl.DataFrame:
    """VOR board in league points. Requires derived/proj_season + ECR/ADP.

    Columns: name, position, proj_points, vor, adp, value_vs_adp, tier.
    """
    settings = load_settings()
    rules = scoring.load_rules(settings.league.current_season)
    proj = store.read("derived", "proj_season")

    repl = replacement_levels(rules, teams)
    parts = []
    for pos, cutoff in repl.items():
        p = proj.filter(pl.col("position") == pos).sort("season_points", descending=True)
        if p.is_empty():
            continue
        repl_val = p["season_points"].gather(min(cutoff, p.height) - 1).item() if p.height else 0.0
        parts.append(p.with_columns((pl.col("season_points") - repl_val).alias("vor")))
    board = pl.concat(parts, how="diagonal_relaxed") if parts else proj.with_columns(pl.lit(0.0).alias("vor"))

    # attach ADP
    adp = _adp_frame()
    board = board.join(adp, on="name", how="left")
    board = board.sort("vor", descending=True).with_columns(
        pl.arange(1, pl.len() + 1).alias("overall_rank"),
    )
    board = board.with_columns((pl.col("adp") - pl.col("overall_rank")).alias("value_vs_adp"))

    # positional tiers: new tier when the drop from the prior player exceeds
    # ~0.6 std of that position's season points
    board = board.sort(["position", "season_points"], descending=[False, True])
    board = board.with_columns(
        (
            (pl.col("season_points").shift(1).over("position") - pl.col("season_points"))
            > (0.6 * pl.col("season_points").std().over("position"))
        )
        .fill_null(True)
        .cast(pl.Int32)
        .cum_sum()
        .over("position")
        .add(1)
        .alias("tier")
    )
    board = board.sort("vor", descending=True, nulls_last=True)
    store.write(board, "derived", "value_board")
    return board


def _adp_frame() -> pl.DataFrame:
    """Overall consensus draft position. nflverse redraft-overall ECR is the primary
    (complete, current); the FantasyPros cheatsheet scrape is a secondary source."""
    if store.exists("raw/nflverse", "ff_rankings"):
        d = store.read("raw/nflverse", "ff_rankings").filter(pl.col("page_type") == "redraft-overall")
        if d.height:
            return (
                d.select(pl.col("player").alias("name"), pl.col("ecr").cast(pl.Float64).alias("adp"))
                .drop_nulls()
                .unique(subset=["name"], keep="first")
            )
    if store.exists("raw/fantasypros", "ecr_draft"):
        d = store.read("raw/fantasypros", "ecr_draft")
        return d.select(pl.col("name"), pl.col("ecr").cast(pl.Float64).alias("adp")).drop_nulls()
    return pl.DataFrame(schema={"name": pl.Utf8, "adp": pl.Float64})


# --------------------------------------------------------------------------
# 3. Live pick assist
# --------------------------------------------------------------------------

def recommend_pick(
    drafted_names: list[str],
    my_roster_names: list[str],
    picks_until_next: int,
    teams: int = 12,
) -> pl.DataFrame:
    """Given the board so far, rank the best available picks for me right now.

    Score = VOR, minus an urgency discount for players likely to survive to my next
    pick, plus a roster-need bonus for positions I'm thin at.
    """
    board = store.read("derived", "value_board") if store.exists("derived", "value_board") else value_board(teams)
    taken = {_n(x) for x in drafted_names}
    avail = board.filter(~pl.col("name").map_elements(_n, return_dtype=pl.Utf8).is_in(list(taken)))

    have_df = board.filter(
        pl.col("name").map_elements(_n, return_dtype=pl.Utf8).is_in([_n(x) for x in my_roster_names])
    )
    have = dict(have_df.group_by("position").len().iter_rows())
    need_bonus = {
        "RB": 10.0 if have.get("RB", 0) < 2 else (4.0 if have.get("RB", 0) < 4 else 0.0),
        "WR": 10.0 if have.get("WR", 0) < 2 else (4.0 if have.get("WR", 0) < 4 else 0.0),
        "QB": 6.0 if have.get("QB", 0) < 1 else 0.0,
        "TE": 5.0 if have.get("TE", 0) < 1 else 0.0,
        "K": -20.0 if len(my_roster_names) < 13 else 0.0,
        "DEF": -15.0 if len(my_roster_names) < 12 else 0.0,
    }

    my_pick_now = len(drafted_names) + 1
    my_next_pick = my_pick_now + picks_until_next
    # P(player still on the board at my next pick): players go ~in ADP order with spread.
    adp = pl.col("adp").fill_null(300.0)
    p_gone = (1.0 / (1.0 + (-(my_next_pick - adp) / 8.0).exp())).clip(0.0, 1.0)

    return (
        avail.with_columns(
            pl.col("position").replace_strict(need_bonus, default=0.0).alias("need_bonus"),
            p_gone.alias("p_gone_by_next"),
        )
        .with_columns(
            (
                pl.col("vor").fill_null(0.0)
                + pl.col("need_bonus")
                + pl.col("vor").fill_null(0.0).clip(lower_bound=0) * pl.col("p_gone_by_next") * 0.4
            ).alias("pick_score")
        )
        .sort("pick_score", descending=True, nulls_last=True)
        .select(
            "name", "position", "season_points", "vor", "adp", "tier",
            "need_bonus", "p_gone_by_next", "pick_score",
        )
        .head(15)
    )


def _n(s: str) -> str:
    return "".join(c for c in (s or "").lower() if c.isalpha())
