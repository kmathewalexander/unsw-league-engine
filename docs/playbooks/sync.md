# Playbook: `sync`

Refresh every data source and rebuild the derived tables.

```bash
uv run playbook sync                 # incremental (history pulled once, live data refreshed)
uv run playbook sync --full          # re-pull all history + force refresh
uv run playbook sync --week 3        # also pull FantasyPros week-3 projections
uv run playbook sync --skip-yahoo    # nflverse + FantasyPros only
```

## What it does

1. **Yahoo** (`ingest/yahoo.py`)
   - One-time OAuth consent on first run (browser opens; paste the code back).
   - Auto-discovers every prior season of the league your account can see by matching
     league name across Yahoo's yearly game ids, and writes the map to
     `config/league.toml → [league.history]`.
   - Snapshots per season to `data/raw/yahoo/<season>/`: `league_settings`,
     `league_players`, `draft_results`, `transactions`, `league_teams`,
     `league_standings`, `matchups_by_week`.
2. **nflverse** (`ingest/nfl.py`) → `data/raw/nflverse/`: `player_stats`, `schedules`,
   `snap_counts`, `depth_charts`, `injuries`, `players`, `ff_playerids`, `ff_rankings`,
   `ff_opportunity`. Lookback from 2016.
3. **FantasyPros** (`ingest/fantasypros.py`) → `data/raw/fantasypros/`: ECR cheatsheet
   blob + (best-effort) projection tables. Degrades silently if selectors break.
4. **Derived**: player crosswalk, parsed scoring rules (printed), and
   `derived/league_points.parquet` — league points for every player-week.

## Checks after running

- The printed **scoring rules** match your Yahoo league settings page. No "UNMAPPED".
- `config/league.toml → [league.history]` lists the seasons you expected.
- `data/meta.json` shows fresh timestamps and non-zero row counts.

## Failure modes

| Symptom | Fix |
|---|---|
| Browser doesn't open / headless | yfpy prints a URL — open it manually, paste the code. |
| `current_league_id is empty` | Set it in `config/league.toml`. |
| Only current season discovered | Older seasons may be under a differently-named league, or your account lost access. Add ids manually to `[league.history]`. |
| FantasyPros rows = 0 | Their page changed; projections fall back to nflverse ECR. Open an issue to fix selectors. |
