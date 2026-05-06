"""LangGraph server entrypoint for the Google Forms agent."""

from __future__ import annotations

from typing import Any

from google_form_agent.agent import build_agent


async def agent() -> Any:
    """Factory used by LangGraph Server for graph id `agent`."""
    return await build_agent()
