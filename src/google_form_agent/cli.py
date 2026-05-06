"""Command-line interface for the Google Forms Deep Agent."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Iterable

from dotenv import load_dotenv
from langchain_core.messages import BaseMessage

from google_form_agent.agent import build_agent


def format_messages(messages: Iterable[BaseMessage]) -> str:
    for message in reversed(list(messages)):
        if message.type == "ai" and message.content:
            return str(message.content)
    return "The agent finished without a final text response."


async def run_agent(prompt: str) -> str:
    load_dotenv()
    agent = await build_agent()
    result = await agent.ainvoke({"messages": [{"role": "user", "content": prompt}]})
    return format_messages(result["messages"])


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create Google Forms with a LangChain Deep Agent and Google Forms MCP."
    )
    parser.add_argument(
        "prompt",
        nargs="+",
        help="The form request, for example: create a workshop feedback form...",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    prompt = " ".join(args.prompt)
    print(asyncio.run(run_agent(prompt)))


if __name__ == "__main__":
    main()
