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
- Agent-managed Google Form response data can be mirrored into a SQL store. The current backend implementation uses Postgres via `FORM_RESPONSE_PG_CONN_STR` or fallback `PG_CONN_STR`. In Docker Compose, the repo now includes a `postgres` service and the backend/web UI can share that same database.
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
- For first-pass spreadsheet analysis requests in chat, the backend may attach a hidden `<<<SPREADSHEET_ANALYSIS_VISUAL>>>...<<<END_SPREADSHEET_ANALYSIS_VISUAL>>>` JSON block to the assistant message. The web UI should strip that block from visible markdown and render it as an inline chart panel instead of showing raw JSON.
- For chat-side spreadsheet graphs, let the backend choose both the topics worth plotting and the chart type, but do not add a second LLM round-trip just for chart selection inside the spreadsheet-analysis shortcut. Use deterministic data-analysis heuristics to rank questions, skip weak topics, and explain the choice briefly so analysis stays responsive when the main model provider is slow.
- For first-pass spreadsheet dashboards, keep the chart selector conservative: prefer at most a few strong topics, and skip administrative fields, fact-recall style questions, overwhelmingly one-sided binary splits, highly dominant distributions, and over-fragmented answer sets even if they are technically chartable.
- If direct question-level topics are not useful enough for a first-pass dashboard, synthesize a small number of derived analysis topics from the response distributions themselves, such as "most split questions" or "strongest consensus questions", instead of forcing weak raw-question charts into the UI.
- For broad spreadsheet analysis, prefer domain-specific synthesized topics when repeated domain markers can be inferred from the question text itself (for example AI, LoRa, IoT, KidBright, networking, programming). Use those inferred domains to summarize where confusion or agreement clusters, instead of only surfacing generic disagreement/consensus charts.
- For deeper spreadsheet analysis in chat, derive compact insight cards from the processed response sheet as well as charts. Prefer structural findings such as strongest disagreement, strongest consensus, and useful respondent composition segments over shallow restatements of single-answer counts.
- Do not default to raw per-question charts in the chat dashboard, and do not chart question titles against derived percentages as if they were natural dashboard metrics. Prefer overall distributions such as score distribution or respondent composition, and only surface raw question charts when the user explicitly asks for per-question views. Render each chart as its own analysis section with a dedicated description instead of a grouped mini-chart gallery.
- Do not show internal chart-selection guidance or raw request-focus text in the user-facing chart UI. The web UI should derive a concise reader-facing narrative from the plotted data itself, and the chart panels should be large enough to read without feeling like thumbnails.
- The chart panel should follow the user's language, not just the spreadsheet contents. Pass the inferred user language through the spreadsheet-analysis payload and localize chart headings, counts, and fallback narratives from that payload. When generating fallback chart narratives, rank series by actual value rather than trusting input order.
- Do not force an auto-generated fallback sentence under every chart. If a chart does not have a trustworthy explicit summary from the backend, omit the narrative line rather than showing brittle derived prose in the UI.
- The spreadsheet-analysis chart UI should use an explicit multi-color palette for bars and pie slices instead of relying only on theme chart variables, so each category remains visually distinct even when the active theme compresses the default chart colors.
- In the thread header, keep the connected Google avatar pinned as the rightmost control and size it on the same icon-button grid as the neighboring header actions so it aligns cleanly with tools like "New thread".
- Keep the connected Google avatar anchored to a fixed top-right header position across thread states. Other header actions may appear or disappear to its left, but they should not shift the avatar's screen position.
- The overall chat dashboard should always prefer at least one genuinely aggregate chart when possible. If score or segment charts are unavailable, fall back to an overall agreement-profile chart across questions rather than dropping to zero charts or reviving low-value per-question title charts.
- Shared user-account Google OAuth is stored in the shared token file configured by `GOOGLE_OAUTH_TOKEN_PATH`; backend Workspace credentials prefer that token file over `GOOGLE_REFRESH_TOKEN`, but service-account credentials still take precedence if `SERVICE_ACCOUNT_PATH` is configured.
- Google Form ownership always follows the credential principal that actually executes the Forms API `forms.create` call. If a user on the server is not the owner of the created form, check auth-source precedence first: `SERVICE_ACCOUNT_PATH` overrides user OAuth, then the active OAuth token file, then `GOOGLE_REFRESH_TOKEN`.
- The web UI "Disconnect Google" action only removes the shared OAuth token file. If `GOOGLE_REFRESH_TOKEN` or `SERVICE_ACCOUNT_PATH` is still configured, the backend can remain authenticated even though the UI looks disconnected.
- The web UI OAuth scope list must include the full `https://www.googleapis.com/auth/forms` scope, not only `forms.body`, because native Apps Script-based form-to-sheet linking checks for the broader Forms scope on the shared user token.
- The web UI Google OAuth flow may also request `openid`, `userinfo.email`, and `userinfo.profile` so the UI can show the connected Google account's avatar/name/email after sign-in. Persist that basic profile data alongside the session-scoped token and return it from the OAuth status route rather than re-fetching it on every page load.
- In the chat header, the connected Google account control should collapse to an avatar-only trigger. Clicking that avatar should open a small account popover with the stored Google profile details and the disconnect action; do not disconnect on the first click anymore.
- The shared Apps Script runtime used for native form-to-sheet linking must be reachable by the authenticated user account and belong to the same standard Google Cloud project as the app's OAuth client. If a non-owner test account gets `Requested entity was not found`, treat it as a shared runtime/deployment accessibility or project-linking problem rather than a form-creation failure.
- The shared Apps Script project URL can be derived directly from `GOOGLE_APPS_SCRIPT_PROJECT_ID`; cross-account execution depends on the target user being able to access that shared script project and on `GOOGLE_APPS_SCRIPT_DEPLOYMENT_ID` still pointing at a live API executable deployment from that project.
- Shared API-executable Apps Script deployments used across multiple Google accounts must be deployed with `executionApi.access = ANYONE`; `MYSELF` causes non-owner users to hit `Requested entity was not found` even when the project and deployment ids are otherwise valid.
- Google OAuth is now session-scoped in the web UI: each browser session stores its token under a separate file keyed by a `google_oauth_session` cookie, and the backend local direct-flow credential loaders honor that per-request session key.
- The Google OAuth disconnect flow must clear both the session-specific token file and the `google_oauth_session` cookie; otherwise reconnecting can silently reuse the same stale per-session auth context.
- For local direct Google workflows executed through `asyncio.to_thread`, bind the `google_oauth_session` key explicitly at the thread boundary instead of relying on implicit context propagation.
- For direct local tool invocations that create forms or invoke Apps Script, pass the `google_oauth_session_key` into the tool call itself and bind the context inside the tool body. Middleware-level context binding alone is not sufficient to guarantee the correct per-session token file is used all the way through response-sheet linking.
- When reading `google_oauth_session_key` from a LangGraph model request, do not assume a single request-state layout. Search `runtime.context`, `state`, and similar nested request containers recursively because middleware and stream envelopes can move the context object.
- For browser-session Google auth, send `google_oauth_session_key` redundantly in request `context`, `metadata`, and `config.configurable`. Different LangGraph/middleware layers may preserve different containers, and multi-browser session routing must not depend on only one of them surviving.
- Forms and spreadsheets should still be created with each browser user's own OAuth session, but native linking and Apps Script image insertion should run only through the configured shared runtime or shared web-app path. Do not fall back to auto-created per-user managed Apps Script runtimes in this deployment model.
- In the shared-runtime-only deployment model, the active linker may read the top-level shared `scriptId` and `deploymentId` persisted in `.data/google-apps-script.json`, but it must never treat `managedSessions`, `managedScriptId`, or other per-user fallback metadata as the shared runtime.
- The Apps Script `linkFormToSheet` helper should retry both `FormApp.openById()` and `setDestination()` for a short window. Newly created forms and spreadsheets can take a moment to become fully available, and image insertion already depends on that retry behavior.
- The shared Apps Script runtime/web-app path is the only supported automatic-linking path for this deployment. Keep failure reporting explicit when that shared runtime is unreachable or misconfigured.
- Before declaring native linking failed, verify the form's current destination directly from Apps Script. Newly created forms can produce ambiguous immediate link responses; a follow-up destination check is the authoritative success signal.
- After creating or redeploying an Apps Script runtime, do not execute it immediately. Poll a lightweight `ping` function until the Execution API can actually run the deployment, otherwise newly created managed runtimes can fail with transient `Requested entity was not found`.
- Persist the actual linker runtime mode on failures as well as successes. A saved `linkMode` of `api-executable` is not enough to debug browser-session linker issues; distinguish shared-runtime and web-app paths in failure records.
- If backend credential loading has no explicit `google_oauth_session_key`, prefer the legacy shared token file when present; otherwise, when exactly one file exists under `/data/google-oauth-sessions`, treat that single session token as the effective shared token instead of failing immediately.
- Do not scan `/data/google-oauth-sessions` during graph or MCP-client startup on the event loop. The single-session fallback is request-time credential logic only; startup code should use env credentials or the explicit shared token path to avoid LangGraph blocking-call errors.
- Do not rely on saved OAuth scope metadata as a hard gate before Apps Script operations. The authoritative check is whether the actual refreshed credentials can call the Apps Script API successfully for the current session token.
- For native Apps Script execution, refresh the shared script project content/deployment before running, but call the Execution API `scripts.run` with the API-executable `deploymentId` path parameter. The Apps Script project `scriptId` is for project/deployment management, not for the `scripts.run` execution target.
- If `GOOGLE_APPS_SCRIPT_PROJECT_ID` and `GOOGLE_APPS_SCRIPT_DEPLOYMENT_ID` are explicitly configured, treat that runtime as a shared managed deployment and do not attempt to update or redeploy it using each end user's OAuth credentials. In the shared-runtime-only deployment model, shared runtime may come from those env vars or the top-level persisted shared `scriptId`/`deploymentId`, but do not auto-create or auto-deploy a per-user runtime from end-user OAuth when the shared runtime is missing.
- Native spreadsheet linking and Apps Script image insertion should follow the same shared-runtime fallback policy: a failed shared web-app attempt must not short-circuit the shared API-executable path, and transient `Requested entity was not found` linker failures should be retried before the flow is marked failed.
- Google Forms API `forms.create` can return transient upstream errors such as HTTP 502. Retry boundedly before treating form creation as failed; this is separate from Apps Script linking issues.
- Keep Sheets post-processing on the same Google Workspace credential set used by form creation and spreadsheet creation. Mixing a narrower Sheets-only refresh path with the broader workspace token can trigger `invalid_scope` during immediate postprocess even when create/link already succeeded.
- Form creation must degrade cleanly when native response-sheet linking is unavailable. If the form and spreadsheet are created but the shared Apps Script linker runtime fails, return both resources plus the link failure details instead of aborting the whole user flow.
- Apply the same degrade rule to Apps Script-based form image insertion: keep the form creation result instead of aborting the whole run when the shared runtime cannot insert some images.
- For Google Forms REST `batchUpdate`, never pass oversized inline image URIs. If an embedded image does not materialize to a short HTTP(S) `sourceUri` (for example Drive upload failed and only a long data URI remains), drop that image from the REST payload instead of failing the whole form with `URI fields are limited to 2048 characters`.
- Current limitation: the bundled `google-forms-mcp` stdio server still authenticates process-wide from environment variables at startup, so session isolation is reliable for the backend's local direct Google workflows but not guaranteed for any MCP code path that still depends on the MCP server's startup refresh token.
- When the server still reports `Shared Google OAuth token is missing Apps Script scopes` after the web UI scope list has already been updated, assume the running deployment is still using an older saved token file or was not rebuilt. Clear the saved OAuth token files for the active browser session, keep `GOOGLE_REFRESH_TOKEN` blank, rebuild, then reconnect Google.
- When the public domain changes, update `WEBUI_APP_URL` and the Google OAuth client's authorized JavaScript origin plus redirect URI (`https://<domain>/api/google/oauth/callback`). After a domain change, reconnect Google from the new domain so the session-scoped token file is recreated for that origin/session.
- Run backend and web UI together with `docker compose up --build`; Docker Desktop must be running with the Linux engine.
- Thread history and LangGraph checkpoints for the backend should persist under `/app/.langgraph_api`. In Docker Compose, mount that path to a named volume (for example `langgraph_runtime_data`) so thread history survives backend container restarts. Be aware that `docker compose down -v` will remove that history.
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
  - do not synthesize a form description from inferred topic, quiz type, or question count; only use an explicit description if the user actually provided one
  - generated form titles should normalize duplicated test/survey prefixes and should not keep respondent-field phrases such as `โดยมีชื่อ หน่วยงาน เบอร์โทร อีเมล` in the final title
  - apply final title cleanup to the completed generated title itself, not just the inferred topic, so duplicated prefixes and respondent-field suffixes are removed even when they survive earlier parsing
  - if the user did not provide an explicit `Title: ...`, let the agent choose the actual form title from the brief and extracted question context, with the old heuristic title extractor kept only as fallback
- The form formatter currently produces a small set of post-processed tabs:
  - a cleaned wide `Processed Responses`-style sheet that adds helper columns such as `Response ID`, answered-question count, and completion rate before the metadata/question columns
  - a `Question Summary` sheet with grouped counts/percentages
- The legacy `Response Details` tab is no longer part of the active formatter output. Reformatting should delete that generated tab if it already exists from an older run.
- Keep `_initialize_empty_postprocess_tabs(...)` self-contained: the helper must write the empty processed/summary sheets and return the formatted payload itself. Do not leave any of that return/write logic inside `_delete_sheet_if_exists(...)`, or spreadsheet-analysis shortcuts can fail with runtime `NameError` on `summary_sheet_name`.
- In post-processed wide sheets, cohort/session and score-like columns (for example `รุ่น`, `รุ่นการอบรม`, `คะแนน`, `score`) should be classified as metadata/helper fields, not as answer/question columns.
- For broad spreadsheet-analysis requests that already include a spreadsheet target, prefer a deterministic first-pass response built from the post-processed `Question Summary` sheet so the chat can show charts without depending on the model to emit chart JSON correctly.
- For broad spreadsheet-analysis requests, create the post-process tabs only the first time for a workbook. On later analysis requests, detect and reuse the existing processed/summary tabs instead of reformatting again unless the user explicitly asks to format or reformat.
- Treat `Processed Responses` / `คำตอบที่จัดรูปแบบ` tabs as generated analysis tabs when classifying workbook sheets. The raw-source picker must never choose those tabs as the source sheet for reuse detection or reformatting decisions.
- Response-store sync is best-effort, not a webhook listener. The backend should register agent-created forms immediately, then refresh the Postgres response store when it creates the form or later revisits the linked spreadsheet for formatting or analysis. Do not let SQL sync failures abort form creation or spreadsheet analysis.
- The backend now exposes direct Postgres response-store tools for the agent. Use `inspect_form_response_database` for schema discovery and `query_form_response_database` for read-only SQL over `agent_forms`, `form_responses`, and `form_response_answers` when the user asks database questions.
- For obvious Postgres data requests such as listing stored forms or showing recent responses, prefer the local database shortcut so the agent returns actual rows instead of explaining the schema first. Reserve schema inspection for explicit structure/table/column questions.
- Read-only SQL validation for the Postgres response-store must match actual SQL keywords, not raw substrings, so legitimate column names such as `updated_at` are not blocked as if they were write statements.
- The backend also runs a lightweight background polling worker for the Postgres response store. While the service is up, agent-managed forms should be re-synced on a configurable interval (`FORM_RESPONSE_SYNC_INTERVAL_SECONDS`) so newly submitted answers appear in SQL without waiting for the next analysis request.
- Background Google response-sync workers must re-bind the original browser OAuth session key for each agent-managed form before calling the Forms API. If the worker falls back to the shared/default token path, it can hit `403 PERMISSION_DENIED` on user-owned forms even though creation and manual analysis work in the browser session.
- For agent-managed forms created before session-key persistence was added, spreadsheet-triggered sync must fall back to the current browser OAuth session key instead of overwriting it with `None`. A successful manual analysis/format run should backfill that session key into the local form registry so later background sync cycles can use the correct Google identity.
- When a user message includes a spreadsheet target plus an explicit formatting or sheet-maintenance intent (`format`, `reformat`, `prepare`, `จัดรูปแบบ`, `เตรียมวิเคราะห์`, `remove tab`, `remove sheet`, `ลบชีต`), route it through the local spreadsheet-format shortcut immediately instead of falling through to the main model.
- Spreadsheet-analysis intent detection must recognize Thai analysis wording as well as English (`วิเคราะห์`, `สรุป`, `กราฟ`, `ชาร์ต`, `ข้อมูล`, `ชีต`, `สเปรดชีต`, `เปรียบเทียบ`). If a spreadsheet target is present and the remaining request is empty, prefer the local first-pass analysis shortcut unless there is explicit formatting/maintenance intent.
- Set `FilesystemBackend(..., virtual_mode=False)` explicitly in the deep-agent factory so startup logs do not emit the upcoming deepagents deprecation warning.
- Immediate post-processing after linking should be best-effort:
  - if the response sheet is already usable, create the processed/analysis tabs right away
  - after linking, prefer retrying long enough for the raw Google Form response header row to appear before accepting an empty-placeholder postprocess result
  - if the linked sheet is still empty or not ready, still create the analysis tabs with placeholder headers/notes instead of treating post-processing as failed
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
- In successful form-creation replies, do not append extra generic handoff text telling the user to send the spreadsheet back later. Keep the result focused on the created links and concrete status lines; reserve next-step guidance for actual failure or recovery cases.

