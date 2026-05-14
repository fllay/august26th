# Codex Instructions

These instructions apply to this repository unless a more specific `AGENTS.md` exists in a subdirectory.

## Project Context

- Repository: `august26th`
- Purpose: LangChain Deep Agent that creates Google Forms and helps format linked Google Sheets response data for analysis.
- Primary language/framework: Python 3.11+ package using `deepagents`, `langchain-openai`, and `langchain-mcp-adapters`.
- Web UI: Next.js Agent Chat UI under `agent-chat-ui/`, connected to the LangGraph backend with assistant id `agent`.
- MCP server: bundled under `mcp/google-forms-mcp` and built locally or inside the backend Docker image.
- Deep Agents skills live under `skills/`; keep each skill in its own folder with a `SKILL.md` file.
- Keep durable project notes and project-local Codex instructions in this `AGENTS.md` file.

## Project Decisions

- Keep secrets in `.env`; commit only `.env.example`.
- Start the bundled Google Forms MCP server over stdio with Node and a configured `GOOGLE_FORMS_MCP_PATH`.
- Configure the LLM provider with `LLM_PROVIDER=openrouter` or `LLM_PROVIDER=local`.
- Configure OpenRouter fallbacks with `OPENROUTER_MODEL_2` and `OPENROUTER_MODEL_3`.
- Configure local OpenAI-compatible LLM servers with `LOCAL_LLM_BASE_URL`, `LOCAL_LLM_MODEL`, and optional `LOCAL_LLM_API_KEY`.
- For local text-only LLMs, uploaded PDF/DOC/DOCX files are converted to text before model calls; image uploads are retained as attachment context.
- Uploaded DOCX files are treated as structured source material, not just loose reference text:
  - embedded images are extracted and preserved in document order
  - image-only paragraphs can be mapped to the following image-based question when the question title explicitly refers to an image
  - multiple images inside one answer choice may be merged into a single composite image because Google Forms only supports one native image per option
- Configure the Web UI backend URLs through `.env` with `WEBUI_PUBLIC_API_URL` and `WEBUI_LANGGRAPH_API_URL`.
- Run backend and web UI together with `docker compose up --build`; Docker Desktop must be running with the Linux engine.
- Current product workflow is:
  1. user asks the agent to create a form
  2. agent creates the Google Form
  3. agent automatically creates and links the response Google Spreadsheet through Apps Script
  4. user can send the returned spreadsheet link back
  5. agent formats the linked response sheet into analysis-oriented tabs
- Native automatic Google Forms -> Sheets linking is part of the current flow when Apps Script runtime/scopes are configured.
- The backend now prefers deterministic shortcuts for obvious handoffs:
  - clear form-creation prompts can go straight into the local `create_form_with_response_sheet` tool path
  - pasted linked spreadsheet URLs can go straight into `format_response_sheet_for_analysis`
- For direct form creation, keep these behavioral rules aligned with the current backend:
  - respondent-information fields should be inferred from the user's prompt, not from uploaded reference documents
  - uploaded files may define the quiz/test questions and answer key, but should not invent extra participant fields
  - exact-source mode should prefer the uploaded source's real question count over any default generated count
- The form formatter should produce analysis-oriented output, not just a cleaned copy of the raw response tab.
- The agent should support simple natural prompts, especially short Thai prompts, without requiring heavily structured instructions.
- The agent should reply in the user's language when practical; Thai users should get Thai-facing responses and English users should get English-facing responses.
- Quiz behavior should be inferred from user intent/context, not only from the presence of answer keys:
  - obvious quiz/test/pre-test/post-test/exam wording should enable quiz mode
  - feedback/registration/survey contexts should stay non-quiz unless the user explicitly asks otherwise
  - if an uploaded source includes answer signals and the request is clearly a test, the generated form should be created as a quiz with grading metadata
- Current DOCX answer-key detection supports several common answer signals:
  - highlighted option text
  - shaded option text/cells
  - non-default colored option text commonly used to mark correct answers
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
- For backend changes, prefer `python -m compileall src`.
- For Web UI changes, prefer `npm run build` in `agent-chat-ui/`.

## Handoff Notes

- Before finishing a task, summarize changed files and any verification performed.
- If assumptions were made, record stable ones in this file only when they are likely to remain useful.
