---
name: league-draft-review
description: Draft-prep review for the UNSW league — how each manager drafts, who appears to do their research, where value was in past drafts. Use before next year's draft or when asked about manager tendencies.
---

# league-draft-review

```bash
uv run playbook draft-review      # rebuilds picks + market + outcomes, writes reports/draft-review-*.md
```

Inputs: `config/draft_history/<season>.txt` (`Player|POS` in pick order, from Yahoo
`/f1/827662/draftresults` and `?draft_results_period=previous`), `orders.toml` (round-1 order +
franchise renames). Add a season by pasting the page text into a new file and its order into
`orders.toml`. After each draft, refresh 2026 and re-run. Stored at `data/raw/league/draft/season_*`
and `data/derived/league/draft_manager_profiles`.

Method: market price at draft time = archived FantasyPros superflex ranking (`redraft-op`) from
nflverse; outcome = season points in league scoring. See `docs/playbooks/draft-review.md`.

How to read it (research is *inferred*, never observed):
- `consensus_gap` low = follows public rankings; high = idiosyncratic.
- `deviation_edge` = payoff on picks that strayed >=15 slots from market minus payoff on consensus
  picks. Blank until a manager has >=3 such picks — do not over-read it.
- `hit_rate` / `bust_rate` / `surplus_vs_mkt` (relative to league average) = did picks work out.
- `moves` = in-season engagement (waiver/trade activity).
- Two seasons of data: treat everything as a lead, not a verdict. Renames are mapped in
  `orders.toml` (`assumed` rows need confirming).

Report: exploitable tendencies first (who reaches at QB, who waits on TE, K/DEF timing, homers),
then who to trade with / who to avoid, then a next-year draft plan for my slot.
