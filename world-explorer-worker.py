#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import os
import signal
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from agents import (
    set_default_openai_api,
    set_default_openai_client,
    set_tracing_disabled,
)
from openai import AsyncOpenAI
from temporalio.client import Client
from temporalio.contrib.openai_agents import ModelActivityParameters, OpenAIAgentsPlugin
from temporalio.worker import Worker

from world_explorer.activities import (
    fetch_favorites,
    fetch_unwatched_movies,
    fetch_unwatched_series,
    fetch_watched_movies,
    fetch_watched_series,
)
from world_explorer.workflow import WorldExplorerWorkflow

set_default_openai_client(
    AsyncOpenAI(
        base_url=os.environ.get("OPENAI_BASE_URL"),
        api_key=os.environ.get("OPENAI_API_KEY", "not-needed"),
    ),
    use_for_tracing=False,
)
set_default_openai_api("chat_completions")
set_tracing_disabled(True)


async def main() -> None:
    shutdown = asyncio.Event()
    loop = asyncio.get_event_loop()
    loop.add_signal_handler(signal.SIGTERM, shutdown.set)
    loop.add_signal_handler(signal.SIGINT, shutdown.set)

    client = await Client.connect(
        os.environ.get("TEMPORAL_ADDRESS", "localhost:7233"),
        namespace=os.environ.get("TEMPORAL_NAMESPACE", "default"),
        plugins=[
            OpenAIAgentsPlugin(
                model_params=ModelActivityParameters(
                    start_to_close_timeout=timedelta(minutes=10),
                    heartbeat_timeout=timedelta(seconds=30),
                ),
            ),
        ],
    )

    async with Worker(
        client,
        task_queue=os.environ.get("TEMPORAL_TASK_QUEUE", "world-explorer-queue"),
        workflows=[WorldExplorerWorkflow],
        activities=[
            fetch_favorites,
            fetch_watched_movies,
            fetch_watched_series,
            fetch_unwatched_movies,
            fetch_unwatched_series,
        ],
        activity_executor=ThreadPoolExecutor(max_workers=4),
    ):
        await shutdown.wait()


asyncio.run(main())
