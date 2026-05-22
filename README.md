# august26th
An AI agent for training management.

## Google OAuth

The app now includes a built-in Google OAuth 2.0 consent flow for Google Forms.

1. Create a Google OAuth client in Google Cloud.
2. Add the callback URL for the exact host users will open in the browser. For local use, that is:

```text
http://localhost:3000/api/google/oauth/callback
```

3. Set `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` in `.env`.
4. Start both services:

```bash
docker compose up --build
```

5. Open the web UI at `http://localhost:3000` and click `Connect Google`.

After consent succeeds, the refresh token is stored in shared app storage and
the backend will use it for Google Forms MCP calls. A manual
`GOOGLE_REFRESH_TOKEN` is still supported, but it is now optional.

The OAuth flow also stores the connected Google account's basic profile
(name, email, avatar URL) for the active browser session so the web UI can show
which account is currently connected.

The app now derives the OAuth callback URL automatically from the incoming
request host. If users open the UI from a different host or IP, add that exact
`/api/google/oauth/callback` URL to the Google OAuth client as well.

## Google Sheets Analysis Skill

The project now includes a bundled skill:

- `google-sheets-form-response-analysis`

Use it when a user wants to analyze Google Form responses that are stored in a
Google Sheet, or more generally analyze spreadsheet data in Google Sheets, for
example counts, rating distributions, grouped summaries, trend analysis, data
quality checks, written analysis tabs, or charts and graphs.

To make the skill usable, enable the Sheets MCP in `.env`:

```env
ENABLE_GOOGLE_SHEETS_MCP=true
```

When the user connects Google in the web UI, the Sheets MCP now reuses that same
shared OAuth token automatically.

Optional alternative for server automation:

```env
SERVICE_ACCOUNT_PATH=/path/to/service-account.json
DRIVE_FOLDER_ID=your-google-drive-folder-id
```

The backend starts the upstream `mcp-google-sheets` server with a default
analysis-oriented tool subset:

- `search_spreadsheets`
- `list_spreadsheets`
- `list_sheets`
- `get_sheet_data`
- `get_multiple_sheet_data`
- `get_sheet_formulas`
- `find_in_spreadsheet`
- `create_sheet`
- `update_cells`
- `batch_update_cells`
- `add_chart`
- `batch_update`

This keeps tool context smaller than loading the full upstream tool list.

## LLM Provider

The backend supports OpenAI-compatible chat APIs.

Use OpenRouter:

```env
LLM_PROVIDER=openrouter
OPENROUTER_API_KEY=sk-or-v1-your-key
OPENROUTER_MODEL=openai/gpt-4.1
OPENROUTER_MODEL_2=google/gemini-2.5-flash
OPENROUTER_MODEL_3=anthropic/claude-3.5-haiku
```

Use a local LLM server:

```env
LLM_PROVIDER=local
LOCAL_LLM_BASE_URL=http://host.docker.internal:11434/v1
LOCAL_LLM_MODEL=llama3.1
LOCAL_LLM_API_KEY=not-needed
```

For Docker Desktop on Windows, `host.docker.internal` lets the backend container reach a local server running on the host. For a remote Ollama server, use the server IP or DNS name instead:

```env
LOCAL_LLM_BASE_URL=http://10.147.17.215:11434/v1
```

The server must accept network traffic on port `11434`. On the Ollama server, run Ollama bound to the network, for example with `OLLAMA_HOST=0.0.0.0:11434`, and allow the port through the firewall. For CLI runs directly on Windows, use `http://localhost:<port>/v1`.

## Uploaded Files

The Web UI accepts JPEG, PNG, GIF, WEBP, PDF, DOC, DOCX, XLSX, PPTX, RTF, TXT,
Markdown, CSV, TSV, JSON, XML, and HTML files.

For local text-only LLMs, the backend converts uploaded content before sending it to the model:

- PDF: extracts readable page text with `pypdf`
- DOCX: extracts readable Word document text
- DOC: best-effort text extraction from legacy Word binary files
- Images: passes filename and MIME type as attachment context
