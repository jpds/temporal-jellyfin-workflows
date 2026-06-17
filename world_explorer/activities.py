from __future__ import annotations

import logging

from temporalio import activity

from common import _ITEM_FETCH_LIMIT, _make_client

LOG = logging.getLogger(__name__)


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


def _fetch_in_progress_names() -> set[str]:
    client, user_id = _make_client()
    response = client.jellyfin.shows(
        "/NextUp",
        {
            "UserId": user_id,
            "Limit": _ITEM_FETCH_LIMIT,
        },
    )
    return {
        item["SeriesName"]
        for item in response.get("Items", [])
        if item.get("SeriesName")
    }


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
def fetch_unwatched_movies() -> list[str]:
    return _fetch_movies("IsUnplayed")


@activity.defn
def fetch_unwatched_series() -> list[str]:
    in_progress = _fetch_in_progress_names()
    return [
        item["Name"]
        for item in _fetch_all_series()
        if not item.get("UserData", {}).get("Played", False)
        and item["Name"] not in in_progress
    ]
