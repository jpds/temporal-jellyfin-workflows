from __future__ import annotations

import asyncio
import os
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    import annotated_types  # noqa: F401
    import pydantic_core  # noqa: F401
    from agents import Agent, Runner

    from common import JellyfinData
    from recommendations.activities import (
        fetch_favorites,
        fetch_in_progress_series,
        fetch_unwatched_movies,
        fetch_unwatched_series,
        fetch_watched_movies,
        fetch_watched_series,
    )

_MODEL = os.environ.get("RECOMMENDER_MODEL", "gpt-4o")

_ACTIVITY_RETRY = RetryPolicy(
    maximum_attempts=5,
    initial_interval=timedelta(seconds=2),
)
_ACTIVITY_TIMEOUT = timedelta(seconds=30)

_RECOMMENDATIONS_SYSTEM_PROMPT = """
You are a film and TV recommendation assistant

The user's Jellyfin library is given in labelled sections:
- FAVORITES: content they have marked as la crème de la crème
- WATCHED MOVIES / TV SERIES: titles they have watched at some point in the past
- IN-PROGRESS TV SERIES: shows they are partway through
- UNWATCHED MOVIES / TV SERIES: titles they own but have not watched

Recommend 10 to 15 titles, mixing both movies and TV series. Include a blend
of unwatched titles from their library and fresh discoveries not yet in it

Do not recommend something the user has already seen
"""


def _recommendations_section(label: str, items: list[str]) -> dict:
    return {
        "type": "input_text",
        "text": f"{label}:\n" + ("\n".join(items) or "(none)"),
    }


def _build_recommendations_input(data: JellyfinData) -> list[dict]:
    fav_set = set(data.favorites)
    return [
        {
            "role": "user",
            "content": [
                _recommendations_section("FAVORITES", sorted(data.favorites)),
                _recommendations_section(
                    "WATCHED MOVIES",
                    sorted(m for m in data.watched_movies if m not in fav_set),
                ),
                _recommendations_section(
                    "WATCHED TV SERIES",
                    sorted(s for s in data.watched_series if s not in fav_set),
                ),
                _recommendations_section(
                    "IN-PROGRESS TV SERIES", sorted(data.in_progress_series)
                ),
                _recommendations_section(
                    "UNWATCHED MOVIES", sorted(data.unwatched_movies)
                ),
                _recommendations_section(
                    "UNWATCHED TV SERIES", sorted(data.unwatched_series)
                ),
            ],
        }
    ]


@workflow.defn
class RecommendationsWorkflow:
    @workflow.run
    async def run(self) -> str:
        results = await asyncio.gather(
            workflow.execute_activity(
                fetch_favorites,
                start_to_close_timeout=_ACTIVITY_TIMEOUT,
                retry_policy=_ACTIVITY_RETRY,
            ),
            workflow.execute_activity(
                fetch_watched_movies,
                start_to_close_timeout=_ACTIVITY_TIMEOUT,
                retry_policy=_ACTIVITY_RETRY,
            ),
            workflow.execute_activity(
                fetch_watched_series,
                start_to_close_timeout=_ACTIVITY_TIMEOUT,
                retry_policy=_ACTIVITY_RETRY,
            ),
            workflow.execute_activity(
                fetch_in_progress_series,
                start_to_close_timeout=_ACTIVITY_TIMEOUT,
                retry_policy=_ACTIVITY_RETRY,
            ),
            workflow.execute_activity(
                fetch_unwatched_movies,
                start_to_close_timeout=_ACTIVITY_TIMEOUT,
                retry_policy=_ACTIVITY_RETRY,
            ),
            workflow.execute_activity(
                fetch_unwatched_series,
                start_to_close_timeout=_ACTIVITY_TIMEOUT,
                retry_policy=_ACTIVITY_RETRY,
            ),
        )
        (
            favorites,
            watched_movies,
            watched_series,
            in_progress_series,
            unwatched_movies,
            unwatched_series,
        ) = results
        data = JellyfinData(
            favorites=favorites,
            watched_movies=watched_movies,
            watched_series=watched_series,
            in_progress_series=in_progress_series,
            unwatched_movies=unwatched_movies,
            unwatched_series=unwatched_series,
        )
        agent = Agent(
            name="Jellyfin Recommender",
            instructions=_RECOMMENDATIONS_SYSTEM_PROMPT,
            model=_MODEL,
        )
        result = await Runner.run(agent, input=_build_recommendations_input(data))
        return result.final_output
