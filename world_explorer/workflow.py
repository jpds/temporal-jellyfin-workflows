from __future__ import annotations

import asyncio
import os
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    import annotated_types  # noqa: F401
    import pydantic
    import pydantic_core  # noqa: F401
    from agents import Agent, Runner

    from world_explorer.activities import (
        fetch_favorites,
        fetch_unwatched_movies,
        fetch_unwatched_series,
        fetch_watched_movies,
        fetch_watched_series,
    )

_MODEL = os.environ.get("RECOMMENDER_MODEL", "gpt-4o")


class _LanguageCode(pydantic.BaseModel):
    iso_codes: list[str] = pydantic.Field(
        description=(
            "ISO 639-2 three-letter codes. Single item for a language"
            " (e.g. ['jpn']). Multiple items for a country with a small set"
            " of official languages (e.g. ['fre', 'dut'] for 'Belgian')."
            " Empty list if the input is too ambiguous (more than ~3 languages)."
        )
    )
    canonical_names: list[str] = pydantic.Field(
        description=(
            "English names matching iso_codes (e.g. ['French', 'Dutch'])."
            " Empty list when iso_codes is empty."
        )
    )
    label: str = pydantic.Field(
        description=(
            "What the user is looking for, used in the discovery prompt."
            " Use the canonical language name for language inputs ('Japanese')."
            " Use the country adjective for country/nationality inputs"
            " ('Belgian', 'Indian'). Normalise raw ISO codes to the canonical"
            " name ('jpn' -> 'Japanese'). Always populate for any recognised"
            " input; empty string only when the input is unrecognised."
        )
    )
    suggestion: str = pydantic.Field(
        description=(
            "Populated only when iso_codes is empty: names the specific"
            " languages the user might have meant."
            " Empty string when iso_codes is populated."
        )
    )


_VALIDATION_SYSTEM_PROMPT = """
Resolve the user's input to one or more ISO 639-2 three-letter bibliographic codes.

For a specific language or unambiguous alias, return a single entry:
  "Farsi"    -> iso_codes=["per"], canonical_names=["Persian"]
  "Mandarin" -> iso_codes=["chi"], canonical_names=["Chinese"]
  "jpn"      -> iso_codes=["jpn"], canonical_names=["Japanese"]

For a country or nationality with a small set of official languages, return all:
  "Belgian"   -> iso_codes=["fre", "dut"], canonical_names=["French", "Dutch"]
  "Swiss"     -> iso_codes=["ger", "fre", "ita"],
                 canonical_names=["German", "French", "Italian"]
  "Brazilian" -> iso_codes=["por"], canonical_names=["Portuguese"]
  "Canadian"  -> iso_codes=["eng", "fre"], canonical_names=["English", "French"]

If the input maps to too many languages (more than three, e.g. "Indian"), return
empty lists and populate suggestion with specific languages the user might mean:
  "Indian" -> suggestion="Did you mean Hindi (hin), Tamil (tam),
  Bengali (ben), or Telugu (tel)?"

If the input is not a recognised language, country, or nationality, return empty
lists and an empty suggestion.
"""

_ACTIVITY_RETRY = RetryPolicy(
    maximum_attempts=5,
    initial_interval=timedelta(seconds=2),
)
_ACTIVITY_TIMEOUT = timedelta(seconds=30)

_WORLD_EXPLORER_SYSTEM_PROMPT = """
You are a world cinema and international TV recommendation assistant.

The user wants to explore content matching a target: a language ("Japanese"),
a nationality ("Belgian"), or a country ("Brazil"). You are given their full
Jellyfin library in labelled sections (content in all languages and origins):
- TARGET: the language, nationality, or country they want to explore
- FAVORITES: content they have marked as la creme de la creme
- WATCHED MOVIES / TV SERIES: titles they have watched
- UNWATCHED MOVIES / TV SERIES: titles they own but have not watched

Recommend 10 to 15 titles that match the target, mixing movies and TV series:
- For a language target (e.g. "Japanese"): content originally produced in that language
- For a nationality or country target (e.g. "Belgian", "Brazil"): content produced
  in that country, regardless of which language it is in

Include a blend of:
- Unwatched titles already in their library that match the target
- Fresh discoveries not yet in their library

Do not recommend titles the user has already watched or marked as a favourite.
Tailor suggestions to their tastes as inferred from their favourites and watch history.
Include a one-sentence description for each recommendation.
"""


def _section(label: str, items: list[str]) -> dict:
    return {
        "type": "input_text",
        "text": f"{label}:\n" + ("\n".join(items) or "(none)"),
    }


@workflow.defn
class WorldExplorerWorkflow:
    @workflow.run
    async def run(self, target: str) -> str:
        validation = await Runner.run(
            Agent(
                name="World Explorer Validator",
                instructions=_VALIDATION_SYSTEM_PROMPT,
                output_type=_LanguageCode,
                model=_MODEL,
            ),
            input=target,
        )
        lang = validation.final_output
        if not lang.label:
            return f"'{target}' is not a recognised language or country."

        (
            favorites,
            watched_movies,
            watched_series,
            unwatched_movies,
            unwatched_series,
        ) = await asyncio.gather(
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
        fav_set = set(favorites)
        result = await Runner.run(
            Agent(
                name="World Explorer Assistant",
                instructions=_WORLD_EXPLORER_SYSTEM_PROMPT,
                model=_MODEL,
            ),
            input=[
                {
                    "role": "user",
                    "content": [
                        _section("TARGET", [lang.label]),
                        _section("FAVORITES", sorted(favorites)),
                        _section(
                            "WATCHED MOVIES",
                            sorted(m for m in watched_movies if m not in fav_set),
                        ),
                        _section(
                            "WATCHED TV SERIES",
                            sorted(s for s in watched_series if s not in fav_set),
                        ),
                        _section("UNWATCHED MOVIES", sorted(unwatched_movies)),
                        _section("UNWATCHED TV SERIES", sorted(unwatched_series)),
                    ],
                }
            ],
        )
        return result.final_output
