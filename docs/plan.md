# UNSW League Analysis Engine — Implementation Plan

## Context

The user runs a Yahoo NFL fantasy team in a custom-scoring league ("UNSW League") and wants a
semi-automated analysis engine that helps them (a) draft, (b) set optimal lineups, (c) find waiver
targets, (d) evaluate trades, and (e) project their season standing. All advice must be grounded in
**this league's actual scoring rules** and **its historical draft/manager behaviour**, not generic
fantasy defaults.

The 2026 draft has **not happened yet**, so draft tooling is the immediate priority; lineup/waiver
tooling is next for Week 1 (NFL season opens ~Sep 4 2026). The project lives in
`Development/UNSW League/` and will be git-initialised there.

Delivery model: a Python project with a set of **CLI "playbooks"** — each playbook is one repeatable
command that produces a Markdown report, backed by a documented decision procedure in `docs/playbooks/`.

## Toolchain & setup

- Install **uv** (`curl -LsSf https://astral.sh/uv/install.sh | sh`) — self-contained, no admin.
- `uv python install 3.12`; project pinned to 3.12 via `.python-version` + `pyproject.toml`.
- Dependencies (via `uv add`):
  - `yfpy` — Yahoo Fantasy Sports API wrapper (OAuth2, league settings, drafts, rosters, matchups, transactions, ownership).
  - `nflreadpy` — nflverse data (weekly player stats, snap counts, depth charts, schedules, injuries, PBP).
  - `pandas`, `polars` (nflreadpy returns polars), `numpy`, `scipy`.
  - `pulp` — ILP solver for optimal-lineup constraint problem.
  - `httpx` + `selectolax` (or `beautifulsoup4`) — FantasyPros consensus/ECR + weekly projection scraping.
  - `typer` + `rich` — CLI + terminal report rendering.
  - `pydantic` + `tomli` — config models.
  - `duckdb` — local analytical store over Parquet files.
  - dev: `pytest`, `ruff`.
- Secrets in `.env` (gitignored): `YAHOO_CONSUMER_KEY`, `YAHOO_CONSUMER_SECRET`. Yahoo OAuth token
  cached to `private/oauth2.json` (gitignored). The user must first create a Yahoo developer app at
  https://developer.yahoo.com/apps/create/ (Fantasy Sports → Read scope) and paste key/secret into `.env`.

## Repository layout

```
UNSW League/
  pyproject.toml, .python-version, .env.example, .gitignore, README.md
  config/
    league.toml            # league_key(s), team name, FAAB budget, roster slots, timezone, source toggles
  src/league_engine/
    config.py              # load config/league.toml + .env into pydantic models
    ingest/
      yahoo.py             # yfpy client wrapper + season/league-key auto-discovery
      nfl.py               # nflreadpy pulls, cached to Parquet
      fantasypros.py       # scrape ECR + weekly projections
    store.py               # DuckDB/Parquet read+write helpers, snapshot versioning
    scoring.py             # parse Yahoo scoring settings -> scoring function; apply to nflverse stat lines
    projections.py         # blended projection model -> mean/floor/ceiling in LEAGUE points
    analysis/
      draft.py             # historical draft tendencies + VOR/VONA board + live pick assist
      lineup.py            # ILP optimal lineup + start/sit deltas
      waivers.py           # availability x projected-points-added x schedule -> ranked targets + FAAB bid
      trades.py            # roster-need detection, value surplus, trade finder, ROS impact
      standings.py         # Monte Carlo rest-of-season simulation
    report.py              # Markdown/rich report builders
    cli.py                 # typer app: the playbooks
  docs/playbooks/          # one .md per playbook: inputs, procedure, thresholds, how to read output
  data/                    # gitignored Parquet cache (raw + derived)
  reports/                 # gitignored generated Markdown reports
  private/                 # gitignored OAuth token
  tests/
```

## Core components

### 1. Yahoo ingest (`ingest/yahoo.py`)
- Wrap `yfpy.YahooFantasySportsQuery`. One-time interactive OAuth handshake; token reused after.
- **Season auto-discovery**: use `get_user_leagues_by_game_key()` across NFL game keys to enumerate
  every prior season of this league the account can see; resolve each season's `league_key`. Store the
  chain in `config/league.toml` after first run so it's inspectable.
- Pull & snapshot to Parquet: `get_league_settings` (scoring + roster), `get_league_draft_results`
  (all seasons), `get_league_transactions` (all seasons), `get_league_matchups_by_week` /
  `get_league_scoreboard_by_week`, `get_team_roster_by_week`, `get_player_ownership` /
  `get_player_percent_owned_by_week`, `get_player_draft_analysis` (Yahoo ADP).
- Player ID crosswalk: map Yahoo player IDs ↔ nflverse `gsis_id`/`pfr_id` using nflreadpy's
  `load_ff_playerids()` / id-map table, with a manual override file for misses.

### 2. Scoring engine (`scoring.py`)
- Translate the league's Yahoo `stat_modifiers` (+ bonuses/thresholds if present) into a pure function
  `score(stat_line: dict) -> float`.
- Apply it to nflverse weekly stat lines to produce **league-accurate historical fantasy points** for
  every player-week — this is the backbone all analysis uses. Validate by reconciling a few known
  historical player-weeks against Yahoo's own reported points (assert within rounding).
- Config-driven so non-standard rules (e.g. TE premium, 6pt pass TD, first-down points, return yards,
  IDP) are just table entries, never hardcoded.

### 3. Projections (`projections.py`)
- Weekly projection per player = blend of:
  - FantasyPros consensus projection (scraped), converted into league points via `scoring.py`.
  - Usage-trend model: rolling snap %, route %, target share, carry share, red-zone touches from
    nflreadpy → expected volume → league points.
  - Matchup adjustment: opponent defense points-allowed vs position (from our own historical-points table).
  - Game-environment adjustment: Vegas implied team total / spread (from nflreadpy schedules + lines).
- Output `mean`, `floor` (~20th pct), `ceiling` (~80th pct) via historical residual variance by position.
- Preseason/draft mode: fall back to season-long projections + prior-year league points + ADP when no
  current-season usage exists yet.

### 4. Analysis modules — each backs one playbook

- **draft.py**
  - Historical tendencies: per-manager positional-pick timing, reach vs Yahoo ADP, position runs,
    bye/stack behaviour, keeper patterns — from `draft_results` across all discovered seasons.
  - Value board: VOR / VONA / tiered rankings in league points using replacement levels derived from
    this league's roster settings and starter demand.
  - Live assist: given picks so far (entered manually or pasted), recommend best pick = value over
    next expected availability, adjusted for the user's current roster construction and the modelled
    tendencies of managers picking before their next turn.

- **lineup.py**
  - ILP (`pulp`): maximise projected league points subject to exact roster-slot constraints
    (QB/RB/WR/TE/FLEX/superflex/K/DEF as configured), respecting locked/injured/bye players.
  - Report: recommended starters, bench points left, start/sit swap deltas, "close calls" flagged with
    floor/ceiling and matchup notes.

- **waivers.py**
  - Candidate pool: players below an ownership threshold from Yahoo ownership data OR not on any roster
    (from league rosters).
  - Rank by projected-points-added vs the user's current worst startable option at that position, over
    the next 1 and 4 weeks (schedule-aware).
  - Drop candidates from the user's roster; suggested FAAB bid as % of remaining budget scaled to
    projected-points-added and positional scarcity; trending adds/drops as a secondary signal.

- **trades.py**
  - Detect roster surplus/deficit by comparing starter projections to league-average starter
    projections by slot.
  - For each other team, find 1-for-1 / 2-for-1 packages that improve the user's projected starting
    lineup ROS while being plausibly acceptable to the counterparty (improves their weakest slot too).
  - Report ranked trade ideas with ROS points delta for both sides.

- **standings.py**
  - Monte Carlo (N≈10k): simulate every remaining week using player projections → team weekly score
    distributions → win/loss, tiebreakers, playoff seeding.
  - Report: playoff odds, projected final record, expected seed, and the swing games / weeks that
    matter most.

### 5. Playbooks (`cli.py` — `typer`)

| Command | What it does |
|---|---|
| `playbook sync` | Refresh all Yahoo + nflverse + FantasyPros data into `data/` (run before others; `--full` re-pulls history) |
| `playbook draft-prep` | Pre-draft report: league draft tendencies, manager profiles, value board, target list by round |
| `playbook draft-live` | Interactive: enter picks as they happen, get a recommended pick each turn |
| `playbook lineup [--week N]` | Optimal lineup + start/sit calls for the user's team |
| `playbook waivers [--week N]` | Ranked waiver targets, drop candidates, FAAB bids |
| `playbook trades` | Ranked trade proposals with ROS impact |
| `playbook standings` | Monte Carlo playoff odds + projected finish |
| `playbook brief [--week N]` | One combined weekly report: lineup + waivers + standings + key news |

Each writes a timestamped Markdown file to `reports/` and prints a `rich` summary. Every command has a
companion `docs/playbooks/<name>.md` documenting the exact procedure, data inputs, and thresholds so the
logic is auditable and tunable.

## Build phases

1. **Scaffold** — uv, `pyproject.toml`, layout, config models, `.gitignore`, git init, README with Yahoo-app setup steps.
2. **Yahoo ingest + scoring** — OAuth handshake, season auto-discovery, pull settings/drafts/transactions/matchups, build & validate `scoring.py` against Yahoo-reported points. *Milestone: `playbook sync` works; scoring reconciles.*
3. **nflverse + FantasyPros ingest + id crosswalk** — historical league-points table for all player-weeks.
4. **Projections** (draft/preseason mode first, since that's what's needed now).
5. **`draft-prep` + `draft-live` playbooks** — the immediate priority.
6. **`lineup` + `waivers` playbooks** — ready before Week 1.
7. **`standings` + `trades` + `brief`** — in-season depth.

## Verification

- `playbook sync` then inspect `data/` Parquet: league settings show the real custom scoring; draft
  results present for every season the API exposed.
- Scoring reconciliation test: `pytest tests/test_scoring.py` — recompute points for ~20 known
  historical player-weeks (from `get_team_roster_player_stats_by_week`) and assert equality with
  Yahoo's reported values within rounding.
- `playbook draft-prep` produces a report whose ADP/tendency numbers can be spot-checked against the
  raw Yahoo draft-results Parquet.
- `playbook lineup --week 1` on a past season: confirm the ILP picks the actual highest-scoring valid
  lineup in hindsight when fed realized points instead of projections (optimality check).
- `playbook standings`: run mid-past-season with realized results for completed weeks; sanity-check
  that a dominant team shows high playoff odds.
- `ruff check` + `pytest` clean.

## Open items / user follow-ups

- User creates a Yahoo developer app (Read scope) and adds `YAHOO_CONSUMER_KEY` / `SECRET` to `.env`.
- User provides the current-season Yahoo league URL or `league_id` for `config/league.toml` (initial seed).
- Confirm exact custom scoring after first `sync` (engine pulls it; user eyeballs the parsed table).
- FantasyPros scraping is best-effort against their public pages and may need selector maintenance; if
  it breaks, projections degrade gracefully to the usage + prior-year model.
