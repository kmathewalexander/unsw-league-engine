# Playbook: `standings`

Project the rest of the season by Monte Carlo.

```bash
uv run playbook standings
```

> Requires current standings + remaining schedule. Wired once `sync` snapshots
> `league_standings` and `matchups_by_week` for the active season.

## Logic (`analysis/standings.simulate`)

For `iterations` (default 10,000):

1. For each remaining week, draw every team's score from
   `Normal(team_week_mean, team_week_sd)` where the mean is that team's optimal-lineup
   projection for the week and sd rolls up player-level variance.
2. Resolve head-to-head wins, accumulate points-for.
3. Rank final standings (wins, then PF); record playoff berth (top `playoff_teams`)
   and seed.

## Output

Per team: `playoff_odds | avg_wins | avg_seed`.

`analysis/standings.swing_weeks` additionally ranks *your* remaining games by how much
a win vs a loss moves your playoff odds — where to stream aggressively / spend FAAB.

## Tuning

- `[standings] monte_carlo_iterations`, `playoff_teams` in `config/league.toml`.
- Team weekly sd: currently a flat fallback (25) + player variance; refine with
  realised weekly score variance once the season has data.
