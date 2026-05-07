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

The app now derives the OAuth callback URL automatically from the incoming
request host. If users open the UI from a different host or IP, add that exact
`/api/google/oauth/callback` URL to the Google OAuth client as well.

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
