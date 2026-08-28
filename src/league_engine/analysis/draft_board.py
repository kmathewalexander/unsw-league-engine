"""Parse a pasted Yahoo draft-room board and resolve names to the value board.

Yahoo's "Draft Results" panel copies as repeating blocks, newest pick first::

    8
    D. Maye
    D. Maye
    QB
    NE
    Bye 11
    Your Team

We parse (pick, first_initial, last_name, position, team, is_mine) and match each
to a canonical player on the value board by (last name, first initial, position),
tolerating name suffixes (Jr/Sr/II/III) and team-abbreviation differences.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import polars as pl

from .. import store

_SUFFIX = re.compile(r"\b(jr|sr|ii|iii|iv|v)\b\.?", re.IGNORECASE)
_POS = {"QB", "RB", "WR", "TE", "K", "DEF", "DST"}
# Yahoo team codes -> nflverse-ish; only the ones that differ in case/spelling matter,
# and we compare case-insensitively anyway, so this is just for display.
_MINE_LABELS = {"your team", "my team"}


@dataclass
class Pick:
    pick: int
    initial: str          # "D"
    last: str             # "maye"  (normalised, suffix-stripped)
    display: str          # "D. Maye"
    position: str
    team: str
    is_mine: bool


def _norm_last(name: str) -> str:
    """Normalise a surname fragment (already first-name-free): strip suffix + punctuation."""
    name = _SUFFIX.sub("", name)
    return re.sub(r"[^a-z]", "", name.lower())


def _canon_last(full_name: str) -> str:
    """Surname key for a full 'First Last[ Jr]' name: drop the first token, then normalise."""
    parts = full_name.split()
    rest = " ".join(parts[1:]) if len(parts) > 1 else full_name
    return _norm_last(rest)


def parse_board(text: str) -> list[Pick]:
    """Parse pasted Yahoo draft board text. Order in the result is by pick number."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    picks: list[Pick] = []
    i = 0
    n = len(lines)
    while i < n:
        # a pick block starts with a bare integer (the pick number)
        if not re.fullmatch(r"\d{1,3}", lines[i]):
            i += 1
            continue
        pick_no = int(lines[i])
        # find the position line within the next ~6 lines
        j = i + 1
        pos_idx = None
        while j < min(i + 8, n):
            if lines[j].upper() in _POS:
                pos_idx = j
                break
            j += 1
        if pos_idx is None:
            i += 1
            continue
        # name is the line just before the position (Yahoo repeats it; take the last repeat)
        name_line = lines[pos_idx - 1]
        # sometimes an injury tag ("Q") sits between name and position
        if name_line.upper() in {"Q", "O", "D", "IR", "SUS", "PUP"} and pos_idx - 2 > i:
            name_line = lines[pos_idx - 2]
        m = re.match(r"([A-Za-z])\.?\s+(.+)", name_line)
        if not m:
            i = pos_idx + 1
            continue
        initial, last_raw = m.group(1), m.group(2)
        team = lines[pos_idx + 1] if pos_idx + 1 < n else ""
        # owner label: first line after "Bye N" (or 2 lines after team)
        owner = ""
        for k in range(pos_idx + 1, min(pos_idx + 6, n)):
            if lines[k].lower().startswith("bye"):
                owner = lines[k + 1] if k + 1 < n else ""
                break
        picks.append(
            Pick(
                pick=pick_no,
                initial=initial.upper(),
                last=_norm_last(last_raw),
                display=f"{initial}. {last_raw}",
                position=lines[pos_idx].upper(),
                team=team.upper(),
                is_mine=owner.strip().lower() in _MINE_LABELS,
            )
        )
        i = pos_idx + 1

    picks.sort(key=lambda p: p.pick)
    return picks


def resolve(picks: list[Pick], board: pl.DataFrame | None = None) -> pl.DataFrame:
    """Match parsed picks to value-board rows.

    Returns a frame: pick, display, position, team, is_mine, matched_name, matched (bool).
    """
    if board is None:
        board = store.read("derived", "value_board")
    b = board.with_columns(
        pl.col("name").map_elements(_canon_last, return_dtype=pl.Utf8).alias("_last"),
        pl.col("name").str.slice(0, 1).str.to_uppercase().alias("_init"),
    )
    rows = []
    for p in picks:
        cand = b.filter(
            (pl.col("_last") == p.last)
            & (pl.col("_init") == p.initial)
            & (pl.col("position") == p.position)
        )
        if cand.height == 0:  # relax position (Yahoo QB in SF, board QB — usually fine, but WR/RB slot quirks)
            cand = b.filter((pl.col("_last") == p.last) & (pl.col("_init") == p.initial))
        matched = cand["name"][0] if cand.height else None
        rows.append(
            {
                "pick": p.pick,
                "display": p.display,
                "position": p.position,
                "team": p.team,
                "is_mine": p.is_mine,
                "matched_name": matched,
                "matched": matched is not None,
            }
        )
    return pl.DataFrame(rows)
