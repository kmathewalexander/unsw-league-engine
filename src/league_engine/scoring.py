"""League scoring engine.

Reads the league's Yahoo settings snapshot, turns the stat modifiers into a set of
weighted rules, and applies them to nflverse weekly stat lines to produce
**league-accurate fantasy points** for every player-week. That points table is the
common currency for every downstream analysis (draft value, lineup, waivers, trades,
standings).

Design notes:
  * Stats are matched by *normalised display name* first (robust across leagues),
    Yahoo stat_id second.
  * Anything we can't map is surfaced in ``ScoringRules.unmapped`` so the
    reconciliation test and the ``sync`` output can flag it for a manual mapping
    in ``STAT_EXPR`` rather than silently scoring zero.
  * Team DEF scoring depends on points/yards allowed and is computed in
    ``team_defense_points`` from schedules + team aggregates, not from this map.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import polars as pl

from . import store

# --- normalised Yahoo stat name -> polars expression over nflverse player_stats ---
# Key: alnum-lowercased Yahoo stat display_name (and common aliases).
# Value: a polars expression yielding the per-game count for that stat.

_C = pl.col


def _first_downs_expr() -> pl.Expr:
    return _C("passing_first_downs") + _C("rushing_first_downs") + _C("receiving_first_downs")


STAT_EXPR: dict[str, pl.Expr] = {
    # passing
    "passyds": _C("passing_yards"),
    "passingyards": _C("passing_yards"),
    "passtd": _C("passing_tds"),
    "passingtouchdowns": _C("passing_tds"),
    "int": _C("passing_interceptions"),
    "interceptions": _C("passing_interceptions"),
    "passint": _C("passing_interceptions"),
    "passatt": _C("attempts"),
    "passingattempts": _C("attempts"),
    "passcmp": _C("completions"),
    "completions": _C("completions"),
    "passcompletions": _C("completions"),
    "incompletions": _C("attempts") - _C("completions"),
    "passincompletions": _C("attempts") - _C("completions"),
    "pass1stdowns": _C("passing_first_downs"),
    "passingfirstdowns": _C("passing_first_downs"),
    "pass40yddtd": _C("passing_40"),  # bonus category (40+ yd pass TD proxy: 40+ yd pass)
    # rushing
    "rushatt": _C("carries"),
    "rushingattempts": _C("carries"),
    "rushyds": _C("rushing_yards"),
    "rushingyards": _C("rushing_yards"),
    "rushtd": _C("rushing_tds"),
    "rushingtouchdowns": _C("rushing_tds"),
    "rush1stdowns": _C("rushing_first_downs"),
    "rushingfirstdowns": _C("rushing_first_downs"),
    # receiving
    "rec": _C("receptions"),
    "receptions": _C("receptions"),
    "recyds": _C("receiving_yards"),
    "receivingyards": _C("receiving_yards"),
    "rectd": _C("receiving_tds"),
    "receivingtouchdowns": _C("receiving_tds"),
    "rec1stdowns": _C("receiving_first_downs"),
    "receivingfirstdowns": _C("receiving_first_downs"),
    "targets": _C("targets"),
    # misc offense
    "rettd": _C("special_teams_tds"),
    "returntd": _C("special_teams_tds"),
    "returntouchdowns": _C("special_teams_tds"),
    "kickoffandpuntreturntouchdowns": _C("special_teams_tds"),
    "retyds": _C("punt_return_yards") + _C("kickoff_return_yards"),
    "returnyards": _C("punt_return_yards") + _C("kickoff_return_yards"),
    "2ptconversions": (
        _C("passing_2pt_conversions") + _C("rushing_2pt_conversions") + _C("receiving_2pt_conversions")
    ),
    "2pt": (
        _C("passing_2pt_conversions") + _C("rushing_2pt_conversions") + _C("receiving_2pt_conversions")
    ),
    "fumlost": _C("fumbles_lost_total"),
    "fumbleslost": _C("fumbles_lost_total"),
    "fumrectd": _C("fumble_recovery_tds"),
    "fumblereturntd": _C("fumble_recovery_tds"),
    "totalfirstdowns": _first_downs_expr(),
    # kicker
    "fgmade": _C("fg_made"),
    "fieldgoalsmade": _C("fg_made"),
    "fgmissed": _C("fg_missed"),
    "fieldgoalsmissed": _C("fg_missed"),
    "fg019": _C("fg_made_0_19"),
    "fgm019": _C("fg_made_0_19"),
    "fg2029": _C("fg_made_20_29"),
    "fgm2029": _C("fg_made_20_29"),
    "fg3039": _C("fg_made_30_39"),
    "fgm3039": _C("fg_made_30_39"),
    "fg4049": _C("fg_made_40_49"),
    "fgm4049": _C("fg_made_40_49"),
    "fg50": _C("fg_made_50_59") + _C("fg_made_60_"),
    "fgm50": _C("fg_made_50_59") + _C("fg_made_60_"),
    "patmade": _C("pat_made"),
    "pointaftermade": _C("pat_made"),
    "extrapointconverted": _C("pat_made"),
    "patmissed": _C("pat_missed"),
    "pointaftermissed": _C("pat_missed"),
    # IDP
    "tacklesolo": _C("def_tackles_solo"),
    "tacklessolo": _C("def_tackles_solo"),
    "tackleassist": _C("def_tackle_assists"),
    "tacklesassist": _C("def_tackle_assists"),
    "sack": _C("def_sacks"),
    "sacks": _C("def_sacks"),
    "defint": _C("def_interceptions"),
    "fumbleforce": _C("def_fumbles_forced"),
    "forcedfumble": _C("def_fumbles_forced"),
    "fumblerec": _C("def_fumbles"),
    "fumblerecovery": _C("def_fumbles"),
    "deftd": _C("def_tds"),
    "safety": _C("def_safeties"),
    "safe": _C("def_safeties"),
    "passdefended": _C("def_pass_defended"),
    "passesdefended": _C("def_pass_defended"),
    "blockkick": _C("def_fg_blocks") + _C("def_pat_blocks") + _C("def_punt_blocks"),
    "blockedkick": _C("def_fg_blocks") + _C("def_pat_blocks") + _C("def_punt_blocks"),
}

# Yahoo NFL stat_id -> normalised name, used as a fallback when the display name
# doesn't match anything above. Only the stable core ids.
STAT_ID_ALIAS: dict[int, str] = {
    4: "passyds", 5: "passtd", 6: "int", 8: "rushatt", 9: "rushyds", 10: "rushtd",
    11: "rec", 12: "recyds", 13: "rectd", 15: "returnyards", 16: "returntd",
    18: "2ptconversions", 20: "fumlost", 57: "targets",
    78: "pass1stdowns", 79: "rush1stdowns", 80: "rec1stdowns",
}

# Yahoo positions that are scored via team_defense_points, not the stat map.
DEF_POSITIONS = {"DEF", "DST"}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


@dataclass
class ScoringRule:
    stat_id: int
    name: str
    norm: str
    multiplier: float
    expr: pl.Expr | None  # None => unmapped


@dataclass
class RosterSlot:
    position: str
    count: int


@dataclass
class ScoringRules:
    season: int
    rules: list[ScoringRule]
    roster: list[RosterSlot]
    unmapped: list[str] = field(default_factory=list)

    @property
    def is_ppr(self) -> float:
        for r in self.rules:
            if r.norm in {"rec", "receptions"}:
                return r.multiplier
        return 0.0

    @property
    def starting_slots(self) -> list[RosterSlot]:
        bench = {"BN", "IR", "IL", "NA"}
        return [s for s in self.roster if s.position not in bench]

    def describe(self) -> str:
        lines = [f"Scoring rules (season {self.season}):"]
        for r in sorted(self.rules, key=lambda x: -abs(x.multiplier)):
            flag = "" if r.expr is not None else "  <-- UNMAPPED, scores 0"
            lines.append(f"  {r.name:<28} x {r.multiplier:+.3f}{flag}")
        slot_str = ", ".join(f"{s.position}:{s.count}" for s in self.roster)
        lines.append(f"Roster: {slot_str}")
        if self.unmapped:
            lines.append(f"Unmapped stats: {', '.join(self.unmapped)}")
        return "\n".join(lines)


def _as_list(node: object) -> list:
    if node is None:
        return []
    if isinstance(node, list):
        return node
    return [node]


def parse_settings(settings_json: dict, season: int) -> ScoringRules:
    """Parse a Yahoo ``get_league_settings`` serialized dict into ScoringRules."""
    cats_raw = settings_json.get("stat_categories", {})
    stats = _as_list(cats_raw.get("stats") if isinstance(cats_raw, dict) else cats_raw)
    name_by_id: dict[int, str] = {}
    for s in stats:
        s = s.get("stat", s) if isinstance(s, dict) else s
        try:
            sid = int(s.get("stat_id"))
        except (TypeError, ValueError):
            continue
        name_by_id[sid] = s.get("display_name") or s.get("name") or str(sid)

    mods_raw = settings_json.get("stat_modifiers", {})
    mods = _as_list(mods_raw.get("stats") if isinstance(mods_raw, dict) else mods_raw)

    rules: list[ScoringRule] = []
    unmapped: list[str] = []
    for m in mods:
        m = m.get("stat", m) if isinstance(m, dict) else m
        try:
            sid = int(m.get("stat_id"))
            mult = float(m.get("value"))
        except (TypeError, ValueError):
            continue
        name = name_by_id.get(sid, STAT_ID_ALIAS.get(sid, str(sid)))
        norm = _norm(name)
        expr = STAT_EXPR.get(norm)
        if expr is None and sid in STAT_ID_ALIAS:
            norm = STAT_ID_ALIAS[sid]
            expr = STAT_EXPR.get(norm)
        if expr is None:
            unmapped.append(f"{name} (id {sid})")
        rules.append(ScoringRule(sid, name, norm, mult, expr))

    rp_raw = settings_json.get("roster_positions", [])
    roster: list[RosterSlot] = []
    for rp in _as_list(rp_raw):
        rp = rp.get("roster_position", rp) if isinstance(rp, dict) else rp
        pos = rp.get("position")
        cnt = rp.get("count")
        if pos is not None and cnt is not None:
            roster.append(RosterSlot(str(pos), int(cnt)))

    return ScoringRules(season=season, rules=rules, roster=roster, unmapped=unmapped)


# --- manual scoring config (config/scoring.toml) -------------------------------
# Clean, documented keys the user enters by hand from the Yahoo league settings page
# when the Yahoo API is unavailable. Maps to the same expressions as the Yahoo path.
MANUAL_KEYS: dict[str, pl.Expr] = {
    # passing
    "passing_yards": _C("passing_yards"),
    "passing_td": _C("passing_tds"),
    "passing_interception": _C("passing_interceptions"),
    "passing_attempt": _C("attempts"),
    "passing_completion": _C("completions"),
    "passing_incompletion": _C("attempts") - _C("completions"),
    "passing_first_down": _C("passing_first_downs"),
    "passing_2pt": _C("passing_2pt_conversions"),
    "passing_sack": _C("sacks_suffered"),
    # rushing
    "rushing_attempt": _C("carries"),
    "rushing_yards": _C("rushing_yards"),
    "rushing_td": _C("rushing_tds"),
    "rushing_first_down": _C("rushing_first_downs"),
    "rushing_2pt": _C("rushing_2pt_conversions"),
    # receiving
    "reception": _C("receptions"),
    "receiving_yards": _C("receiving_yards"),
    "receiving_td": _C("receiving_tds"),
    "receiving_first_down": _C("receiving_first_downs"),
    "receiving_2pt": _C("receiving_2pt_conversions"),
    "target": _C("targets"),
    # misc offense
    "return_td": _C("special_teams_tds"),
    "return_yards": _C("punt_return_yards") + _C("kickoff_return_yards"),
    "two_point_conversion": (
        _C("passing_2pt_conversions") + _C("rushing_2pt_conversions") + _C("receiving_2pt_conversions")
    ),
    "fumble_lost": _C("fumbles_lost_total"),
    "fumble_recovery_td": _C("fumble_recovery_tds"),
    # kicker
    "fg_made": _C("fg_made"),
    "fg_missed": _C("fg_missed"),
    "fg_made_0_19": _C("fg_made_0_19"),
    "fg_made_20_29": _C("fg_made_20_29"),
    "fg_made_30_39": _C("fg_made_30_39"),
    "fg_made_40_49": _C("fg_made_40_49"),
    "fg_made_50_plus": _C("fg_made_50_59") + _C("fg_made_60_"),
    "pat_made": _C("pat_made"),
    "pat_missed": _C("pat_missed"),
    # IDP
    "idp_tackle_solo": _C("def_tackles_solo"),
    "idp_tackle_assist": _C("def_tackle_assists"),
    "idp_sack": _C("def_sacks"),
    "idp_interception": _C("def_interceptions"),
    "idp_forced_fumble": _C("def_fumbles_forced"),
    "idp_fumble_recovery": _C("def_fumbles"),
    "idp_td": _C("def_tds"),
    "idp_pass_defended": _C("def_pass_defended"),
    "idp_safety": _C("def_safeties"),
}


def parse_manual(data: dict, season: int) -> ScoringRules:
    """Parse ``config/scoring.toml`` -> ScoringRules.

    Expected shape::

        [[stat]]
        key = "passing_yards"
        points = 0.04

        [roster]
        QB = 1
        RB = 2
        "W/R/T" = 1
        BN = 6
    """
    rules: list[ScoringRule] = []
    unmapped: list[str] = []
    for i, item in enumerate(data.get("stat", [])):
        key = str(item.get("key", "")).strip()
        try:
            pts = float(item.get("points"))
        except (TypeError, ValueError):
            continue
        expr = MANUAL_KEYS.get(key)
        if expr is None:
            unmapped.append(key)
        rules.append(ScoringRule(stat_id=-(i + 1), name=key, norm=key, multiplier=pts, expr=expr))

    roster = [RosterSlot(str(pos), int(cnt)) for pos, cnt in (data.get("roster", {}) or {}).items()]
    return ScoringRules(season=season, rules=rules, roster=roster, unmapped=unmapped)


def manual_config_path():
    from .config import load_settings

    return load_settings().root / "config" / "scoring.toml"


def load_rules(season: int) -> ScoringRules:
    """Prefer the hand-entered config/scoring.toml; fall back to the Yahoo snapshot."""
    manual = manual_config_path()
    if manual.exists():
        import tomllib

        with manual.open("rb") as fh:
            return parse_manual(tomllib.load(fh), season)
    if store.json_exists("raw/yahoo", str(season), "league_settings"):
        data = store.read_json("raw/yahoo", str(season), "league_settings")
        if isinstance(data, dict) and "settings" in data:
            data = data["settings"]
        return parse_settings(data, season)
    raise FileNotFoundError(
        "No scoring rules found. Either run `playbook sync` with Yahoo API access, "
        "or fill in config/scoring.toml (copy config/scoring.toml.example)."
    )


def score_weekly(player_stats: pl.DataFrame, rules: ScoringRules) -> pl.DataFrame:
    """Add a ``points`` column to an nflverse player_stats frame using ``rules``.

    DEF/DST rows are left at 0 here (scored separately via team_defense_points).
    """
    df = player_stats
    # Ensure referenced columns exist and are numeric; fill nulls with 0.
    needed: set[str] = set()
    for r in rules.rules:
        if r.expr is not None:
            needed |= set(r.expr.meta.root_names())
    for c in needed:
        if c not in df.columns:
            df = df.with_columns(pl.lit(0.0).alias(c))
    df = df.with_columns([pl.col(c).cast(pl.Float64, strict=False).fill_null(0.0) for c in needed])

    total = pl.lit(0.0)
    for r in rules.rules:
        if r.expr is not None:
            total = total + r.expr.fill_null(0.0) * r.multiplier
    return df.with_columns(total.alias("points"))


def build_points_table(current_season: int) -> pl.DataFrame:
    """Compute league points for every player-week across all pulled nflverse seasons.

    Uses the current league's scoring rules for every season (so historical player
    value is expressed in *today's* scoring). Writes data/derived/league_points.parquet.
    """
    rules = load_rules(current_season)
    stats = store.read("raw/nflverse", "player_stats")
    scored = score_weekly(stats, rules)
    keep = [
        "player_id", "player_display_name", "position", "position_group",
        "season", "week", "season_type", "team", "opponent_team", "points",
    ]
    out = scored.select([c for c in keep if c in scored.columns])
    store.write(out, "derived", "league_points")
    return out


def reconcile(season: int, sample: int = 25) -> pl.DataFrame:
    """Compare engine points vs Yahoo-reported points for a sample of roster player-weeks.

    Requires a ``roster_player_stats_week_<w>`` style snapshot; if absent, returns
    an empty frame (the caller/test skips).
    """
    # Left as a helper the reconciliation test drives with explicit fixtures.
    raise NotImplementedError("Driven by tests/test_scoring.py with Yahoo roster fixtures.")
