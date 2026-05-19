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
- Configure the Web UI backend through `.env` with `WEBUI_LANGGRAPH_API_URL`, and set `WEBUI_APP_URL` on server/domain deployments for Google OAuth callback stability.
- For server or reverse-proxy deployments, set `WEBUI_APP_URL` to the public Next.js origin (for example `https://forms.example.com`) so Google OAuth redirect and callback URLs do not depend on proxy-forwarded host headers.
- The web UI browser client now defaults to the same-origin Next.js `/api` proxy for LangGraph traffic instead of requiring a separate `WEBUI_PUBLIC_API_URL`.
- When the web UI uses the same-origin `/api` proxy, normalize that relative path to an absolute browser URL before passing it into the LangGraph SDK client or React stream hook; those SDK entry points expect absolute URLs and can throw `Failed to construct 'URL': Invalid URL` on plain `/api`.
- Shared user-account Google OAuth is stored in the shared token file configured by `GOOGLE_OAUTH_TOKEN_PATH`; backend Workspace credentials prefer that token file over `GOOGLE_REFRESH_TOKEN`, but service-account credentials still take precedence if `SERVICE_ACCOUNT_PATH` is configured.
- Google Form ownership always follows the credential principal that actually executes the Forms API `forms.create` call. If a user on the server is not the owner of the created form, check auth-source precedence first: `SERVICE_ACCOUNT_PATH` overrides user OAuth, then the active OAuth token file, then `GOOGLE_REFRESH_TOKEN`.
- The web UI "Disconnect Google" action only removes the shared OAuth token file. If `GOOGLE_REFRESH_TOKEN` or `SERVICE_ACCOUNT_PATH` is still configured, the backend can remain authenticated even though the UI looks disconnected.
- The web UI OAuth scope list must include the full `https://www.googleapis.com/auth/forms` scope, not only `forms.body`, because native Apps Script-based form-to-sheet linking checks for the broader Forms scope on the shared user token.
- The shared Apps Script runtime used for native form-to-sheet linking must be reachable by the authenticated user account and belong to the same standard Google Cloud project as the app's OAuth client. If a non-owner test account gets `Requested entity was not found`, treat it as a shared runtime/deployment accessibility or project-linking problem rather than a form-creation failure.
- The shared Apps Script project URL can be derived directly from `GOOGLE_APPS_SCRIPT_PROJECT_ID`; cross-account execution depends on the target user being able to access that shared script project and on `GOOGLE_APPS_SCRIPT_DEPLOYMENT_ID` still pointing at a live API executable deployment from that project.
- Shared API-executable Apps Script deployments used across multiple Google accounts must be deployed with `executionApi.access = ANYONE`; `MYSELF` causes non-owner users to hit `Requested entity was not found` even when the project and deployment ids are otherwise valid.
- Google OAuth is now session-scoped in the web UI: each browser session stores its token under a separate file keyed by a `google_oauth_session` cookie, and the backend local direct-flow credential loaders honor that per-request session key.
- The Google OAuth disconnect flow must clear both the session-specific token file and the `google_oauth_session` cookie; otherwise reconnecting can silently reuse the same stale per-session auth context.
- For local direct Google workflows executed through `asyncio.to_thread`, bind the `google_oauth_session` key explicitly at the thread boundary instead of relying on implicit context propagation.
- For direct local tool invocations that create forms or invoke Apps Script, pass the `google_oauth_session_key` into the tool call itself and bind the context inside the tool body. Middleware-level context binding alone is not sufficient to guarantee the correct per-session token file is used all the way through response-sheet linking.
- When reading `google_oauth_session_key` from a LangGraph model request, do not assume a single request-state layout. Search `runtime.context`, `state`, and similar nested request containers recursively because middleware and stream envelopes can move the context object.
- If backend credential loading has no explicit `google_oauth_session_key`, prefer the legacy shared token file when present; otherwise, when exactly one file exists under `/data/google-oauth-sessions`, treat that single session token as the effective shared token instead of failing immediately.
- Do not scan `/data/google-oauth-sessions` during graph or MCP-client startup on the event loop. The single-session fallback is request-time credential logic only; startup code should use env credentials or the explicit shared token path to avoid LangGraph blocking-call errors.
- Do not rely on saved OAuth scope metadata as a hard gate before Apps Script operations. The authoritative check is whether the actual refreshed credentials can call the Apps Script API successfully for the current session token.
- For native Apps Script execution, refresh the shared script project content/deployment before running and call the Execution API with the Apps Script `scriptId`, not the deployment id. Otherwise the runtime can execute an older deployed manifest and surface stale authorization failures such as missing `FormApp.openById` permissions.
- If `GOOGLE_APPS_SCRIPT_PROJECT_ID` and `GOOGLE_APPS_SCRIPT_DEPLOYMENT_ID` are explicitly configured, treat that runtime as a shared managed deployment and do not attempt to update or redeploy it using each end user's OAuth credentials. Only auto-create or auto-deploy a runtime when no shared runtime has been configured.
- Native spreadsheet linking and Apps Script image insertion should follow the same fallback policy: a failed shared web-app or shared API-executable attempt must not short-circuit the managed fallback runtime, and transient `Requested entity was not found` linker failures should be retried before the flow is marked failed.
- If a configured shared Apps Script runtime fails with cross-account execution errors such as `Requested entity was not found`, `AuthRequiredError`, or insufficient authentication scopes, fall back to a locally managed per-user Apps Script runtime before declaring automatic linking unavailable.
- Form creation must degrade cleanly when native response-sheet linking is unavailable. If the form and spreadsheet are created but the shared Apps Script linker runtime fails, return both resources plus the link failure details instead of aborting the whole user flow.
- Apply the same degrade-and-fallback rule to Apps Script-based form image insertion: try the shared runtime, then the per-user managed runtime, and if both fail keep the form creation result instead of aborting the whole run.
- Current limitation: the bundled `google-forms-mcp` stdio server still authenticates process-wide from environment variables at startup, so session isolation is reliable for the backend's local direct Google workflows but not guaranteed for any MCP code path that still depends on the MCP server's startup refresh token.
- When the server still reports `Shared Google OAuth token is missing Apps Script scopes` after the web UI scope list has already been updated, assume the running deployment is still using an older saved token file or was not rebuilt. Clear the saved OAuth token files for the active browser session, keep `GOOGLE_REFRESH_TOKEN` blank, rebuild, then reconnect Google.
- When the public domain changes, update `WEBUI_APP_URL` and the Google OAuth client's authorized JavaScript origin plus redirect URI (`https://<domain>/api/google/oauth/callback`). After a domain change, reconnect Google from the new domain so the session-scoped token file is recreated for that origin/session.
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

