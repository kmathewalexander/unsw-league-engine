"""Load and validate project configuration.

Config lives in two places:
  * ``config/league.toml`` -- league identity, source toggles, model params (committed).
  * ``.env``               -- Yahoo API secrets (never committed).

``playbook sync`` writes the discovered season -> league_id map back into ``league.toml``
under ``[league.history]`` so it stays inspectable.
"""

from __future__ import annotations

import os
import tomllib
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, Field

# Repo root = two parents up from this file (src/league_engine/config.py -> repo/).
ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = ROOT / "config" / "league.toml"
DATA_DIR = ROOT / "data"
REPORTS_DIR = ROOT / "reports"
PRIVATE_DIR = ROOT / "private"
ENV_PATH = ROOT / ".env"


class LeagueConfig(BaseModel):
    game_code: str = "nfl"
    current_season: int
    current_league_id: str
    team_name: str
    teams: int = 12
    timezone: str = "Australia/Sydney"
    history: dict[int, str] = Field(default_factory=dict)


class FaabConfig(BaseModel):
    season_budget: int = 100
    min_bid: int = 0


class SourcesConfig(BaseModel):
    use_fantasypros: bool = True
    use_nflverse: bool = True


class ProjectionsConfig(BaseModel):
    w_fantasypros: float = 0.5
    w_usage_model: float = 0.3
    w_prior_year: float = 0.2
    floor_percentile: int = 20
    ceiling_percentile: int = 80


class StandingsConfig(BaseModel):
    monte_carlo_iterations: int = 10000
    playoff_teams: int = 6


class YahooSecrets(BaseModel):
    consumer_key: str
    consumer_secret: str


class Settings(BaseModel):
    league: LeagueConfig
    faab: FaabConfig = FaabConfig()
    sources: SourcesConfig = SourcesConfig()
    projections: ProjectionsConfig = ProjectionsConfig()
    standings: StandingsConfig = StandingsConfig()

    @property
    def root(self) -> Path:
        return ROOT

    @property
    def data_dir(self) -> Path:
        return DATA_DIR

    @property
    def reports_dir(self) -> Path:
        return REPORTS_DIR

    @property
    def private_dir(self) -> Path:
        return PRIVATE_DIR


def _read_toml() -> dict:
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"Missing config file: {CONFIG_PATH}")
    with CONFIG_PATH.open("rb") as fh:
        return tomllib.load(fh)


@lru_cache(maxsize=1)
def load_settings() -> Settings:
    raw = _read_toml()
    league_raw = raw.get("league", {})
    history_raw = league_raw.pop("history", {}) or {}
    history = {int(k): str(v) for k, v in history_raw.items()}

    settings = Settings(
        league=LeagueConfig(**league_raw, history=history),
        faab=FaabConfig(**raw.get("faab", {})),
        sources=SourcesConfig(**raw.get("sources", {})),
        projections=ProjectionsConfig(**raw.get("projections", {})),
        standings=StandingsConfig(**raw.get("standings", {})),
    )
    for d in (settings.data_dir, settings.reports_dir, settings.private_dir):
        d.mkdir(parents=True, exist_ok=True)
    return settings


def load_yahoo_secrets() -> YahooSecrets:
    load_dotenv(ENV_PATH)
    key = os.environ.get("YAHOO_CONSUMER_KEY", "").strip()
    secret = os.environ.get("YAHOO_CONSUMER_SECRET", "").strip()
    if not key or not secret:
        raise RuntimeError(
            "YAHOO_CONSUMER_KEY / YAHOO_CONSUMER_SECRET not set. "
            "Copy .env.example to .env and fill them in (see README)."
        )
    return YahooSecrets(consumer_key=key, consumer_secret=secret)


def write_history(history: dict[int, str]) -> None:
    """Persist the discovered season -> league_id map back into league.toml.

    Uses a minimal targeted rewrite of the ``[league.history]`` block so we don't
    depend on a TOML writer dependency.
    """
    text = CONFIG_PATH.read_text()
    lines = text.splitlines()
    out: list[str] = []
    in_history = False
    wrote = False
    for line in lines:
        stripped = line.strip()
        if stripped == "[league.history]":
            in_history = True
            out.append(line)
            for season in sorted(history):
                out.append(f'{season} = "{history[season]}"')
            wrote = True
            continue
        if in_history:
            # skip old history entries until the next section / blank-then-section
            if stripped.startswith("[") and stripped.endswith("]"):
                in_history = False
                out.append(line)
            elif stripped == "" or stripped.startswith("#") or "=" in stripped:
                continue
            else:
                in_history = False
                out.append(line)
        else:
            out.append(line)
    if not wrote:
        out.append("")
        out.append("[league.history]")
        for season in sorted(history):
            out.append(f'{season} = "{history[season]}"')
    CONFIG_PATH.write_text("\n".join(out) + "\n")
    load_settings.cache_clear()
