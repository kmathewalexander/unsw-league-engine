"""Optimal lineup via integer linear programming (PuLP)."""

from __future__ import annotations

from dataclasses import dataclass

import polars as pl
import pulp

from .. import scoring

# Which roster positions each real position is eligible for.
FLEX_ALIASES = {
    "W/R/T": {"RB", "WR", "TE"},
    "W/R": {"RB", "WR"},
    "R/W/T": {"RB", "WR", "TE"},
    "Q/W/R/T": {"QB", "RB", "WR", "TE"},
    "FLEX": {"RB", "WR", "TE"},
    "SUPER_FLEX": {"QB", "RB", "WR", "TE"},
}


@dataclass(frozen=True)
class Player:
    name: str
    position: str
    proj: float
    floor: float = 0.0
    ceiling: float = 0.0
    locked_out: bool = False  # bye / injured / suspended


def eligible(pos: str, slot: str) -> bool:
    slot = slot.upper()
    if slot == pos:
        return True
    return pos in FLEX_ALIASES.get(slot, set())


def optimize(players: list[Player], rules: scoring.ScoringRules, objective: str = "proj") -> dict:
    """Return {slot_index: Player} maximising summed projection under roster constraints."""
    slots = []
    for s in rules.starting_slots:
        slots.extend([(f"{s.position}#{i+1}", s.position) for i in range(s.count)])

    prob = pulp.LpProblem("lineup", pulp.LpMaximize)
    x = {
        (p.name, sid): pulp.LpVariable(f"x_{i}_{j}", cat="Binary")
        for i, p in enumerate(players)
        for j, (sid, spos) in enumerate(slots)
        if eligible(p.position, spos) and not p.locked_out
    }
    val = {"proj": lambda p: p.proj, "floor": lambda p: p.floor, "ceiling": lambda p: p.ceiling}[objective]
    prob += pulp.lpSum(val(p) * x[(p.name, sid)] for (pn, sid) in x for p in players if p.name == pn)

    for sid, _ in slots:
        prob += pulp.lpSum(x[(pn, s)] for (pn, s) in x if s == sid) <= 1
    for p in players:
        prob += pulp.lpSum(x[(pn, s)] for (pn, s) in x if pn == p.name) <= 1

    prob.solve(pulp.PULP_CBC_CMD(msg=0))
    by_name = {p.name: p for p in players}
    chosen = {sid: by_name[pn] for (pn, sid) in x if x[(pn, sid)].value() == 1}
    return chosen


def start_sit_table(players: list[Player], rules: scoring.ScoringRules) -> pl.DataFrame:
    """Recommended starters + the bench player closest to bumping each starter."""
    best = optimize(players, rules, "proj")
    starters = set(best.values())
    bench = [p for p in players if p not in starters and not p.locked_out]
    rows = []
    for sid, p in best.items():
        challengers = sorted(
            (b for b in bench if eligible(b.position, sid.split("#")[0])),
            key=lambda b: -b.proj,
        )
        top = challengers[0] if challengers else None
        rows.append(
            {
                "slot": sid,
                "starter": p.name,
                "proj": round(p.proj, 2),
                "floor": round(p.floor, 2),
                "ceiling": round(p.ceiling, 2),
                "next_best": top.name if top else "",
                "gap": round(p.proj - top.proj, 2) if top else None,
            }
        )
    return pl.DataFrame(rows)
