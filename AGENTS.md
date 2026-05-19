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
- For public/domain deployments, do not route the LangGraph backend directly on the same `/api` path that the Next.js app uses for its own API routes and Google OAuth callback. Keep the Next.js app at the main hostname and expose LangGraph either on a separate hostname or a distinct non-conflicting path, then point `WEBUI_PUBLIC_API_URL` at that public browser-reachable endpoint.
- Shared user-account Google OAuth is stored in the shared token file configured by `GOOGLE_OAUTH_TOKEN_PATH`; backend Workspace credentials prefer that token file over `GOOGLE_REFRESH_TOKEN`, but service-account credentials still take precedence if `SERVICE_ACCOUNT_PATH` is configured.
- The web UI "Disconnect Google" action only removes the shared OAuth token file. If `GOOGLE_REFRESH_TOKEN` or `SERVICE_ACCOUNT_PATH` is still configured, the backend can remain authenticated even though the UI looks disconnected.
- The web UI OAuth scope list must include the full `https://www.googleapis.com/auth/forms` scope, not only `forms.body`, because native Apps Script-based form-to-sheet linking checks for the broader Forms scope on the shared user token.
- The shared Apps Script runtime used for native form-to-sheet linking must be reachable by the authenticated user account and belong to the same standard Google Cloud project as the app's OAuth client. If a non-owner test account gets `Requested entity was not found`, treat it as a shared runtime/deployment accessibility or project-linking problem rather than a form-creation failure.
- The shared Apps Script project URL can be derived directly from `GOOGLE_APPS_SCRIPT_PROJECT_ID`; cross-account execution depends on the target user being able to access that shared script project and on `GOOGLE_APPS_SCRIPT_DEPLOYMENT_ID` still pointing at a live API executable deployment from that project.
- Google OAuth is now session-scoped in the web UI: each browser session stores its token under a separate file keyed by a `google_oauth_session` cookie, and the backend local direct-flow credential loaders honor that per-request session key.
- The Google OAuth disconnect flow must clear both the session-specific token file and the `google_oauth_session` cookie; otherwise reconnecting can silently reuse the same stale per-session auth context.
- For local direct Google workflows executed through `asyncio.to_thread`, bind the `google_oauth_session` key explicitly at the thread boundary instead of relying on implicit context propagation.
- For direct local tool invocations that create forms or invoke Apps Script, pass the `google_oauth_session_key` into the tool call itself and bind the context inside the tool body. Middleware-level context binding alone is not sufficient to guarantee the correct per-session token file is used all the way through response-sheet linking.
- Current limitation: the bundled `google-forms-mcp` stdio server still authenticates process-wide from environment variables at startup, so session isolation is reliable for the backend's local direct Google workflows but not guaranteed for any MCP code path that still depends on the MCP server's startup refresh token.
- When the server still reports `Shared Google OAuth token is missing Apps Script scopes` after the web UI scope list has already been updated, assume the running deployment is still using an older saved token file or was not rebuilt. Clear the saved OAuth token files for the active browser session, keep `GOOGLE_REFRESH_TOKEN` blank, rebuild, then reconnect Google.
- When the public domain changes, update all three places together: the Google OAuth client's authorized JavaScript origin, its redirect URI (`https://<domain>/api/google/oauth/callback`), and the app's `WEBUI_PUBLIC_API_URL`. After a domain change, reconnect Google from the new domain so the session-scoped token file is recreated for that origin/session.
- Run backend and web UI together with `docker compose up --build`; Docker Desktop must be running with the Linux engine.
- Current product workflow is:
  1. user asks the agent to create a form
  2. agent creates the Google Form
  3. agent automatically creates and links the response Google Spreadsheet through Apps Script
  4. agent immediately tries to post-process the linked response sheet
  5. user can still send the returned spreadsheet link back for reformatting or deeper analysis
- Native automatic Google Forms -> Sheets linking is part of the current flow when Apps Script runtime/scopes are configured.
- The backend now prefers deterministic shortcuts for obvious handoffs:
  - clear form-creation prompts can go straight into the local `create_form_with_response_sheet` tool path
  - pasted linked spreadsheet URLs can go straight into `format_response_sheet_for_analysis`
- For direct form creation, keep these behavioral rules aligned with the current backend:
  - respondent-information fields should be inferred from the user's prompt, not from uploaded reference documents
  - uploaded files may define the quiz/test questions and answer key, but should not invent extra participant fields
  - exact-source mode should prefer the uploaded source's real question count over any default generated count
- The form formatter currently produces a small set of post-processed tabs:
  - a cleaned wide `Processed Responses`-style sheet that stays close to the raw response layout
  - a long-form `Response Details` analysis sheet
  - a `Question Summary` sheet with grouped counts/percentages
- Immediate post-processing after linking should be best-effort:
  - if the response sheet is already usable, create the processed/analysis tabs right away
  - if the linked sheet is still empty or not ready, do not fail form creation; report that post-processing is waiting for first responses
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
