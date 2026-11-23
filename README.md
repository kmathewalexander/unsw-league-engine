# UNSW League Analysis Engine

Semi-automated analysis engine for a custom-scoring Yahoo NFL fantasy league. It ingests the
league's real scoring rules, draft history, rosters, matchups and transactions from the Yahoo
Fantasy Sports API, blends them with public nflverse data and FantasyPros consensus projections,
and runs a set of repeatable **playbooks** for drafting, lineup-setting, waivers, trades and
standings projection.

## Setup

### 1. Toolchain

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
uv sync                      # creates .venv with Python 3.12 and all deps
```

### 2. League scoring (required)

Yahoo retired self-service Fantasy Sports API access — it's now an application/approval process
([sports.yahoo.com/developer/access](https://sports.yahoo.com/developer/access/)). Until that's
approved, enter your league's scoring by hand:

```bash
cp config/scoring.toml.example config/scoring.toml
```

Edit `config/scoring.toml` to match your Yahoo **League → Settings** page (the scoring table and
the roster-positions table). It's a 10-minute one-time job and fully documented in the file.

Also set your league identity in `config/league.toml`: `current_league_id` (from your league URL
`.../f1/<id>`) and `team_name` (exact).

### 3. Yahoo API (optional, for full automation later)

Apply at [sports.yahoo.com/developer/access](https://sports.yahoo.com/developer/access/) citing
"personal / single league use". If approved, add the Client ID/Secret to `.env` (copy
`.env.example`) and the engine auto-pulls scoring, draft history, rosters and matchups — no more
manual config. `config/scoring.toml` always overrides the API pull if present.

### 4. First sync

```bash
uv run playbook sync            # add --skip-yahoo until API access is approved
```

`sync` pulls nflverse + FantasyPros data, applies your scoring to every player-week, and builds the
derived tables under `data/`. With Yahoo access it also does a one-time browser OAuth consent and
snapshots your league history.

The league history (weekly Yahoo snapshots under `data/raw/league/`, derived tables, `reports/` and
the `config/*.csv` inputs) is committed to git, so a fresh clone carries the full season. Only the
nflverse cache (`data/raw/nflverse/`) and `data/derived/league_points.parquet` are gitignored:
they're large, rewritten on every sync, and rebuilt by `playbook sync`. Secrets (`.env`, `private/`)
stay ignored.

## Playbooks

| Command | Purpose |
|---|---|
| `uv run playbook sync` | Refresh all Yahoo + nflverse + FantasyPros data |
| `uv run playbook draft-prep` | Pre-draft report: league tendencies, manager profiles, value board, targets |
| `uv run playbook draft-live` | Interactive draft assistant |
| `uv run playbook lineup --week N` | Optimal lineup + start/sit calls |
| `uv run playbook waivers --week N` | Ranked waiver targets, drops, FAAB bids |
| `uv run playbook review --week N` | Power rankings, all-play luck, playoff odds, optimal lineup ([review.md](docs/playbooks/review.md)) |
| `uv run playbook player NAME...` | Usage / luck / trend profile + marginal lineup gain |
| `uv run playbook track` | Weekly snapshot of rosters, scoring, waiver wire, standings |
| `uv run playbook trend` | Trend: scoring x form x scheme fit x schedule difficulty ([track-trend.md](docs/playbooks/track-trend.md)) |
| `uv run playbook draft-review` | Draft prep: manager tendencies + who does their research ([draft-review.md](docs/playbooks/draft-review.md)) |
| `uv run playbook weekly` | sync → track → trend → review → waivers |
| `uv run playbook trades` | Ranked trade proposals with rest-of-season impact |
| `uv run playbook standings` | Monte Carlo playoff odds + projected finish |
| `uv run playbook brief --week N` | Combined weekly report |

Each playbook writes a timestamped Markdown report to `reports/` and prints a summary. The decision
procedure behind each is documented in [`docs/playbooks/`](docs/playbooks/).

## Layout

See [the implementation plan](docs/plan.md) for the full architecture. Key packages live under
`src/league_engine/`: `ingest/` (data sources), `scoring.py` (league scoring engine),
`projections.py` (projection model), `analysis/` (one module per playbook), `cli.py` (playbook CLI).

## Agent & skills

Project agent `league-analyst` (`.claude/agents/`) runs the weekly cycle in the background; skills `league-draft-review`, `league-track`, `league-trend`, `league-waivers`, `league-weekly` (`.claude/skills/`) hold each procedure.

The agent runs on a schedule as the `unsw-league-weekly` task: Wednesdays 08:00 Sydney (after Monday Night Football finishes on Tuesday Sydney time).
