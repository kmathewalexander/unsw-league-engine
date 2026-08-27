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

### 2. Yahoo developer app

1. Go to https://developer.yahoo.com/apps/create/
2. Application Type: **Installed Application**
3. API Permissions: **Fantasy Sports → Read**
4. Redirect URI: `https://localhost:8080`
5. Copy `.env.example` to `.env` and paste in the Client ID / Client Secret.

### 3. League config

Edit `config/league.toml` — set the current-season Yahoo `league_id` (the number in your league
URL: `https://football.fantasysports.yahoo.com/f1/<league_id>`) and your team name.

### 4. First sync

```bash
uv run playbook sync
```

The first run opens a browser for a one-time Yahoo OAuth consent; paste the verification code back
into the terminal. The token is cached in `private/` and reused afterwards. `sync` then auto-discovers
every prior season of the league the API exposes and snapshots all data to `data/`.

## Playbooks

| Command | Purpose |
|---|---|
| `uv run playbook sync` | Refresh all Yahoo + nflverse + FantasyPros data |
| `uv run playbook draft-prep` | Pre-draft report: league tendencies, manager profiles, value board, targets |
| `uv run playbook draft-live` | Interactive draft assistant |
| `uv run playbook lineup --week N` | Optimal lineup + start/sit calls |
| `uv run playbook waivers --week N` | Ranked waiver targets, drops, FAAB bids |
| `uv run playbook trades` | Ranked trade proposals with rest-of-season impact |
| `uv run playbook standings` | Monte Carlo playoff odds + projected finish |
| `uv run playbook brief --week N` | Combined weekly report |

Each playbook writes a timestamped Markdown report to `reports/` and prints a summary. The decision
procedure behind each is documented in [`docs/playbooks/`](docs/playbooks/).

## Layout

See [the implementation plan](docs/plan.md) for the full architecture. Key packages live under
`src/league_engine/`: `ingest/` (data sources), `scoring.py` (league scoring engine),
`projections.py` (projection model), `analysis/` (one module per playbook), `cli.py` (playbook CLI).
