"""Playbook CLI. Each command is a repeatable procedure documented in docs/playbooks/."""

from __future__ import annotations

import typer
from rich.console import Console

from . import crosswalk, projections, report, scoring, store
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

    if not skip_yahoo:
        console.rule("Yahoo")
        history = yahoo_ingest.sync(full=full)
        console.print(f"Seasons discovered: {sorted(history)}")

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
    console.print("[green]sync complete[/green]")


@app.command("scoring-check")
def scoring_check():
    """Print the parsed league scoring rules and flag anything unmapped."""
    settings = load_settings()
    rules = scoring.load_rules(settings.league.current_season)
    console.print(rules.describe())
    if rules.unmapped:
        console.print(
            "[yellow]Add expressions for the unmapped stats in "
            "src/league_engine/scoring.py:STAT_EXPR[/yellow]"
        )


@app.command("draft-prep")
def draft_prep(teams: int = typer.Option(12)):
    """Pre-draft report: league tendencies, manager profiles, value board, targets."""
    from .analysis import draft

    projections.season_projection()
    board = draft.value_board(teams)
    profiles = draft.manager_profiles()
    runs = draft.positional_runs()

    report.print_df(board.head(30), "Value board (top 30 by VOR)")
    sections = [
        ("Value board", report.df_to_md(board, 200)),
        ("Manager profiles", report.df_to_md(profiles)),
        ("Positional run rates", report.df_to_md(runs)),
        (
            "Best values vs ADP",
            report.df_to_md(
                board.filter(pl.col("adp").is_not_null())
                .sort("value_vs_adp", descending=True, nulls_last=True)
                .head(30)
            ),
        ),
    ]
    report.save("draft-prep", sections)


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
