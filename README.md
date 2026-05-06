# august26th
An AI agent for training management.

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
