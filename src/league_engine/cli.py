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
def waivers(week: int = typer.Option(...), top: int = typer.Option(25)):
    """Free agents ranked by marginal lineup gain, with usage / luck / trend evidence."""
    from .analysis import review as rv

    pt = rv.player_table()
    table = rv.waiver_table(load_settings().league.team_name, pt, top_n=top)
    report.print_df(table, f"Waiver targets — week {week}", max_rows=top)
    report.save(f"waivers-week{week}", [("Targets", report.df_to_md(table, 100))])


@app.command()
def review(week: int = typer.Option(3, help="First week still to be simulated")):
    """League power rankings, all-play luck check, slot strength and playoff odds."""
    from .analysis import review as rv

    pt = rv.player_table()
    teams, lineups, unmatched = rv.team_table(pt)
    power = rv.power_table(teams, rv.load_standings())
    odds = rv.odds_table(power, first_week=week)
    table = power.join(odds, on="team", how="left").sort("power_rank")
    show = table.select(
        "power_rank", "team", "wins", "losses", "proj_week", "vs_avg", "allplay_pct",
        "playoff_odds", "avg_wins", "avg_seed",
    ).with_columns(pl.col(pl.Float64).round(2))
    report.print_df(show, "League review", max_rows=13)
    me = load_settings().league.team_name
    mine = pl.DataFrame(
        [{"slot": s, "player": p.name, "forecast": round(p.proj, 1)} for s, p in lineups[me].items()]
    )
    report.print_df(mine, f"{me}: optimal lineup")
    secs = [("League", report.df_to_md(show)), ("My lineup", report.df_to_md(mine))]
    if unmatched.height:
        report.print_df(unmatched, "Roster names not matched to nflverse")
        secs.append(("Unmatched", report.df_to_md(unmatched)))
    report.save("review", secs)


@app.command()
def player(names: list[str] = typer.Argument(...)):
    """Evidence profile for one or more players (usage, luck, trend, forecast, marginal gain)."""
    from .analysis import review as rv

    pt = rv.player_table()
    report.print_df(rv.profile(names, pt), "Player profile")
    wt = rv.waiver_table(load_settings().league.team_name, pt, top_n=500)
    keys = {rv._nk(n) for n in names}
    hit = wt.filter(pl.col("name").map_elements(rv._nk, return_dtype=pl.Utf8).is_in(list(keys)))
    if hit.height:
        report.print_df(hit.select("name", "lineup_gain", "replaces"), "Marginal gain for my lineup if added")


@app.command()
def track(week: int = typer.Option(None, help="Week label for the snapshot (default: upcoming NFL week)")):
    """Snapshot rosters, scoring rules, waiver wire and standings for the week (data/raw/league)."""
    from . import tracking

    res = tracking.track_all(week)
    console.print(res)


@app.command()
def trend(week: int = typer.Option(None), top: int = typer.Option(15)):
    """Weekly trend analysis: scoring x form x scheme fit x schedule difficulty."""
    from . import tracking
    from .analysis import trend as tr

    week = week or tracking.current_week()
    t = tr.build(week)
    v = tr.league_view(week, t)
    cols = ["name", "position", "team", "forecast", "z_scoring", "z_form", "z_scheme", "z_schedule",
            "trend_score", "trend_delta", "next_opps", "drivers", "signal"]
    fmt = lambda d: d.select([c for c in cols if c in d.columns]).with_columns(pl.col(pl.Float64).round(2))  # noqa: E731
    report.print_df(fmt(v["mine"]), f"My roster trend — week {week}", max_rows=20)
    report.print_df(fmt(v["free_agents"]), "Free-agent trend risers", max_rows=top)
    report.print_df(v["teams"].with_columns(pl.col(pl.Float64).round(2)), "Team trend (top-8 by trend)", max_rows=13)
    report.save(f"trend-week{week}", [
        ("My roster", report.df_to_md(fmt(v["mine"]), 30)),
        ("Free agents", report.df_to_md(fmt(v["free_agents"]), 30)),
        ("Teams", report.df_to_md(v["teams"].with_columns(pl.col(pl.Float64).round(2)), 13)),
    ])


@app.command()
def weekly(week: int = typer.Option(None), skip_sync: bool = typer.Option(False)):
    """Full weekly cycle: sync -> track -> trend -> review -> waivers."""
    from . import tracking

    if not skip_sync:
        sync(full=False, skip_yahoo=True, skip_fantasypros=True, week="draft")
    wk = week or tracking.current_week()
    track(week=wk)
    trend(week=wk, top=15)
    review(week=wk)
    waivers(week=wk, top=20)


@app.command("draft-review")
def draft_review():
    """Draft prep review: manager tendencies + who appears to do their research (2025-26 drafts)."""
    from .analysis import draft_review as dr

    picks = dr.build_picks()
    prof = dr.manager_profiles(picks)
    r2 = lambda d: d.with_columns(pl.col(pl.Float64).round(2))  # noqa: E731
    cols = ["franchise", "style", "consensus_gap", "deviation_edge", "hit_rate", "bust_rate", "surplus_vs_mkt",
            "research_signal", "moves"]
    report.print_df(r2(prof.select([c for c in cols if c in prof.columns])), "Who does their research (proxy)", 13)
    tcols = ["franchise", "first_qb_rd", "qb_r1_3", "rb_r1_5", "wr_r1_5", "first_te_rd", "k_rd", "def_rd", "rookie_share"]
    report.print_df(r2(prof.select(tcols)), "Drafting tendencies", 13)
    t = dr.tendencies(picks)
    report.print_df(r2(t["league_position_reach"]), "League habit vs market (pick - market rank; - = reached)", 20)
    report.print_df(r2(t["where_value_was"]), "Where value was (2025, places vs pick slot)", 30)
    report.save("draft-review", [
        ("Research proxy", report.df_to_md(r2(prof.select([c for c in cols if c in prof.columns])), 13)),
        ("Tendencies", report.df_to_md(r2(prof.select(tcols)), 13)),
        ("League position habit", report.df_to_md(r2(t["league_position_reach"]), 20)),
        ("Where value was", report.df_to_md(r2(t["where_value_was"]), 40)),
        ("Position by round", report.df_to_md(t["pos_by_round"], 40)),
        ("Homer stacks", report.df_to_md(t["homer_stacks"], 20)),
    ])


@app.command()
def trades():
    """Ranked trade proposals with rest-of-season impact."""
    console.print("[yellow]trades: requires league rosters snapshot (Yahoo roster endpoints).[/yellow]")
    console.print("Wire `_load_all_rosters()` then call analysis.trades.find_trades().")


@app.command()
def standings(week: int = typer.Option(3, help="First week still to be simulated")):
    """Monte Carlo playoff odds (alias of the odds columns in `review`)."""
    review(week=week)


@app.command()
def brief(week: int = typer.Option(...)):
    """Combined weekly report: lineup + waivers + standings."""
    lineup(week=week)
    waivers(week=week)
    console.print("[green]brief complete[/green] (standings section pending schedule wiring)")


def _load_my_roster():
    import polars as pl

    settings = load_settings()
    league_csv = settings.root / "config" / "league_rosters.csv"
    if league_csv.exists():
        from .analysis import review as _rv

        pt = _rv.player_table()
        ros = _rv._attach(_rv.load_rosters().filter(pl.col("team") == settings.league.team_name), pt)
        return ros.filter(pl.col("position").is_not_null()).select("name", "position")
    if store.exists("derived", "my_roster"):
        return store.read("derived", "my_roster")
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
