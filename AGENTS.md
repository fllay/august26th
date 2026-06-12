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
- Docker Compose can optionally run `cloudflared` alongside the stack via `CLOUDFLARED_TUNNEL_TOKEN`. The current compose wiring shares the `webui` network namespace so existing tunnel ingress targets that point at `http://localhost:3000` continue to resolve without changing the Cloudflare side.
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
- The web UI must persist Google OAuth tokens to both the session-scoped file under `google-oauth-sessions/` and the canonical shared token file at `GOOGLE_OAUTH_TOKEN_PATH`. The session file supports per-user request flows, while the shared file lets backend graph/MCP startup resolve a stable credential even before request-scoped session context exists.
- For `ENABLE_GOOGLE_SHEETS_MCP=true`, bootstrap `mcp-google-sheets` with the active or lone session-scoped shared OAuth token when available, not only the legacy root token path. When no explicit Sheets credential file/config is set, write a real OAuth client credentials file from `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` and pass it as `CREDENTIALS_PATH`; do not send OAuth client JSON through `CREDENTIALS_CONFIG`, because the upstream server parses that field as service-account info.
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
- When the user explicitly asks to link or sync a form into the database, handle that as its own local shortcut. Resolve the target form from an explicit `form_id` first, then from the most recent form id mentioned earlier in the same thread, and run the existing Postgres response-store sync path directly instead of routing through the main model.
- A successful explicit "link form to database" action must import the full current response snapshot for that form into `agent_forms`, `form_responses`, and `form_response_answers`, then register the form in the local form-link registry so later background sync cycles keep the SQL copy current even for user-owned forms that were not originally created by the agent.
- If `ENABLE_GOOGLE_SHEETS_MCP=true` but `mcp-google-sheets` cannot boot because its auth setup is broken, do not let that abort the whole agent. Fall back to a forms-only MCP client, and keep that Sheets MCP disablement sticky for the current backend process so later graph loads do not keep retrying the same broken stdio server.
- Build the MCP client off the event loop. Token-path discovery can scan the shared OAuth session directory, and LangGraph dev will flag that synchronous filesystem work as blocking if `build_mcp_client(...)` runs directly inside async graph construction.
- The backend now exposes direct Postgres response-store tools for the agent. Use `inspect_form_response_database` for schema discovery and `query_form_response_database` for read-only SQL over `agent_forms`, `form_responses`, and `form_response_answers` when the user asks database questions.
- For obvious Postgres data requests such as listing stored forms or showing recent responses, prefer the local database shortcut so the agent returns actual rows instead of explaining the schema first. Reserve schema inspection for explicit structure/table/column questions.
- When the local Postgres shortcut returns row-oriented data such as form lists or recent responses, render the result as a markdown table instead of pipe-delimited bullet text. The chat UI already supports GFM tables, so use that for readable database output.
- For Postgres requests that ask for analysis, summaries, charts, dashboards, or insights, resolve a target form from `agent_forms`, aggregate `form_responses` plus `form_response_answers`, and reuse the spreadsheet-analysis visual payload so SQL-backed form data renders through the same inline chart panel in chat.
- If the user includes an explicit `form_id`, treat that as a SQL target signal for the database shortcut so analysis requests can resolve the correct stored form even without separately naming Postgres.
- Keep the Postgres routing gate conservative. A normal form-creation prompt must not enter the database shortcut unless it carries an explicit `form_id` or a strong database keyword (`postgres`, `database`, `sql`, `ฐานข้อมูล`, `โพสต์เกรส`); `db` should only match as its own token, not as a substring inside unrelated text or file content.
- Apply the same explicit-`form_id` guard on the form-creation side: if a message includes a `form_id` and analysis wording, do not classify it as form creation just because it contains generic words like `ฟอร์ม` or `Google Form`.
- If a user asks to analyze "this form" or the previously created form in the same thread without repeating the `form_id`, resolve the target from the most recent form id mentioned earlier in that thread before falling back to catalog-wide matching or latest-form defaults.
- For SQL-backed form analysis, also accept bare Google Form ids in analysis-style prompts without requiring `form_id=`. If the prompt already signals analysis and contains a likely form id token, route it through the SQL shortcut before the model can fall through to MCP form tools.
- Markdown tables in chat should stay visually centered within the message area. Do not force them to `width: 100%`; wrap them in a horizontal-scroll container and keep the table itself auto-width so compact result tables do not hug the left edge.
- For database-style markdown tables in chat, let long ids and timestamps wrap aggressively inside capped cell widths. Avoid `shrink-0` on the table element, or long unbroken values will force the table to span the whole message width and defeat centering.
- The web UI no longer needs the chat-message feedback feature. Do not query or mutate `chat_message_history` for thumbs feedback, and do not render feedback columns or controls in thread/history views.
- The standalone `/history` report page and its export route are no longer part of the product. Keep the in-chat thread sidebar (`components/thread/history`) if needed, but do not keep or revive the separate history-report pages under `app/history`.
- If a new backend helper adds type hints such as `Sequence[...]` in `agent.py`, keep the corresponding typing import in sync. Missing typing imports break LangGraph graph load at startup before any request handling begins.
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
- Route explicit read-only SQL in user messages and form-scoped database requests through the local Postgres shortcut when a target form can be resolved. Keep only trivial deterministic templates such as raw response lookup; broader form-scoped analytics should fall through to the generic read-only NL-to-SQL generator instead of accumulating prompt-specific branches.
- For free-form database questions that do not match a deterministic local template, use the configured chat model to generate one read-only PostgreSQL query from the known response-store schema, then pass it through the existing read-only SQL validator before execution.
- For form-scoped NL-to-SQL, inspect stored form structure before generation when possible. Feed the model actual `question_id` / `question_title` mappings, likely identity titles, and a sampled `response_json.answers` shape from Postgres so it does not guess labels like `Name` or assume the wrong JSON layout.
- For score queries on stored form responses, prefer `form_responses.response_json ->> 'totalScore'` as the response-level score when it exists. Use summed per-answer grade scores only as a fallback when `totalScore` is absent.
- For free-form score or ranking questions over stored Google Form responses, prefer grade data from `form_responses.response_json.answers[*].grade.score` instead of casting `form_response_answers.answer_text`. If the first generated SQL fails with a numeric-cast style database error, regenerate once with the concrete error details before surfacing failure.
- In synced Google Forms response payloads, `form_responses.response_json -> 'answers'` is a JSON object keyed by question id, not an array. For NL-to-SQL score queries, iterate it with `jsonb_each(...)` rather than `jsonb_array_elements(...)`, and treat "cannot extract elements from an object" as a retryable query-generation error.
- For NL-to-SQL score-ranking requests, a syntactically valid query can still be semantically wrong if it returns only null identity/score fields. Treat that as retryable: regenerate once with stricter guidance to exclude null scores and return a non-empty respondent identifier, falling back from `respondent_email` to likely identity answers or `response_id`.
- For stored-form top-scorer or respondent-score questions, do not rely only on model-generated SQL. If the NL-to-SQL output is blank, malformed, or omits respondent identity, fall back to a deterministic Postgres query that sums `response_json.answers[*].grade.score` per response and resolves identity from `respondent_email`, likely name fields in `form_response_answers`, or `response_id`.
- For respondent-identity score queries, treat obviously weak identity outputs such as a single letter, blank string, or placeholder-like token as invalid. In those cases, force the deterministic fallback and expose the identity column as `name` in the final answer table.
- For score-ranking outputs, require a usable numeric score as well as a usable identity. If the first query returns a name with null/blank `score` or `total_score`, treat that as invalid and force the deterministic fallback.
- Identity-field matching for stored form answers must normalize title punctuation and spacing. Treat variants like `ชื่อ-นามสกุล`, `ชื่อ - นามสกุล`, and similar normalized forms as the same highest-priority name field when choosing respondent identity.
- For the generic local Postgres query shortcut in chat, return the answer table directly when rows exist. Do not echo the generated SQL or extra row-count bullets unless the user explicitly asks for the SQL.
- If a prompt already contains an explicit existing `form_id`, do not treat generic words like `form`, `ฟอร์ม`, or `แบบฟอร์ม` as enough evidence for form creation. The direct create-form shortcut should require an actual creation verb in that case.
- Treat bare Google Form id tokens as existing-form context when they appear alongside existing-form phrasing such as `from form`, `this form`, `previous form`, `จากฟอร์ม`, or `แบบฟอร์มนี้`. In those cases, do not let the create-form shortcut fire unless the prompt also contains an actual creation verb.
- The repo now includes a dedicated skill at `skills/google-form-response-store-query/` for querying the SQL response store of existing forms. Use it for stored-form questions and keep it separate from the `google-form-author` creation workflow.
- The `skills/google-form-response-store-query/SKILL.md` file includes concrete read-only SQL patterns for score distributions, grouped counts, score filters, average score by cohort, most-wrong questions, top scorers, and schema discovery. Prefer those examples before asking a weak local model to invent SQL.
- Deep Agents skill frontmatter is parsed as YAML. If a metadata value contains a colon, quote it explicitly or the skill loader can skip the file as invalid YAML.
- For form-scoped respondent top-score prompts, bypass generic NL-to-SQL and execute the deterministic fallback query first. Prefer the stored identity answer over `respondent_email` when exposing the `name` column so the final table shows name plus score.
- When ranking likely respondent-identity fields from `form_response_answers`, keep only the strongest title tier for deterministic fallback queries. Do not mix weaker generic `ชื่อ...` fields with a stronger full-name field, or the fallback can pick unrelated values such as class/series names.
- In deterministic respondent top-score SQL, filter placeholder identity values at the SQL level before ordering tied top scorers. Exclude blank values, single-letter placeholders like `a`, and obvious dummy tokens like `test` so a real tied top scorer is shown instead.
- For deterministic respondent top-score SQL, rank top `response_id` candidates first, then resolve the displayed name from `form_response_answers` for those response ids. Do not let the identity fallback collapse to `response_id` before the name lookup step, or the result can show a response id even when a later tied response has a valid stored name.
- Score-ranking direction must propagate through deterministic fallback queries. For prompts asking for the lowest or least score, order candidate `total_score` ascending in both the direct form-scoped shortcut and retry/fallback paths; do not always default to descending top-scorer behavior.
- For form-scoped follow-up prompts asking for score statistics such as median, mean, average, or `ค่ากลาง`, use a deterministic Postgres score-stats query over the same resolved `form_id`. Do not rely on the model to infer basic statistics from the previous table output.
- For deterministic score-stats replies, project only the metrics the user explicitly asked for. Example: `ค่ากลางของคะแนน` should return only `median_score`, not a full summary row with min/max/avg/count unless those were requested.
- SQL-backed form analysis payloads must include a score column derived from `form_responses.response_json->>'totalScore'` so score-distribution charts can be built from Postgres data. If the user explicitly asks for a score distribution graph, return that chart directly instead of appending the generic mixed dashboard charts.
- For explicit single-chart requests such as a score distribution graph, keep the chat response minimal. Do not prepend the generic analysis summary bullets or deep-insight cards, and do not truncate the score distribution series to a fixed top-N bucket list.


- The chat UI spreadsheet-analysis visual supports displayMode: single-chart to suppress the outer spreadsheet-analysis header/frame for explicit single-chart requests. Use it when the user asks for one specific graph instead of a dashboard.

- The chat UI should infer single-chart rendering from the visual payload shape when possible, not only from an explicit backend flag. If there is exactly one chart, no insights, and the analysis request clearly asks for a distribution graph, suppress the spreadsheet-analysis frame/header.

- In the chat UI, any spreadsheet-analysis visual payload with exactly one chart should render in single-chart mode, even if the backend flag or request-text heuristic is missing. This prevents the outer spreadsheet-analysis header from appearing around a single requested graph.

- Spreadsheet-analysis bar charts in the chat UI now render as vertical columns, not horizontal bars. Use category labels on the X axis, numeric counts on the Y axis, and rotate dense labels when needed so score distributions read top-to-bottom.

- The web UI composer supports a Manual SQL toggle for user-authored read-only queries. When enabled, it disables attachments, switches the prompt area to SQL-oriented input, wraps the submitted text in a fenced sql block, and passes manual_sql_enabled through chat context/metadata so the backend can route it through the existing read-only SQL path.

- Manual SQL mode client-side validation should accept SELECT or WITH followed by any SQL word boundary, not only a literal space, so multiline queries starting on the next line are allowed.

- Manual SQL mode must submit the user-authored SQL verbatim and bypass thread-derived form context. When manual_sql_enabled is set in request context/metadata, prioritize validating and executing the latest human message as the exact read-only query, and return just the result table or an explicit no-rows message.

- The database shortcut must recover manual SQL even when the frontend still sends older wrapped payloads or the manual flag is missing. Before falling back to NL-to-SQL on a form-scoped message, try extracting a raw read-only SQL candidate from plain text, old helper wrappers, bare sql prefixes, and fenced code blocks.

- SQL-looking user input must never fall through to the generic NL-to-SQL branch when direct SQL extraction fails. Treat that as a direct manual-SQL validation failure and return a deterministic read-only-SQL error message instead of reinterpreting the request from thread form context.

- Read-only SQL validation must accept SELECT or WITH followed by any word boundary, not only a literal space. Multiline SQL that starts with SELECT on the first line and the rest of the query on following lines is valid and should not be rejected.
