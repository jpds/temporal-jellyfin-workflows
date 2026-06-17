from __future__ import annotations

import asyncio
import dataclasses
import os
import re
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    import annotated_types  # noqa: F401
    import pydantic_core  # noqa: F401
    from agents import Agent, Runner

    from director_completeness.activities import (
        DirectorInfo,
        FilmInfo,
        fetch_all_movie_titles,
        fetch_director_filmography,
        fetch_prominent_directors,
        resolve_director_tmdb_id,
    )

_MODEL = os.environ.get("RECOMMENDER_MODEL", "gpt-4o")

_ACTIVITY_RETRY = RetryPolicy(
    maximum_attempts=5,
    initial_interval=timedelta(seconds=2),
)
_ACTIVITY_TIMEOUT = timedelta(seconds=30)

_DIRECTOR_COMPLETENESS_SYSTEM_PROMPT = """
You are a film collection assistant.

The user has a significant number of films by certain directors but is missing
others from their filmographies. For each director, you are given the titles
they own and the titles they are missing.

Summarise what is missing, one or two sentences per director. Prioritise:
- Directors where the user is closest to a complete collection
- Note if a missing film is considered essential or a career highlight
- If a missing film predates everything they own, note it as an early work;
  if it postdates everything they own, note it as a recent release

Be concise. Do not repeat the full lists back; just narrate the gaps.
"""


def _normalise(title: str) -> str:
    title = title.lower()
    title = re.sub(r"\(\d{4}\)\s*$", "", title)
    title = re.sub(r"[^a-z0-9 ]", "", title)
    return title.strip()


@dataclasses.dataclass
class DirectorGap:
    name: str
    owned: list[str]
    missing: list[FilmInfo]


def _compute_director_gaps(
    directors: list[DirectorInfo],
    filmographies: dict[str, list[FilmInfo]],
    all_titles: list[str],
) -> list[DirectorGap]:
    library = {_normalise(t) for t in all_titles}
    result = []
    for d in directors:
        all_films = filmographies.get(d.name)
        if not all_films:
            continue
        missing = [f for f in all_films if _normalise(f.title) not in library]
        if missing:
            result.append(
                DirectorGap(name=d.name, owned=d.owned_titles, missing=missing)
            )
    return sorted(result, key=lambda g: g.name)


def _build_director_completeness_input(gaps: list[DirectorGap]) -> list[dict]:
    lines = [
        "",
        "DIRECTORS WITH INCOMPLETE COLLECTIONS:",
        "",
    ]
    for g in gaps:
        owned_str = ", ".join(g.owned) or "(none)"
        missing_str = ", ".join(
            f"{f.title} ({f.year or 'TBA'})"
            for f in sorted(g.missing, key=lambda f: f.title)
        )
        lines.append(g.name)
        lines.append(f"  Owned ({len(g.owned)}): {owned_str}")
        lines.append(f"  Missing ({len(g.missing)}): {missing_str}")
        lines.append("")
        lines.append(f"Today's date: {workflow.now().date().isoformat()}")
    return [
        {
            "role": "user",
            "content": [{"type": "input_text", "text": "\n".join(lines)}],
        }
    ]


@workflow.defn
class DirectorCompletenessWorkflow:
    @workflow.run
    async def run(self) -> str:
        all_titles, directors = await asyncio.gather(
            workflow.execute_activity(
                fetch_all_movie_titles,
                start_to_close_timeout=_ACTIVITY_TIMEOUT,
                retry_policy=_ACTIVITY_RETRY,
            ),
            workflow.execute_activity(
                fetch_prominent_directors,
                start_to_close_timeout=_ACTIVITY_TIMEOUT,
                retry_policy=_ACTIVITY_RETRY,
            ),
        )
        workflow.random().shuffle(directors)
        resolve_sem = asyncio.Semaphore(3)
        filmography_sem = asyncio.Semaphore(3)

        async def _resolve_and_fetch(d: DirectorInfo) -> list[FilmInfo]:
            async with resolve_sem:
                tmdb_id = await workflow.execute_activity(
                    resolve_director_tmdb_id,
                    d,
                    activity_id=f"resolve-{d.jellyfin_person_id}",
                    start_to_close_timeout=_ACTIVITY_TIMEOUT,
                    retry_policy=_ACTIVITY_RETRY,
                )
            if not tmdb_id:
                return []
            async with filmography_sem:
                return await workflow.execute_activity(
                    fetch_director_filmography,
                    dataclasses.replace(d, tmdb_person_id=tmdb_id),
                    activity_id=d.jellyfin_person_id,
                    start_to_close_timeout=_ACTIVITY_TIMEOUT,
                    retry_policy=_ACTIVITY_RETRY,
                )

        filmography_lists = await asyncio.gather(
            *[_resolve_and_fetch(d) for d in directors]
        )
        filmographies = {
            d.name: films
            for d, films in zip(directors, filmography_lists)
            if films
        }
        gaps = _compute_director_gaps(directors, filmographies, all_titles)
        if not gaps:
            return "Your collections for all represented directors appear complete."
        agent = Agent(
            name="Director Completeness Assistant",
            instructions=_DIRECTOR_COMPLETENESS_SYSTEM_PROMPT,
            model=_MODEL,
        )
        result = await Runner.run(
            agent, input=_build_director_completeness_input(gaps)
        )
        return result.final_output
