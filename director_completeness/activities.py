from __future__ import annotations

import dataclasses
import json
import logging
import os
import urllib.parse
import urllib.request

from temporalio import activity
from temporalio.exceptions import ApplicationError

from common import _ITEM_FETCH_LIMIT, _make_client

LOG = logging.getLogger(__name__)


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
