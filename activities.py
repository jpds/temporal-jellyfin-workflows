from __future__ import annotations

import dataclasses
import json
import logging
import os
import re
import urllib.parse
import urllib.request

from jellyfin_apiclient_python import JellyfinClient
from temporalio import activity
from temporalio.exceptions import ApplicationError

_UUID_RE = re.compile(
    r"^(?:"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    r"|[0-9a-f]{32}"
    r")$",
    re.I,
)

LOG = logging.getLogger(__name__)

_ITEM_FETCH_LIMIT = 10000


@dataclasses.dataclass
class JellyfinData:
    favorites: list[str]
    watched_movies: list[str]
    watched_series: list[str]
    in_progress_series: list[str]
    unwatched_movies: list[str]
    unwatched_series: list[str]


def _make_client() -> tuple[JellyfinClient, str]:
    client = JellyfinClient()
    client.config.data["app.name"] = "jellyfin-recommender"
    client.config.data["app.version"] = "1.0.0"
    client.config.data["auth.ssl"] = True
    client.authenticate(
        {
            "Servers": [
                {
                    "AccessToken": os.environ["JELLYFIN_API_KEY"],
                    "address": os.environ["JELLYFIN_URL"],
                }
            ]
        },
        discover=False,
    )

    user_id = os.environ.get("JELLYFIN_USER_ID")
    if not user_id or not _UUID_RE.match(user_id):
        users = client.jellyfin.get_users()
        if not users:
            raise RuntimeError("No users found on Jellyfin server")
        if not user_id:
            user_id = users[0]["Id"]
            LOG.info("Auto-detected user ID: %s", user_id)
        else:
            match = next(
                (u for u in users if u["Name"].lower() == user_id.lower()), None
            )
            if not match:
                raise RuntimeError(f"No Jellyfin user found with name '{user_id}'")
            LOG.info("Resolved '%s' to user ID: %s", user_id, match["Id"])
            user_id = match["Id"]

    return client, user_id


def _fetch_movies(played_filter: str) -> list[str]:
    client, user_id = _make_client()
    response = client.jellyfin.items(
        params={
            "userId": user_id,
            "Recursive": "true",
            "IncludeItemTypes": "Movie",
            "Filters": played_filter,
            "Limit": _ITEM_FETCH_LIMIT,
        }
    )
    total = response.get("TotalRecordCount", 0)
    items = response.get("Items", [])
    if total > len(items):
        LOG.warning(
            "Movie fetch truncated: retrieved %d of %d. "
            "Increase _ITEM_FETCH_LIMIT or add pagination.",
            len(items),
            total,
        )
    return [item["Name"] for item in items]


def _fetch_all_series() -> list[dict]:
    # IsPlayed/IsUnplayed filters are not supported for Series; check
    # UserData fields client-side instead.
    client, user_id = _make_client()
    response = client.jellyfin.items(
        params={
            "userId": user_id,
            "Recursive": "true",
            "IncludeItemTypes": "Series",
            "Fields": "UserData",
            "Limit": _ITEM_FETCH_LIMIT,
        }
    )
    total = response.get("TotalRecordCount", 0)
    items = response.get("Items", [])
    if total > len(items):
        LOG.warning(
            "Series fetch truncated: retrieved %d of %d. "
            "Increase _ITEM_FETCH_LIMIT or add pagination.",
            len(items),
            total,
        )
    return items


@activity.defn
def fetch_favorites() -> list[str]:
    client, user_id = _make_client()
    response = client.jellyfin.items(
        params={
            "userId": user_id,
            "Recursive": "true",
            "IncludeItemTypes": "Movie,Series",
            "Filters": "IsFavorite",
        }
    )
    return [item["Name"] for item in response.get("Items", [])]


@activity.defn
def fetch_watched_movies() -> list[str]:
    return _fetch_movies("IsPlayed")


@activity.defn
def fetch_watched_series() -> list[str]:
    return [
        item["Name"]
        for item in _fetch_all_series()
        if item.get("UserData", {}).get("Played", False)
    ]


@activity.defn
def fetch_in_progress_series() -> list[str]:
    client, user_id = _make_client()
    response = client.jellyfin.shows(
        "/NextUp",
        {
            "UserId": user_id,
            "Limit": _ITEM_FETCH_LIMIT,
        },
    )
    seen: set[str] = set()
    result: list[str] = []
    for item in response.get("Items", []):
        name = item.get("SeriesName")
        if name and name not in seen:
            seen.add(name)
            result.append(name)
    return result


@activity.defn
def fetch_unwatched_movies() -> list[str]:
    return _fetch_movies("IsUnplayed")


@activity.defn
def fetch_unwatched_series() -> list[str]:
    in_progress = set(fetch_in_progress_series())
    return [
        item["Name"]
        for item in _fetch_all_series()
        if not item.get("UserData", {}).get("Played", False)
        and item["Name"] not in in_progress
    ]


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


@dataclasses.dataclass
class DirectorInfo:
    name: str
    jellyfin_person_id: str
    tmdb_person_id: str | None
    owned_titles: list[str]


@dataclasses.dataclass
class FilmInfo:
    title: str
    year: int | None
    popularity: float | None


def _fetch_jellyfin_person_tmdb_id(jellyfin_person_id: str) -> str | None:
    base_url = os.environ["JELLYFIN_URL"].rstrip("/")
    api_key = os.environ["JELLYFIN_API_KEY"]
    url = (
        f"{base_url}/Persons/{jellyfin_person_id}?Fields=ProviderIds&api_key={api_key}"
    )
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:  # noqa: S310
            data = json.loads(resp.read())
        tmdb_id = data.get("ProviderIds", {}).get("Tmdb")
        if not tmdb_id:
            LOG.debug("Jellyfin has no TMDB ID for person %s", jellyfin_person_id)
        return tmdb_id
    except Exception as exc:
        LOG.warning("Jellyfin person lookup failed for %s: %s", jellyfin_person_id, exc)
        return None


def _search_tmdb_person(name: str) -> str | None:
    tmdb_key = os.environ.get("TMDB_API_KEY")
    if not tmdb_key:
        return None
    base = os.environ.get("TMDB_BASE_URL", "https://api.themoviedb.org")
    qs = urllib.parse.urlencode({"query": name, "api_key": tmdb_key})
    url = f"{base}/3/search/person?{qs}"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:  # noqa: S310
            data = json.loads(resp.read())
        results = data.get("results", [])
        if results:
            return str(results[0]["id"])
    except Exception as exc:
        LOG.warning("TMDB person search failed for '%s': %s", name, exc)
    return None


@activity.defn
def fetch_all_movie_titles() -> list[str]:
    client, user_id = _make_client()
    response = client.jellyfin.items(
        params={
            "userId": user_id,
            "Recursive": "true",
            "IncludeItemTypes": "Movie",
            "Limit": _ITEM_FETCH_LIMIT,
        }
    )
    total = response.get("TotalRecordCount", 0)
    items = response.get("Items", [])
    if total > len(items):
        LOG.warning(
            "Movie fetch truncated: retrieved %d of %d.",
            len(items),
            total,
        )
    return [item["Name"] for item in items]


@activity.defn
def fetch_prominent_directors(min_owned: int = 2) -> list[DirectorInfo]:
    client, user_id = _make_client()
    response = client.jellyfin.items(
        params={
            "userId": user_id,
            "Recursive": "true",
            "IncludeItemTypes": "Movie",
            "Fields": "People",
            "Limit": _ITEM_FETCH_LIMIT,
        }
    )
    total = response.get("TotalRecordCount", 0)
    items = response.get("Items", [])
    if total > len(items):
        LOG.warning(
            "Movie fetch truncated: retrieved %d of %d.",
            len(items),
            total,
        )

    director_titles: dict[str, list[str]] = {}
    director_pid: dict[str, str] = {}
    for item in items:
        for person in item.get("People", []):
            if person.get("Type") != "Director":
                continue
            name = person["Name"]
            director_titles.setdefault(name, []).append(item["Name"])
            director_pid[name] = person["Id"]

    result = []
    for name, titles in sorted(director_titles.items(), key=lambda x: -len(x[1])):
        if len(titles) < min_owned:
            continue
        result.append(
            DirectorInfo(
                name=name,
                jellyfin_person_id=director_pid[name],
                tmdb_person_id=None,
                owned_titles=sorted(titles),
            )
        )
    return result


@activity.defn
def resolve_director_tmdb_id(director: DirectorInfo) -> str | None:
    tmdb_id = _fetch_jellyfin_person_tmdb_id(director.jellyfin_person_id)
    if tmdb_id:
        return tmdb_id
    return _search_tmdb_person(director.name)


@activity.defn
def fetch_director_filmography(director: DirectorInfo) -> list[FilmInfo]:
    if not director.tmdb_person_id:
        LOG.warning("No TMDB person ID for '%s'; skipping", director.name)
        return []
    tmdb_key = os.environ.get("TMDB_API_KEY")
    if not tmdb_key:
        raise ApplicationError("TMDB_API_KEY is required", non_retryable=True)
    base = os.environ.get("TMDB_BASE_URL", "https://api.themoviedb.org")
    url = f"{base}/3/person/{director.tmdb_person_id}/movie_credits?api_key={tmdb_key}"
    with urllib.request.urlopen(url, timeout=10) as resp:  # noqa: S310
        body = resp.read()
    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ApplicationError(
            f"TMDB returned invalid JSON for person {director.tmdb_person_id}",
            non_retryable=True,
        ) from exc
    films = []
    for entry in data.get("crew", []):
        if entry.get("job") != "Director":
            continue
        release_date = entry.get("release_date") or ""
        if not release_date:
            continue
        try:
            year = int(release_date[:4])
        except (ValueError, TypeError):
            year = None
        films.append(
            FilmInfo(
                title=entry["title"],
                year=year,
                popularity=entry.get("popularity"),
            )
        )
    return sorted(films, key=lambda f: f.year or 0)


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
