# Playbook: `draft-prep`

Produce the pre-draft brief: what your league does, and who to target.

```bash
uv run playbook draft-prep --teams 12
```

Writes `reports/draft-prep-<ts>.md` with four sections.

## 1. Value board (`analysis/draft.value_board`)

For every ranked player:

- `season_points` — projected league points (`projections.season_projection`):
  consensus positional rank → expected PPG from **this league's** historical
  rank→points curve (last 3 seasons), blended with the player's own prior-year PPG.
- `vor` — value over replacement. Replacement level per position is derived from
  your roster slots × teams (flex split 55/25/20 RB/WR/TE), so it reflects *your*
  league's starter demand.
- `adp` — consensus overall draft slot (nflverse `redraft-overall` ECR).
- `value_vs_adp` — `adp − overall_rank`. Positive = the board likes them more than
  the room does (draft-day value).
- `tier` — positional tier; gaps of >0.6 SD of positional points start a new tier.

**Read it as:** draft down the `vor` column, but jump on players near a tier cliff
and on the top `value_vs_adp` names when they fall to you.

## 2. Manager profiles (`analysis/draft.manager_profiles`)

Per opponent, across all discovered seasons: how early they take QB/TE, how RB/WR-heavy
their first 3 rounds are. Use it to predict runs and to know who will reach for a QB.

## 3. Positional run rates (`analysis/draft.positional_runs`)

How often each position clusters (3+ in a 5-pick window). High run-rate positions are
the ones where "wait one more round" backfires in this league.

## 4. Best values vs ADP

The 30 largest positive `value_vs_adp` — your shortlist of targets to let fall.

## Tuning

- Blend weights: `config/league.toml → [projections]`.
- Replacement-level flex split: `analysis/draft.replacement_levels`.
- If history is thin (new league), sections 2–3 will be sparse; the value board still
  works off ECR + prior-year points.
