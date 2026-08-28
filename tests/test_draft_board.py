"""Parsing the pasted Yahoo draft-results panel + resolving names to the board."""

from __future__ import annotations

import polars as pl

from league_engine.analysis import draft_board as db

SAMPLE = """
3
J. Allen
J. Allen
QB
Buf
Bye 7
Team 3
2
B. Robinson
B. Robinson
RB
Atl
Bye 11
Team 2
1
J. Gibbs
J. Gibbs
RB
Det
Bye 8
Your Team
"""

BOARD = pl.DataFrame(
    {
        "name": ["Josh Allen", "Bijan Robinson", "Jahmyr Gibbs", "James Cook III", "Amon-Ra St. Brown"],
        "position": ["QB", "RB", "RB", "RB", "WR"],
    }
)


def test_parse_board_order_and_owner():
    picks = db.parse_board(SAMPLE)
    assert [p.pick for p in picks] == [1, 2, 3]
    assert picks[0].display == "J. Gibbs"
    assert picks[0].position == "RB"
    assert picks[0].is_mine is True
    assert picks[1].is_mine is False


def test_resolve_handles_suffix_and_compound_surnames():
    picks = db.parse_board(
        SAMPLE
        + "\n7\nJ. Cook III\nJ. Cook III\nRB\nBuf\nBye 7\nTeam 7\n"
        + "26\nA. St. Brown\nA. St. Brown\nWR\nDet\nBye 6\nTeam 1\n"
    )
    res = db.resolve(picks, BOARD)
    got = dict(zip(res["display"], res["matched_name"], strict=True))
    assert got["J. Gibbs"] == "Jahmyr Gibbs"
    assert got["J. Cook III"] == "James Cook III"
    assert got["A. St. Brown"] == "Amon-Ra St. Brown"
    assert res["matched"].all()
