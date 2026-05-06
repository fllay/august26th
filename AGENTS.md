# Codex Instructions

These instructions apply to this repository unless a more specific `AGENTS.md` exists in a subdirectory.

## Project Context

- Repository: `Google-form-agent-NECTEC`
- Purpose: LangChain Deep Agent that creates Google Forms using OpenRouter as the LLM API and `matteoantoci/google-forms-mcp` as the Google Forms MCP server.
- Primary language/framework: Python 3.11+ package using `deepagents`, `langchain-openai`, and `langchain-mcp-adapters`.
- Web UI: Next.js Agent Chat UI under `agent-chat-ui/`, connected to the LangGraph backend with assistant id `agent`.
- MCP server: bundled under `mcp/google-forms-mcp` and built locally or inside the backend Docker image.
- Deep Agents skills live under `skills/`; keep each skill in its own folder with a `SKILL.md` file.
- Keep durable project notes and project-local Codex instructions in this `AGENTS.md` file.

## Project Decisions

- Keep secrets in `.env`; commit only `.env.example`.
- Start the bundled Google Forms MCP server over stdio with Node and a configured `GOOGLE_FORMS_MCP_PATH`.
- Configure OpenRouter fallbacks with `OPENROUTER_MODEL_2` and `OPENROUTER_MODEL_3`.
- Configure the Web UI backend URLs through `.env` with `WEBUI_PUBLIC_API_URL` and `WEBUI_LANGGRAPH_API_URL`.
- Run backend and web UI together with `docker compose up --build`; Docker Desktop must be running with the Linux engine.
- Deployment target is not documented yet.

## Working Style

- Prefer small, focused changes that are easy to review.
- Inspect existing files before editing so user work is not overwritten.
- Do not revert or discard changes unless the user explicitly asks.
- Use `rg` for searching when available.
- Keep documentation concise and practical.

## Verification

- When code or configuration is added later, run the relevant checks or tests if available.
- If no automated checks exist, state what was inspected manually.

## Handoff Notes

- Before finishing a task, summarize changed files and any verification performed.
- If assumptions were made, record stable ones in this file only when they are likely to remain useful.
