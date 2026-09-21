---
name: league-waivers
description: Decide lineup and waiver moves for the UNSW league from the engine's evidence — marginal optimal-lineup gain, usage vs results, trend. Use for "who should I pick up", "should I claim X", or start/sit questions.
---

# league-waivers

```bash
uv run playbook waivers --week N          # free agents ranked by marginal lineup gain
uv run playbook player "Player One" "Player Two"   # evidence profile + gain if added
uv run playbook review --week N           # my optimal lineup, power rankings, odds
```

Rules of thumb:
1. **`lineup_gain` is the decision metric** — it re-solves my optimal lineup (incl. the
   Q/W/R/T superflex slot) with the player added. 0.00 = would not start; don't spend a claim.
2. Check `xfp` vs `ppg26`: a big positive `luck` means the box score is flattering him.
3. Check trend (`league-trend`) for scheme/schedule; prefer players with rising snaps.
4. Name the drop (`replaces`, or lowest-value bench) — roster spots are limited (6 BN, 3 IR).
5. Confirm availability on Yahoo before recommending (the wire snapshot can be a day old).

Never submit claims; recommend and let the user act.
