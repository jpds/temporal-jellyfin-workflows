from __future__ import annotations

import dataclasses
import json
import logging
import os
import re
import urllib.parse
import urllib.request

from temporalio import activity
from temporalio.exceptions import ApplicationError

from common import _ITEM_FETCH_LIMIT, _make_client

LOG = logging.getLogger(__name__)


@dataclasses.dataclass
class SeasonInfo:
    number: int
    premiere_date: str | None


@dataclasses.dataclass
class SeriesSeasonInfo:
    name: str
    series_id: str
    tmdb_id: str | None
    tvdb_id: str | None
    owned_season_numbers: list[int]


@activity.defn
def fetch_series_season_presence() -> list[SeriesSeasonInfo]:
    client, user_id = _make_client()

    series_response = client.jellyfin.items(
        params={
            "userId": user_id,
            "Recursive": "true",
            "IncludeItemTypes": "Series",
            "Fields": "ProviderIds",
            "Limit": _ITEM_FETCH_LIMIT,
        }
    )
    series_items = series_response.get("Items", [])

    seasons_response = client.jellyfin.items(
        params={
            "userId": user_id,
            "Recursive": "true",
            "IncludeItemTypes": "Season",
            "Fields": "IndexNumber,SeriesId",
            "Limit": _ITEM_FETCH_LIMIT,
        }
    )

    seasons_by_series: dict[str, list[int]] = {}
    for season in seasons_response.get("Items", []):
        sid = season.get("SeriesId")
        idx = season.get("IndexNumber")
        if sid and idx is not None and idx > 0:  # skip Specials (season 0)
            seasons_by_series.setdefault(sid, []).append(idx)

    result = []
    for item in series_items:
        provider_ids = item.get("ProviderIds", {})
        series_id = item["Id"]
        result.append(
            SeriesSeasonInfo(
                name=item["Name"],
                series_id=series_id,
                tmdb_id=provider_ids.get("Tmdb"),
                tvdb_id=provider_ids.get("Tvdb"),
                owned_season_numbers=sorted(seasons_by_series.get(series_id, [])),
            )
        )
    return result


def _tvmaze_get(path: str) -> object:
    base = os.environ.get("TVMAZE_BASE_URL", "https://api.tvmaze.com")
    url = f"{base}{path}"
    with urllib.request.urlopen(url, timeout=10) as resp:  # noqa: S310
        body = resp.read()
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise ApplicationError(
            f"TVMaze returned invalid JSON for {path}", non_retryable=True
        ) from exc


@activity.defn
def fetch_external_seasons(series: SeriesSeasonInfo) -> list[SeasonInfo]:
    tmdb_key = os.environ.get("TMDB_API_KEY")
    if tmdb_key and series.tmdb_id:
        try:
            base = os.environ.get("TMDB_BASE_URL", "https://api.themoviedb.org")
            url = f"{base}/3/tv/{series.tmdb_id}?api_key={tmdb_key}"
            with urllib.request.urlopen(url, timeout=10) as resp:  # noqa: S310
                data = json.loads(resp.read())
            return sorted(
                [
                    SeasonInfo(
                        number=s["season_number"],
                        premiere_date=s.get("air_date") or None,
                    )
                    for s in data.get("seasons", [])
                    if s.get("season_number", 0) > 0
                ],
                key=lambda x: x.number,
            )
        except Exception:
            LOG.warning("TMDB lookup failed for '%s'; trying TVMaze", series.name)

    search_name = re.sub(r"\s*\(\d{4}\)\s*$", "", series.name)
    qs = urllib.parse.urlencode({"q": search_name})
    results = _tvmaze_get(f"/search/shows?{qs}")
    if not results:
        LOG.warning("No TVMaze match for '%s'; skipping", series.name)
        return []
    show_id = results[0]["show"]["id"]
    seasons = _tvmaze_get(f"/shows/{show_id}/seasons")
    return sorted(
        [
            SeasonInfo(
                number=s["number"],
                premiere_date=s.get("premiereDate") or None,
            )
            for s in seasons
            if s.get("number", 0) > 0
        ],
        key=lambda x: x.number,
    )
