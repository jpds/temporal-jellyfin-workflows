from __future__ import annotations

import dataclasses
import logging
import os
import re

from jellyfin_apiclient_python import JellyfinClient

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
    client.config.data["app.name"] = "temporal-jellyfin"
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
