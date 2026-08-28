"""Playbook CLI. Each command is a repeatable procedure documented in docs/playbooks/."""

from __future__ import annotations

import typer
from rich.console import Console

from . import crosswalk, projections, report, research, scoring, store
from .config import load_settings
from .ingest import fantasypros as fp_ingest
from .ingest import nfl as nfl_ingest
from .ingest import yahoo as yahoo_ingest

app = typer.Typer(add_completion=False, help="UNSW League analysis engine")
console = Console()


@app.command()
def sync(
    full: bool = typer.Option(False, help="Re-pull all history and force-refresh every dataset."),
    skip_yahoo: bool = typer.Option(False, help="Skip Yahoo (use existing snapshots)."),
    skip_fantasypros: bool = typer.Option(False),
    week: str = typer.Option("draft", help="Weekly FantasyPros projections: 'draft' or a week number."),
):
    """Refresh all data sources into data/."""
    settings = load_settings()

    manual_scoring = scoring.manual_config_path().exists()
    if not skip_yahoo:
        console.rule("Yahoo")
        try:
            history = yahoo_ingest.sync(full=full)
            console.print(f"Seasons discovered: {sorted(history)}")
        except Exception as exc:  # noqa: BLE001
            console.print(f"[yellow]Yahoo pull skipped: {exc}[/yellow]")
            if not manual_scoring:
                console.print(
                    "[yellow]No Yahoo data and no config/scoring.toml — the engine needs one "
                    "of them. See docs/playbooks/sync.md.[/yellow]"
                )

    if settings.sources.use_nflverse:
        console.rule("nflverse")
        nfl_ingest.sync(settings.league.current_season, full=full)

    if settings.sources.use_fantasypros and not skip_fantasypros:
        console.rule("FantasyPros")
        wk = "draft" if week == "draft" else int(week)
        fp_ingest.sync(scoring="PPR", week=wk)

    console.rule("derived tables")
    crosswalk.build()
    rules = scoring.load_rules(settings.league.current_season)
    console.print(rules.describe())
    scoring.build_points_table(settings.league.current_season)
    projections.season_projection()
    from .analysis import draft as _draft

    _draft.value_board(settings.league.teams)
    try:
        research.build()
        console.print("[green]research table built[/green]")
    except Exception as exc:  # noqa: BLE001
        console.print(f"[yellow]research build skipped: {exc}[/yellow]")
    console.print("[green]sync complete[/green]")


@app.command("scoring-check")
def scoring_check():
    """Print the parsed league scoring rules and flag anything unmapped."""
    settings = load_settings()
    rules = scoring.load_rules(settings.league.current_season)
    console.print(rules.describe())
    if rules.unmapped:
        console.print(
            "[yellow]Unmapped stats (scoring 0): "
            + ", ".join(rules.unmapped)
            + "\nUse a supported key (see config/scoring.toml.example) or add an "
            "expression in src/league_engine/scoring.py:MANUAL_KEYS.[/yellow]"
        )


@app.command("draft-prep")
def draft_prep(teams: int = typer.Option(None)):
    """Pre-draft report: league tendencies, manager profiles, value board, targets."""
    from .analysis import draft

    teams = teams or load_settings().league.teams
    projections.season_projection()
    draft.value_board(teams)
    research.build()
    board = research.enriched().sort("vor", descending=True, nulls_last=True)
    profiles = draft.manager_profiles()
    runs = draft.positional_runs()

    view = [
        c for c in ("name", "position", "team", "tier", "vor", "adp", "value_vs_adp",
                    "usage_grade", "xfp_pg", "fp_oe_pg", "team_total", "win_total",
                    "sos_playoffs", "age", "rookie", "flag")
        if c in board.columns
    ]
    report.print_df(board.select(view).head(30), "Value board (top 30 by VOR)")

    # unsustainable profile: modest expected volume, big actual production, no injury excuse
    fades = board.filter(
        (pl.col("fp_oe_pg") > 3.0) & (pl.col("xfp_pg") < 14) & pl.col("flag").is_null()
        & pl.col("adp").is_not_null() & (pl.col("adp") <= 100)
    ).select("name", "position", "adp", "fp_oe_pg", "xfp_pg", "team_total")
    # RB/WR/TE going behind the board with strong real usage (QB value is covered elsewhere)
    sleepers = board.filter(
        pl.col("position").is_in(["RB", "WR", "TE"])
        & pl.col("usage_grade").is_in(["A", "B"])
        & (pl.col("adp") > pl.col("overall_rank") + 12)
    ).select("name", "position", "adp", "overall_rank", "usage_grade", "team_total", "xfp_pg").head(20)
    flagged = board.filter(pl.col("flag").is_not_null() & (pl.col("overall_rank") <= 230)).select(
        "name", "position", "overall_rank", "flag"
    )

    sections = [
        ("Value board", report.df_to_md(board.select(view), 220)),
        ("Regression risks (rode TD luck in 2025, still going early)", report.df_to_md(fades)),
        ("Sleepers (strong usage, ADP behind the board)", report.df_to_md(sleepers)),
        ("Status flags to verify", report.df_to_md(flagged)),
        ("Manager profiles", report.df_to_md(profiles)),
        ("Positional run rates", report.df_to_md(runs)),
        (
            "Best values vs ADP",
            report.df_to_md(
                board.filter(pl.col("adp").is_not_null() & ~pl.col("position").is_in(["K", "DEF"]))
                .sort("value_vs_adp", descending=True, nulls_last=True)
                .select(view)
                .head(30)
            ),
        ),
    ]
    report.save("draft-prep", sections)


@app.command()
def preranks(
    teams: int = typer.Option(13),
    out: str = typer.Option("reports/preranks.csv", help="Output CSV path."),
    limit: int = typer.Option(250),
):
    """Export the value board as a Yahoo 'Edit Pre-Draft Ranks' import CSV.

    Columns: rank,name,team,position (Yahoo matches on name). Upload it at
    football.fantasysports.yahoo.com/f1/<league>/<team>/editprerank -> Import.
    """
    from pathlib import Path

    from .analysis import draft

    projections.season_projection()
    board = draft.value_board(teams).sort("vor", descending=True, nulls_last=True)
    main = board.filter(pl.col("position") != "DEF")
    ordered = pl.concat([main, board.filter(pl.col("position") == "DEF")]).head(limit)

    team_col = None
    if store.exists("raw/nflverse", "ff_rankings"):
        fr = (
            store.read("raw/nflverse", "ff_rankings")
            .filter(pl.col("page_type") == "redraft-overall")
            .select("player", "team")
            .unique(subset=["player"])
        )
        ordered = ordered.join(fr.rename({"player": "name"}), on="name", how="left")
        team_col = "team"

    lines = ["rank,name,team,position"]
    for i, r in enumerate(ordered.iter_rows(named=True), 1):
        tm = (r.get(team_col) or "") if team_col else ""
        lines.append(f"{i},{r['name']},{tm},{r['position']}")
    path = Path(load_settings().root / out)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines))
    console.print(f"[green]wrote[/green] {path}  ({len(lines) - 1} players)")
    report.print_df(ordered.select("name", "position", "vor", "tier").head(20), "Top 20")


@app.command("draft-live")
def draft_live(teams: int = typer.Option(12), picks_until_next: int = typer.Option(12)):
    """Interactive draft assistant. Enter picks as 'name' lines; blank line = recommend."""
    from .analysis import draft

    if not store.exists("derived", "value_board"):
        projections.season_projection()
        draft.value_board(teams)

    drafted: list[str] = []
    mine: list[str] = []
    console.print("Commands: '<player>' drafted by someone | 'me <player>' my pick | '' recommend | 'q' quit")
    while True:
        line = console.input("[cyan]draft> [/cyan]").strip()
        if line == "q":
            break
        if line == "":
            rec = draft.recommend_pick(drafted, mine, picks_until_next, teams)
            report.print_df(rec, "Recommended picks")
            continue
        if line.lower().startswith("me "):
            name = line[3:].strip()
            mine.append(name)
            drafted.append(name)
        else:
            drafted.append(line)
        console.print(f"  drafted={len(drafted)} mine={len(mine)}")


@app.command("draft-board")
def draft_board(
    board: str = typer.Option(..., help="Path to a text file with the pasted Yahoo draft-results panel."),
    teams: int = typer.Option(13),
    until: int = typer.Option(
        None, help="Picks until your next turn. Auto-computed from the snake if omitted."
    ),
    brief: bool = typer.Option(
        False, "--brief", help="Compact plain-text output: one line per candidate, no rich table."
    ),
):
    """Parse a pasted Yahoo draft board and recommend your next pick."""
    from pathlib import Path

    from .analysis import draft
    from .analysis import draft_board as db

    if not store.exists("derived", "value_board"):
        projections.season_projection()
        draft.value_board(teams)
    if not store.exists("derived", "research"):
        research.build()
    vb = store.read("derived", "value_board")

    text = Path(board).read_text()
    picks = db.parse_board(text)
    resolved = db.resolve(picks, vb)

    unmatched = resolved.filter(~pl.col("matched"))
    if unmatched.height:
        console.print("[yellow]Could not match these picks (ignored):[/yellow]")
        report.print_df(unmatched.select("pick", "display", "position", "team"), "unmatched")

    drafted = resolved.filter(pl.col("matched"))["matched_name"].to_list()
    mine = resolved.filter(pl.col("matched") & pl.col("is_mine"))["matched_name"].to_list()
    last_pick = max((p.pick for p in picks), default=0)

    if until is None:
        # snake: seat from my earliest pick; urgency uses the gap AFTER my imminent pick
        my_picks = sorted(resolved.filter(pl.col("is_mine"))["pick"].to_list())
        if my_picks:
            seat = my_picks[0]
            on_clock = _next_snake_pick(seat, teams, last_pick)      # my imminent pick
            after = _next_snake_pick(seat, teams, on_clock)          # the one after that
            until = max(0, after - on_clock - 1)
            console.print(
                f"Seat {seat}/{teams}. You're on the clock at #{on_clock}; "
                f"next turn #{after} ({until} picks away)."
            )
        else:
            until = teams
    console.print(f"[dim]{len(drafted)} players off the board, you have {len(mine)}: "
                  f"{', '.join(mine) or '(none)'}[/dim]")

    rec = draft.recommend_pick(drafted, mine, until, teams)
    enr = research.enriched()
    cols = [c for c in ("name", "position", "flag", "usage_grade", "fp_oe_pg", "team_total",
                        "win_total", "sos_playoffs", "age", "rookie") if c in enr.columns]
    rec = rec.join(enr.select(cols), on=["name", "position"], how="left")
    rec = rec.with_columns(
        pl.struct(rec.columns).map_elements(research.notes_for, return_dtype=pl.Utf8).alias("notes")
    )

    if brief:
        have = {}
        for p in mine:
            row = vb.filter(pl.col("name") == p)
            if row.height:
                have[row["position"][0]] = have.get(row["position"][0], 0) + 1
        roster = " ".join(f"{k}{v}" for k, v in sorted(have.items())) or "empty"
        console.print(f"#{last_pick + 1} you pick | roster: {roster} | {until} until next turn")
        for r in rec.head(8).iter_rows(named=True):
            console.print(
                f"  {r['name']:<22} {r['position']:<3} "
                f"VOR {r['vor']:>5.0f}  ADP {r['adp'] or 0:>3.0f}  T{r['tier']}  "
                f"{(r['notes'] or '').replace(chr(10), ' ')}"
            )
        return

    report.print_df(
        rec.select("name", "position", "vor", "adp", "tier", "pick_score", "notes"),
        f"Recommended pick (picks until your turn: {until})",
    )


def _next_snake_pick(seat: int, teams: int, last_pick: int) -> int:
    """Next overall pick number for a given 1-indexed snake seat after last_pick."""
    p = 0
    rnd = 0
    while p <= last_pick + teams * 2:
        rnd += 1
        pick = (rnd - 1) * teams + (seat if rnd % 2 == 1 else teams - seat + 1)
        if pick > last_pick:
            return pick
        p = pick
    return last_pick + 1


@app.command()
def lineup(week: int = typer.Option(..., help="NFL week number")):
    """Optimal lineup + start/sit calls for your team."""
    from .analysis import lineup as lu

    settings = load_settings()
    proj = projections.weekly_projection(week)
    rules = scoring.load_rules(settings.league.current_season)

    # NOTE: needs your roster. Reads data/derived/my_roster.parquet (name,position)
    # produced by `sync` from Yahoo once roster endpoints are wired; until then,
    # falls back to config/my_roster.csv.
    roster = _load_my_roster()
    players = []
    pjoin = proj.select("name", "mean", "floor", "ceiling")
    d = {r["name"]: r for r in pjoin.to_dicts()}
    for row in roster.iter_rows(named=True):
        p = d.get(row["name"], {})
        players.append(
            lu.Player(
                name=row["name"],
                position=row["position"],
                proj=float(p.get("mean") or 0.0),
                floor=float(p.get("floor") or 0.0),
                ceiling=float(p.get("ceiling") or 0.0),
                locked_out=bool(row.get("out", False)),
            )
        )
    table = lu.start_sit_table(players, rules)
    report.print_df(table, f"Optimal lineup — week {week}")
    report.save(f"lineup-week{week}", [("Lineup", report.df_to_md(table))])


@app.command()
def waivers(week: int = typer.Option(...)):
    """Ranked waiver targets, drops and FAAB bids."""
    from .analysis import waivers as wv

    projections.weekly_projection(week)
    roster = _load_my_roster()
    proj = store.read("derived", f"proj_week_{week}")
    mine = proj.join(roster, on="name", how="semi")
    baseline = (
        mine.group_by("position").agg(pl.col("mean").min().alias("worst")).to_dict(as_series=False)
        if mine.height
        else {"position": [], "worst": []}
    )
    base = dict(zip(baseline["position"], baseline["worst"], strict=False))
    table = wv.candidates(week, base)
    report.print_df(table, f"Waiver targets — week {week}")
    report.save(f"waivers-week{week}", [("Targets", report.df_to_md(table, 100))])


@app.command()
def trades():
    """Ranked trade proposals with rest-of-season impact."""
    console.print("[yellow]trades: requires league rosters snapshot (Yahoo roster endpoints).[/yellow]")
    console.print("Wire `_load_all_rosters()` then call analysis.trades.find_trades().")


@app.command()
def standings():
    """Monte Carlo playoff odds + projected finish."""
    console.print("[yellow]standings: requires current standings + remaining schedule snapshot.[/yellow]")
    console.print("Populate from Yahoo matchups_by_week + league_standings, then analysis.standings.simulate().")


@app.command()
def brief(week: int = typer.Option(...)):
    """Combined weekly report: lineup + waivers + standings."""
    lineup(week=week)
    waivers(week=week)
    console.print("[green]brief complete[/green] (standings section pending schedule wiring)")


def _load_my_roster():
    import polars as pl

    if store.exists("derived", "my_roster"):
        return store.read("derived", "my_roster")
    settings = load_settings()
    csv = settings.root / "config" / "my_roster.csv"
    if csv.exists():
        return pl.read_csv(csv)
    raise FileNotFoundError(
        "No roster found. Create config/my_roster.csv with columns name,position[,out] "
        "or run `sync` after roster endpoints are wired."
    )


import polars as pl  # noqa: E402  (used by helpers above)

if __name__ == "__main__":
    app()
