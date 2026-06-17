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

    from missing_seasons.activities import (
        SeasonInfo,
        SeriesSeasonInfo,
        fetch_external_seasons,
        fetch_series_season_presence,
    )

_MODEL = os.environ.get("RECOMMENDER_MODEL", "gpt-4o")

_ACTIVITY_RETRY = RetryPolicy(
    maximum_attempts=5,
    initial_interval=timedelta(seconds=2),
)
_ACTIVITY_TIMEOUT = timedelta(seconds=30)

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


@dataclasses.dataclass
class MissingSeasonReport:
    name: str
    owned: list[int]
    gap: list[SeasonInfo]
    trailing: list[SeasonInfo]


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
