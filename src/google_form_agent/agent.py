"""LangChain Deep Agent wired to the Google Forms MCP server."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from deepagents import create_deep_agent
from deepagents.backends.filesystem import FilesystemBackend
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_openai import ChatOpenAI


OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
SKILLS_DIR = PROJECT_ROOT / "skills"


SYSTEM_PROMPT = """You are a Google Forms creation agent for NECTEC users.

Your job is to convert a user's form request into a clear Google Form using the
available Google Forms MCP tools. Work deliberately:
- Clarify only when required fields or question details are missing.
- Create the form first, then add questions one at a time.
- When calling create_form, pass only the form title. Do not pass a description,
  questions, settings, or other form fields in the create_form call because the
  Google Forms API only allows info.title during creation.
- Prefer concise, user-ready form titles, descriptions, and question labels.
- Use text questions for open responses and multiple choice questions when the
  user gives options.
- After creating or editing a form, report the form title, the questions added,
  and any URL or form ID returned by the tools.
- Never claim a form was created unless a Google Forms MCP tool succeeded.
"""


def get_required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def build_openrouter_model() -> ChatOpenAI:
    """Create a LangChain chat model that uses OpenRouter's OpenAI-compatible API."""
    api_key = get_required_env("OPENROUTER_API_KEY")
    model = os.getenv("OPENROUTER_MODEL", "openai/gpt-4.1")

    default_headers: dict[str, str] = {}
    if site_url := os.getenv("OPENROUTER_SITE_URL"):
        default_headers["HTTP-Referer"] = site_url
    if app_name := os.getenv("OPENROUTER_APP_NAME"):
        default_headers["X-Title"] = app_name

    return ChatOpenAI(
        api_key=api_key,
        base_url=OPENROUTER_BASE_URL,
        model=model,
        temperature=0.2,
        default_headers=default_headers or None,
        max_retries=3,
        disable_streaming=True,
    )


def build_mcp_client() -> MultiServerMCPClient:
    """Build the MCP client for the Google Forms stdio server."""
    server_path = Path(get_required_env("GOOGLE_FORMS_MCP_PATH")).expanduser()
    if not server_path.exists():
        raise RuntimeError(
            "GOOGLE_FORMS_MCP_PATH does not exist. Build google-forms-mcp and "
            f"set GOOGLE_FORMS_MCP_PATH to its build/index.js file: {server_path}"
        )

    return MultiServerMCPClient(
        {
            "google_forms": {
                "transport": "stdio",
                "command": "node",
                "args": [str(server_path)],
                "env": {
                    "GOOGLE_CLIENT_ID": get_required_env("GOOGLE_CLIENT_ID"),
                    "GOOGLE_CLIENT_SECRET": get_required_env("GOOGLE_CLIENT_SECRET"),
                    "GOOGLE_REFRESH_TOKEN": get_required_env("GOOGLE_REFRESH_TOKEN"),
                },
            }
        },
        tool_name_prefix=True,
    )


async def build_agent() -> Any:
    """Create the Deep Agent with Google Forms MCP tools."""
    model = build_openrouter_model()
    client = build_mcp_client()
    tools = await client.get_tools()

    return create_deep_agent(
        model=model,
        tools=tools,
        backend=FilesystemBackend(root_dir=str(PROJECT_ROOT)),
        skills=[SKILLS_DIR.as_posix()],
        system_prompt=SYSTEM_PROMPT,
    )
