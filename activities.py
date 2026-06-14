from __future__ import annotations

import dataclasses
import logging
import os
import re

from jellyfin_apiclient_python import JellyfinClient
from temporalio import activity

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
    response = client.jellyfin.shows("/NextUp", {
        "UserId": user_id,
        "Limit": _ITEM_FETCH_LIMIT,
    })
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
