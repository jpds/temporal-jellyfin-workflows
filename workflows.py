from __future__ import annotations

import asyncio
import dataclasses
import os
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    import annotated_types  # noqa: F401
    import pydantic_core  # noqa: F401
    from agents import Agent, Runner

    from activities import (
        DirectorInfo,
        FilmInfo,
        JellyfinData,
        SeasonInfo,
        SeriesSeasonInfo,
        fetch_all_movie_titles,
        fetch_director_filmography,
        fetch_external_seasons,
        fetch_favorites,
        fetch_in_progress_series,
        fetch_prominent_directors,
        fetch_series_season_presence,
        fetch_unwatched_movies,
        fetch_unwatched_series,
        fetch_watched_movies,
        fetch_watched_series,
        resolve_director_tmdb_id,
    )

_MODEL = os.environ.get("RECOMMENDER_MODEL", "gpt-4o")

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

_ACTIVITY_RETRY = RetryPolicy(
    maximum_attempts=5,
    initial_interval=timedelta(seconds=2),
)
_ACTIVITY_TIMEOUT = timedelta(seconds=30)


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


@dataclasses.dataclass
class MissingSeasonReport:
    name: str
    owned: list[int]
    gap: list[SeasonInfo]
    trailing: list[SeasonInfo]


_MISSING_SEASONS_SYSTEM_PROMPT = """
You are a TV series collection assistant.

The user's Jellyfin library is missing seasons from some of their TV series.
Gap seasons fall between seasons they already own, blocking a continuous viewing
run. Trailing seasons are newer seasons they have not yet collected.

Summarise what is missing, prioritising gap seasons over trailing ones.
Where a trailing season is marked TBA or has a future premiere year, it has not
been released yet - make clear the user cannot acquire it, not that they simply
haven't done so.
Be concise - one or two sentences per series.
"""


def _compute_missing_seasons(
    series_list: list[SeriesSeasonInfo],
    external_seasons: dict[str, list[SeasonInfo]],
) -> list[MissingSeasonReport]:
    result = []
    for s in series_list:
        all_known = external_seasons.get(s.series_id)
        if not all_known:
            continue
        owned_set = set(s.owned_season_numbers)
        missing = [si for si in all_known if si.number not in owned_set]
        if not missing:
            continue
        if owned_set:
            last = max(owned_set)
            gap = [si for si in missing if si.number < last]
            trailing = [si for si in missing if si.number > last]
        else:
            gap, trailing = [], missing
        result.append(
            MissingSeasonReport(
                name=s.name,
                owned=sorted(owned_set),
                gap=gap,
                trailing=trailing,
            )
        )
    return sorted(result, key=lambda r: (not r.gap, r.name))


def _fmt_seasons(nums: list[int]) -> str:
    return ", ".join(str(n) for n in nums)


def _fmt_season(si: SeasonInfo) -> str:
    year = si.premiere_date[:4] if si.premiere_date else "TBA"
    return f"S{si.number} ({year})"


def _fmt_season_infos(seasons: list[SeasonInfo]) -> str:
    return ", ".join(_fmt_season(si) for si in seasons)


def _build_missing_seasons_input(report: list[MissingSeasonReport]) -> list[dict]:
    lines = [
        "",
        "SERIES WITH MISSING SEASONS:",
        "",
    ]
    for r in report:
        lines.append(r.name)
        lines.append(f"  Owned: {_fmt_seasons(r.owned) or '(none)'}")
        if r.gap:
            lines.append(f"  Gap (blocking run): {_fmt_season_infos(r.gap)}")
        if r.trailing:
            lines.append(f"  Trailing: {_fmt_season_infos(r.trailing)}")
        lines.append("")
    lines.append(f"Today's date: {workflow.now().date().isoformat()}")
    return [
        {
            "role": "user",
            "content": [{"type": "input_text", "text": "\n".join(lines)}],
        }
    ]


@workflow.defn
class MissingSeasonsWorkflow:
    @workflow.run
    async def run(self) -> str:
        series_list = await workflow.execute_activity(
            fetch_series_season_presence,
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
            retry_policy=_ACTIVITY_RETRY,
        )
        workflow.random().shuffle(series_list)
        sem = asyncio.Semaphore(3)

        async def _fetch(s: SeriesSeasonInfo) -> list[SeasonInfo]:
            async with sem:
                return await workflow.execute_activity(
                    fetch_external_seasons,
                    s,
                    activity_id=s.name,
                    start_to_close_timeout=_ACTIVITY_TIMEOUT,
                    retry_policy=_ACTIVITY_RETRY,
                )

        season_lists = await asyncio.gather(*[_fetch(s) for s in series_list])
        external_seasons = {
            s.series_id: seasons
            for s, seasons in zip(series_list, season_lists)
            if seasons
        }
        report = _compute_missing_seasons(series_list, external_seasons)
        if not report:
            return (
                "All series in your collection appear to have complete season coverage."
            )
        agent = Agent(
            name="Missing Seasons Assistant",
            instructions=_MISSING_SEASONS_SYSTEM_PROMPT,
            model=_MODEL,
        )
        result = await Runner.run(agent, input=_build_missing_seasons_input(report))
        return result.final_output


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


def _normalise(title: str) -> str:
    import re
    title = title.lower()
    title = re.sub(r'\(\d{4}\)\s*$', '', title)
    title = re.sub(r'[^a-z0-9 ]', '', title)
    return title.strip()


@dataclasses.dataclass
class DirectorGap:
    name: str
    owned: list[str]
    missing: list[FilmInfo]


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
