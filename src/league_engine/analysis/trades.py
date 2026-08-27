"""Trade finder: detect roster surplus/deficit and propose mutually-improving swaps."""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations

import polars as pl


@dataclass
class RosterPlayer:
    name: str
    position: str
    ros_points: float  # rest-of-season projected league points
    team_owner: str


def _starter_strength(players: list[RosterPlayer], slots: dict[str, int]) -> float:
    """Sum of the top-N ROS projections filling the starting slots (flex-aware, simple)."""
    by_pos: dict[str, list[float]] = {}
    for p in players:
        by_pos.setdefault(p.position, []).append(p.ros_points)
    for v in by_pos.values():
        v.sort(reverse=True)
    total = 0.0
    used = {k: 0 for k in by_pos}
    for pos, n in slots.items():
        if pos in {"FLEX", "W/R/T", "R/W/T"}:
            pool = sorted(
                ((val, pp) for pp in ("RB", "WR", "TE") for val in by_pos.get(pp, [])[used.get(pp, 0):]),
                reverse=True,
            )
            for val, pp in pool[:n]:
                total += val
                used[pp] = used.get(pp, 0) + 1
        else:
            vals = by_pos.get(pos, [])
            take = vals[used.get(pos, 0): used.get(pos, 0) + n]
            total += sum(take)
            used[pos] = used.get(pos, 0) + n
    return total


def find_trades(
    my_players: list[RosterPlayer],
    their_rosters: dict[str, list[RosterPlayer]],
    slots: dict[str, int],
    max_per_side: int = 2,
    top_n: int = 10,
) -> pl.DataFrame:
    """Enumerate small packages; keep trades that raise *both* sides' starter strength."""
    my_base = _starter_strength(my_players, slots)
    rows = []
    for owner, roster in their_rosters.items():
        their_base = _starter_strength(roster, slots)
        my_names = [p.name for p in my_players]
        their_names = [p.name for p in roster]
        for k in range(1, max_per_side + 1):
            for give in combinations(my_names, k):
                for get in combinations(their_names, k):
                    new_mine = [p for p in my_players if p.name not in give] + [
                        p for p in roster if p.name in get
                    ]
                    new_theirs = [p for p in roster if p.name not in get] + [
                        p for p in my_players if p.name in give
                    ]
                    d_mine = _starter_strength(new_mine, slots) - my_base
                    d_theirs = _starter_strength(new_theirs, slots) - their_base
                    if d_mine > 0.5 and d_theirs > 0.5:
                        rows.append(
                            {
                                "owner": owner,
                                "i_give": ", ".join(give),
                                "i_get": ", ".join(get),
                                "my_gain": round(d_mine, 1),
                                "their_gain": round(d_theirs, 1),
                                "fairness": round(min(d_mine, d_theirs) / max(d_mine, d_theirs), 2),
                            }
                        )
    if not rows:
        return pl.DataFrame()
    return (
        pl.DataFrame(rows)
        .with_columns((pl.col("my_gain") + pl.col("their_gain")).alias("total_gain"))
        .sort(["my_gain", "fairness"], descending=True)
        .head(top_n)
    )
