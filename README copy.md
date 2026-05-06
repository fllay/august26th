# Google-form-agent-NECTEC

LangChain Deep Agent for creating Google Forms through OpenRouter and the
Google Forms MCP server at <https://github.com/matteoantoci/google-forms-mcp>.

## Setup

Requirements:

- Python 3.11+
- Node.js 18+
- Docker Desktop with the Linux engine running, for the Docker workflow
- Docker Compose v2, available through `docker compose`
- A Google Cloud OAuth client with access to the Google Forms API
- An OpenRouter API key

Install this agent:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
```

The Google Forms MCP server is bundled in this repository under
`mcp/google-forms-mcp`. For local CLI runs, install and build it once:

```powershell
cd mcp\google-forms-mcp
npm install
npm run build
cd ..\..
```

Copy `.env.example` to `.env`, then fill in:

- `OPENROUTER_API_KEY`
- `OPENROUTER_MODEL`
- `GOOGLE_FORMS_MCP_PATH`
- `GOOGLE_CLIENT_ID`
- `GOOGLE_CLIENT_SECRET`
- `GOOGLE_REFRESH_TOKEN`

`GOOGLE_FORMS_MCP_PATH` should point to the bundled MCP server entrypoint:

```env
GOOGLE_FORMS_MCP_PATH=C:\Program Files (x86)\Git\Google-form-agent-NECTEC\mcp\google-forms-mcp\build\index.js
```

## Usage

Run the agent with a natural-language form request:

```powershell
google-form-agent "Create a workshop feedback form with name, email, session rating from 1 to 5, and open comments"
```

The agent connects to OpenRouter for reasoning and uses the Google Forms MCP
tools to create the form and add questions.

## Skills

This project includes Deep Agents skills under:

```text
skills/
```

The default skill is `skills/google-form-author/SKILL.md`. It teaches the agent
the Google Forms authoring workflow, including the required create-then-add
sequence for the Google Forms API. The backend loads this folder when building
the Deep Agent, and the Docker image copies it into `/app/skills`.

## Docker

Docker requirements:

- Docker Desktop must be installed and running.
- Docker must be using the Linux container engine.
- `docker compose version` must work in your terminal.

Run the LangGraph backend and the Agent Chat UI together:

```powershell
docker compose up --build
```

Then open:

```text
http://localhost:3000
```

The Docker setup starts:

- Backend LangGraph server at `http://localhost:2024`
- Web UI at `http://localhost:3000`
- Assistant / graph id: `agent`

The backend image copies and builds the bundled `mcp/google-forms-mcp` server
during the Docker build. Keep your real OpenRouter and Google OAuth values in
`.env`; the compose file injects them into the backend container. In Docker,
`GOOGLE_FORMS_MCP_PATH` is overridden to:

```text
/opt/google-forms-mcp/build/index.js
```

## Configuration

OpenRouter uses an OpenAI-compatible endpoint:

- Base URL: `https://openrouter.ai/api/v1`
- Model: set with `OPENROUTER_MODEL`, for example `openai/gpt-4.1`

The MCP server is started over stdio with:

```text
node %GOOGLE_FORMS_MCP_PATH%
```
