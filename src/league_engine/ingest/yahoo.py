"""Yahoo Fantasy Sports ingest via yfpy.

Responsibilities:
  * Own the OAuth lifecycle (one-time browser consent, token cached under ``private/``).
  * Auto-discover every prior season of the configured league the account can see.
  * Snapshot league settings, draft results, transactions, rosters, matchups and
    standings for each season to ``data/raw/yahoo/<season>/``.

Raw payloads are stored as JSON (Yahoo objects are deeply nested); flattening into
tidy Parquet tables happens in ``scoring.py`` / the analysis modules that consume them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from dotenv import load_dotenv
from yfpy.query import YahooFantasySportsQuery

from .. import store
from ..config import Settings, load_settings, load_yahoo_secrets, write_history

# Yahoo issues a new NFL "game id" each season. We resolve the season -> game_id map
# dynamically from the API (get_all_yahoo_fantasy_game_keys) rather than hardcoding it.


def _serialize(obj: Any) -> Any:
    """Best-effort conversion of a yfpy model (or list thereof) to plain JSON types."""
    if isinstance(obj, list):
        return [_serialize(o) for o in obj]
    if hasattr(obj, "serialized"):
        return obj.serialized()
    if hasattr(obj, "clean_data_dict"):
        return obj.clean_data_dict()
    return obj


@dataclass
class SeasonHandle:
    season: int
    league_id: str
    query: YahooFantasySportsQuery


class YahooClient:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or load_settings()
        self._secrets = load_yahoo_secrets()
        # Load any previously persisted token vars.
        load_dotenv(self.settings.private_dir / ".env")
        self._current: YahooFantasySportsQuery | None = None
        self._game_ids: dict[int, int] | None = None

    # ---- connection -------------------------------------------------------

    def _make_query(self, league_id: str, game_id: int | None) -> YahooFantasySportsQuery:
        return YahooFantasySportsQuery(
            league_id=league_id,
            game_code=self.settings.league.game_code,
            game_id=game_id,
            yahoo_consumer_key=self._secrets.consumer_key,
            yahoo_consumer_secret=self._secrets.consumer_secret,
            env_var_fallback=True,
            env_file_location=self.settings.private_dir,
            save_token_data_to_env_file=True,
            browser_callback=True,
            retries=3,
            backoff=1,
        )

    def current(self) -> YahooFantasySportsQuery:
        if self._current is None:
            lg = self.settings.league
            if not lg.current_league_id:
                raise RuntimeError(
                    "config/league.toml: [league].current_league_id is empty. "
                    "Set it to the number in your Yahoo league URL."
                )
            # game_id=None -> yfpy resolves the current NFL game automatically.
            self._current = self._make_query(lg.current_league_id, None)
        return self._current

    def game_id_for(self, season: int) -> int | None:
        """Season -> Yahoo NFL game id, resolved once from the API and cached."""
        if self._game_ids is None:
            self._game_ids = {}
            try:
                for g in self.current().get_all_yahoo_fantasy_game_keys() or []:
                    try:
                        self._game_ids[int(g.season)] = int(g.game_id)
                    except (TypeError, ValueError):
                        continue
            except Exception as exc:  # noqa: BLE001
                print(f"  [yahoo] could not resolve game ids: {exc}")
        return self._game_ids.get(season)

    # ---- discovery ------------------------------------------------------

    def discover_history(self) -> dict[int, str]:
        """Return {season: league_id} for every NFL league the account can access,
        keyed to the same league lineage as the configured current league.

        Yahoo renews a league each year under a new league_id but keeps the same
        league *name*; we match on name.
        """
        q = self.current()
        target_name = self._league_name(q)
        cur = self.settings.league.current_season
        found: dict[int, str] = {cur: self.settings.league.current_league_id}

        for season in range(cur - 1, cur - 16, -1):
            game_id = self.game_id_for(season)
            if game_id is None:
                continue
            try:
                leagues = q.get_user_leagues_by_game_key(game_id)
            except Exception:
                continue
            for lg in leagues or []:
                name = getattr(lg, "name", None)
                if name and _norm(name) == _norm(target_name):
                    lid = str(getattr(lg, "league_id", "")).strip()
                    if lid:
                        found[season] = lid
        return dict(sorted(found.items()))

    @staticmethod
    def _league_name(q: YahooFantasySportsQuery) -> str:
        meta = q.get_league_metadata()
        return getattr(meta, "name", "") or ""

    # ---- per-season snapshot ------------------------------------------

    def snapshot_season(self, season: int, league_id: str) -> None:
        q = self._make_query(league_id, self.game_id_for(season))
        layer, key = "raw/yahoo", str(season)

        def grab(name: str, fn):
            try:
                store.write_json(_serialize(fn()), layer, key, name)
            except Exception as exc:  # noqa: BLE001 - want one bad call not to abort the season
                store.write_json({"error": str(exc)}, layer, key, name)

        grab("league_metadata", q.get_league_metadata)
        grab("league_settings", q.get_league_settings)
        grab("league_standings", q.get_league_standings)
        grab("league_teams", q.get_league_teams)
        grab("league_players", q.get_league_players)
        grab("draft_results", q.get_league_draft_results)
        grab("transactions", q.get_league_transactions)

        # Weekly matchups: regular season is at most ~18 weeks.
        matchups: dict[int, Any] = {}
        for wk in range(1, 19):
            try:
                matchups[wk] = _serialize(q.get_league_matchups_by_week(wk))
            except Exception:
                break
        store.write_json(matchups, layer, key, "matchups_by_week")

    # ---- current-season live data ------------------------------------

    def snapshot_current_live(self) -> None:
        """Data that changes week to week for the active season."""
        q = self.current()
        season = str(self.settings.league.current_season)
        layer = "raw/yahoo"

        for name, fn in [
            ("league_metadata", q.get_league_metadata),
            ("league_settings", q.get_league_settings),
            ("league_teams", q.get_league_teams),
            ("league_players", q.get_league_players),
            ("league_standings", q.get_league_standings),
            ("draft_results", q.get_league_draft_results),
            ("transactions", q.get_league_transactions),
        ]:
            try:
                store.write_json(_serialize(fn()), layer, season, name)
            except Exception as exc:  # noqa: BLE001
                store.write_json({"error": str(exc)}, layer, season, name)


def _norm(s: str) -> str:
    return "".join(ch.lower() for ch in s if ch.isalnum())


def sync(full: bool = False) -> dict[int, str]:
    """Entry point used by ``playbook sync``.

    Returns the discovered season -> league_id map (also written back to league.toml).
    """
    settings = load_settings()
    client = YahooClient(settings)

    history = dict(settings.league.history)
    if full or not history:
        history = client.discover_history()
        write_history(history)
        settings = load_settings()

    # Snapshot every historical season once (skip if already present unless --full).
    for season, lid in history.items():
        if season == settings.league.current_season:
            continue
        if not full and store.json_exists("raw/yahoo", str(season), "draft_results"):
            continue
        client.snapshot_season(season, lid)

    client.snapshot_current_live()
    return history
