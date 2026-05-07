"""LangChain Deep Agent wired to the Google Forms MCP server."""

import base64
import csv
import html
import io
import json
import os
import re
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from deepagents import create_deep_agent
from deepagents.backends.filesystem import FilesystemBackend
from langchain.agents.middleware.types import AgentMiddleware, ModelRequest, ModelResponse
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_core.messages import AIMessage, AnyMessage
from langchain_openai import ChatOpenAI


OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
LOCAL_LLM_DEFAULT_API_KEY = "not-needed"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
SKILLS_DIR = PROJECT_ROOT / "skills"

PDF_MIME_TYPE = "application/pdf"
DOC_MIME_TYPE = "application/msword"
DOCX_MIME_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX_MIME_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PPTX_MIME_TYPE = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
RTF_MIME_TYPE = "application/rtf"
TEXT_MIME_TYPES = {
    "text/plain",
    "text/markdown",
    "text/csv",
    "text/tab-separated-values",
    "text/html",
    "text/xml",
    "application/json",
    "application/xml",
}
EXTENSION_MIME_TYPES = {
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".csv": "text/csv",
    ".tsv": "text/tab-separated-values",
    ".json": "application/json",
    ".xml": "application/xml",
    ".html": "text/html",
    ".htm": "text/html",
    ".rtf": RTF_MIME_TYPE,
    ".pdf": PDF_MIME_TYPE,
    ".doc": DOC_MIME_TYPE,
    ".docx": DOCX_MIME_TYPE,
    ".xlsx": XLSX_MIME_TYPE,
    ".pptx": PPTX_MIME_TYPE,
}


SYSTEM_PROMPT = """You are a Google Forms creation agent for NECTEC users.

Your job is to convert a user's form request into a clear Google Form using the
available Google Forms MCP tools. Work deliberately:
- If a user asks about an uploaded file and the message contains "Uploaded file
  context", treat that context as the already-extracted file text. Answer from
  it directly. Do not say you cannot access the file unless no extracted context
  is present.
- If the message contains <<<FILE_TEXT>>> and <<<END_FILE_TEXT>>> markers, the
  exact uploaded file text is between those markers. For requests like "show all
  text in this file", return that marked text directly.
- Clarify only when required fields or question details are missing.
- Create the form first, then add questions one at a time.
- When calling create_form, pass only the form title. Do not pass a description,
  questions, settings, or other form fields in the create_form call because the
  Google Forms API only allows info.title during creation.
- Prefer concise, user-ready form titles, descriptions, and question labels.
- Use text questions for open responses and multiple choice questions when the
  user gives options.
- After creating or editing a form, report the form title, the questions added,
  and any URL or form ID returned by the tools.
- Never claim a form was created unless a Google Forms MCP tool succeeded.
"""

OLD_UPLOAD_CONTEXT_RE = re.compile(
    r"Uploaded file context:\n(?P<context>[\s\S]*?)\n\n"
    r"Use the uploaded file context above\. Do not search the filesystem for "
    r"these uploaded files\.",
    re.IGNORECASE,
)
FILE_TEXT_RE = re.compile(
    r"<<<FILE_TEXT>>>\s*(?P<context>[\s\S]*?)\s*<<<END_FILE_TEXT>>>",
    re.IGNORECASE,
)
UPLOAD_FILE_HEADER_RE = re.compile(r"^\[(?:Uploaded|Attached) file: .+\]$", re.IGNORECASE)
PAGE_MARKER_RE = re.compile(r"^--\s*\d+\s+of\s+\d+\s*--$", re.IGNORECASE)


def clean_extracted_file_text(context: str) -> str:
    """Remove UI metadata around extracted upload text before it reaches the LLM."""
    cleaned_lines: list[str] = []
    for raw_line in context.strip().splitlines():
        line = raw_line.strip()
        if not line:
            cleaned_lines.append("")
            continue
        if UPLOAD_FILE_HEADER_RE.match(line):
            continue
        if PAGE_MARKER_RE.match(line):
            continue
        if line.lower() == "file content:":
            continue
        cleaned_lines.append(raw_line.rstrip())

    cleaned = "\n".join(cleaned_lines).strip()
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.replace(" /think", "").replace("/think", "").strip()


def marker_file_context(context: str) -> str:
    """Format extracted uploaded-file text so local models do not attempt extraction."""
    cleaned_context = clean_extracted_file_text(context)
    return (
        "The uploaded file has already been processed by the application. "
        "Do not use tools. Do not say you cannot access it.\n"
        "Exact extracted uploaded file text:\n"
        "<<<FILE_TEXT>>>\n"
        f"{cleaned_context}\n"
        "<<<END_FILE_TEXT>>>\n"
        "When the user asks for text in the uploaded file, return only the text "
        "between FILE_TEXT markers."
    )


def normalize_uploaded_file_context(text: str) -> str:
    """Upgrade old hidden upload context blocks to the stricter marker format."""
    if "<<<FILE_TEXT>>>" in text:
        return FILE_TEXT_RE.sub(
            lambda match: (
                f"<<<FILE_TEXT>>>\n"
                f"{clean_extracted_file_text(match.group('context'))}\n"
                f"<<<END_FILE_TEXT>>>"
            ),
            text,
        )

    return OLD_UPLOAD_CONTEXT_RE.sub(
        lambda match: marker_file_context(match.group("context")),
        text,
    )


def clean_model_file_context_echo(message: AIMessage) -> AIMessage:
    """Hide upload-control markers if a local model echoes them back."""
    if not isinstance(message.content, str) or "<<<FILE_TEXT>>>" not in message.content:
        return message

    match = FILE_TEXT_RE.search(message.content)
    if not match:
        return message

    return message.model_copy(
        update={"content": clean_extracted_file_text(match.group("context"))}
    )


def clean_model_response(response: ModelResponse | AIMessage) -> ModelResponse | AIMessage:
    """Clean uploaded-file control markers from model responses."""
    if isinstance(response, AIMessage):
        return clean_model_file_context_echo(response)

    cleaned_result = [
        clean_model_file_context_echo(message)
        if isinstance(message, AIMessage)
        else message
        for message in response.result
    ]
    if cleaned_result == response.result:
        return response
    return ModelResponse(
        result=cleaned_result,
        structured_response=response.structured_response,
    )


def get_required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def normalize_openai_base_url(base_url: str) -> str:
    """Normalize local OpenAI-compatible base URLs such as Ollama endpoints."""
    normalized = base_url.strip().rstrip("/")
    if not normalized.startswith(("http://", "https://")):
        normalized = f"http://{normalized}"
    if not normalized.endswith("/v1"):
        normalized = f"{normalized}/v1"
    return normalized


def content_to_text(content: Any) -> str:
    """Convert rich LangChain message content into Ollama-friendly text."""
    if isinstance(content, str):
        return content
    if content is None:
        return ""
    if not isinstance(content, list):
        return str(content)

    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
            continue
        if not isinstance(block, dict):
            parts.append(str(block))
            continue

        block_type = block.get("type")
        if block_type == "text":
            text = block.get("text")
            if text:
                parts.append(str(text))
            continue

        metadata = block.get("metadata") if isinstance(block.get("metadata"), dict) else {}
        name = metadata.get("filename") or metadata.get("name") or "unnamed"
        mime_type = block.get("mimeType") or block.get("mime_type") or "unknown type"
        if block_type == "file":
            parts.append(file_block_to_text(block, str(name), normalize_mime_type(str(name), str(mime_type))))
        elif block_type == "image":
            parts.append(f"[Attached image: {name} ({mime_type})]")
        else:
            parts.append(f"[Unsupported content block: {block_type or 'unknown'}]")

    return "\n".join(part for part in parts if part).strip()


def file_block_to_text(block: dict[str, Any], name: str, mime_type: str) -> str:
    """Extract readable text from supported file upload blocks."""
    header = f"[Attached file: {name} ({mime_type})]"
    data = block.get("data")
    if not isinstance(data, str) or not data:
        return header

    try:
        file_bytes = base64.b64decode(data, validate=False)
    except Exception:
        return f"{header}\n[Could not decode uploaded file data.]"

    if mime_type == PDF_MIME_TYPE:
        text = extract_pdf_text(file_bytes)
        if text:
            return marker_file_context(f"{header}\n{text}")
        return f"{header}\n[No readable text was found in this PDF file.]"

    if mime_type == DOCX_MIME_TYPE:
        text = extract_docx_text(file_bytes)
        if text:
            return marker_file_context(f"{header}\n{text}")
        return f"{header}\n[No readable text was found in this DOCX file.]"

    if mime_type == DOC_MIME_TYPE:
        text = extract_doc_text(file_bytes)
        if text:
            return marker_file_context(f"{header}\n{text}")
        return f"{header}\n[No readable text was found in this legacy DOC file.]"

    if mime_type == XLSX_MIME_TYPE:
        text = extract_xlsx_text(file_bytes)
        if text:
            return marker_file_context(f"{header}\n{text}")
        return f"{header}\n[No readable text was found in this XLSX file.]"

    if mime_type == PPTX_MIME_TYPE:
        text = extract_pptx_text(file_bytes)
        if text:
            return marker_file_context(f"{header}\n{text}")
        return f"{header}\n[No readable text was found in this PPTX file.]"

    if mime_type == RTF_MIME_TYPE:
        text = extract_rtf_text(file_bytes)
        if text:
            return marker_file_context(f"{header}\n{text}")
        return f"{header}\n[No readable text was found in this RTF file.]"

    if mime_type in TEXT_MIME_TYPES or mime_type.startswith("text/"):
        text = extract_plain_text(file_bytes, mime_type)
        if text:
            return marker_file_context(f"{header}\n{text}")

    return f"{header}\n[This file type is attached, but text extraction is not supported yet.]"


def normalize_mime_type(name: str, mime_type: str) -> str:
    """Use filename extensions when browsers provide generic MIME types."""
    normalized = mime_type.strip().lower()
    if normalized and normalized not in {"application/octet-stream", "unknown type"}:
        return normalized
    return EXTENSION_MIME_TYPES.get(Path(name).suffix.lower(), normalized or "unknown type")


def decode_text(file_bytes: bytes) -> str:
    """Decode common text encodings without failing the upload."""
    for encoding in ("utf-8-sig", "utf-16", "utf-16le", "utf-16be"):
        try:
            return file_bytes.decode(encoding).strip()
        except UnicodeDecodeError:
            continue
    return file_bytes.decode("latin-1", errors="replace").strip()


def extract_plain_text(file_bytes: bytes, mime_type: str) -> str:
    """Extract and lightly normalize plain text-like uploads."""
    text = decode_text(file_bytes)
    if not text:
        return ""

    if mime_type == "application/json":
        try:
            return json.dumps(json.loads(text), ensure_ascii=False, indent=2)
        except json.JSONDecodeError:
            return text

    if mime_type in {"text/html", "text/xml", "application/xml"}:
        text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", text)
        text = re.sub(r"(?s)<[^>]+>", " ", text)
        return re.sub(r"\s+", " ", html.unescape(text)).strip()

    if mime_type in {"text/csv", "text/tab-separated-values"}:
        delimiter = "\t" if mime_type == "text/tab-separated-values" else ","
        try:
            rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
        except csv.Error:
            return text
        return "\n".join(
            " | ".join(cell.strip() for cell in row)
            for row in rows
            if any(cell.strip() for cell in row)
        ).strip()

    return text


def extract_pdf_text(file_bytes: bytes) -> str:
    """Extract text from a PDF file."""
    try:
        from pypdf import PdfReader
    except ImportError:
        return ""

    try:
        reader = PdfReader(io.BytesIO(file_bytes))
    except Exception:
        return ""

    pages: list[str] = []
    for page in reader.pages:
        try:
            text = page.extract_text() or ""
        except Exception:
            text = ""
        text = text.strip()
        if text:
            pages.append(text)

    return "\n\n".join(pages).strip()


def extract_doc_text(file_bytes: bytes) -> str:
    """Best-effort text extraction from legacy binary DOC files."""
    try:
        import olefile
    except ImportError:
        return ""

    try:
        ole = olefile.OleFileIO(io.BytesIO(file_bytes))
    except Exception:
        return ""

    chunks: list[bytes] = []
    try:
        for stream_path in ole.listdir(streams=True, storages=False):
            stream_name = "/".join(stream_path)
            if stream_name in {"WordDocument", "1Table", "0Table"}:
                try:
                    chunks.append(ole.openstream(stream_path).read())
                except Exception:
                    continue
    finally:
        ole.close()

    if not chunks:
        return ""

    text_candidates: list[str] = []
    joined = b"\n".join(chunks)
    for encoding in ("utf-16le", "latin-1"):
        decoded = joined.decode(encoding, errors="ignore")
        decoded = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]+", " ", decoded)
        decoded = re.sub(r"\s+", " ", decoded).strip()
        words = re.findall(r"[A-Za-z0-9][^\s]{1,}", decoded)
        if len(words) >= 3:
            text_candidates.append(decoded)

    return max(text_candidates, key=len, default="").strip()


def extract_docx_text(file_bytes: bytes) -> str:
    """Extract text from a DOCX using only the Python standard library."""
    try:
        with zipfile.ZipFile(io.BytesIO(file_bytes)) as docx:
            xml_bytes = docx.read("word/document.xml")
    except Exception:
        return ""

    try:
        root = ElementTree.fromstring(xml_bytes)
    except ElementTree.ParseError:
        return ""

    namespace = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    paragraphs: list[str] = []
    for paragraph in root.findall(".//w:p", namespace):
        runs = [
            text_node.text or ""
            for text_node in paragraph.findall(".//w:t", namespace)
        ]
        text = "".join(runs).strip()
        if text:
            paragraphs.append(text)

    return "\n".join(paragraphs).strip()


def xml_text_nodes(xml_bytes: bytes, tag_suffix: str = "t") -> list[str]:
    """Collect text nodes by XML tag suffix across Office XML namespaces."""
    try:
        root = ElementTree.fromstring(xml_bytes)
    except ElementTree.ParseError:
        return []

    values: list[str] = []
    for node in root.iter():
        if node.tag.rsplit("}", 1)[-1] == tag_suffix and node.text:
            values.append(node.text)
    return values


def extract_xlsx_text(file_bytes: bytes) -> str:
    """Extract readable cell text from an XLSX workbook."""
    try:
        workbook = zipfile.ZipFile(io.BytesIO(file_bytes))
    except Exception:
        return ""

    with workbook:
        shared_strings: list[str] = []
        try:
            shared_root = ElementTree.fromstring(workbook.read("xl/sharedStrings.xml"))
            for item in shared_root.iter():
                if item.tag.rsplit("}", 1)[-1] != "si":
                    continue
                shared_strings.append("".join(xml_text_nodes(ElementTree.tostring(item))))
        except Exception:
            shared_strings = []

        rows: list[str] = []
        sheet_names = sorted(
            name
            for name in workbook.namelist()
            if re.match(r"xl/worksheets/sheet\d+\.xml$", name)
        )
        for sheet_index, sheet_name in enumerate(sheet_names, start=1):
            try:
                root = ElementTree.fromstring(workbook.read(sheet_name))
            except Exception:
                continue
            rows.append(f"Sheet {sheet_index}:")
            for row in root.iter():
                if row.tag.rsplit("}", 1)[-1] != "row":
                    continue
                cells: list[str] = []
                for cell in row:
                    if cell.tag.rsplit("}", 1)[-1] != "c":
                        continue
                    cell_type = cell.attrib.get("t")
                    value = ""
                    for child in cell:
                        child_tag = child.tag.rsplit("}", 1)[-1]
                        if child_tag == "v" and child.text is not None:
                            value = child.text
                        elif child_tag == "is":
                            value = "".join(
                                text_node.text or ""
                                for text_node in child.iter()
                                if text_node.tag.rsplit("}", 1)[-1] == "t"
                            )
                    if cell_type == "s" and value.isdigit():
                        index = int(value)
                        value = shared_strings[index] if index < len(shared_strings) else value
                    if value:
                        cells.append(value.strip())
                if cells:
                    rows.append(" | ".join(cells))

    return "\n".join(rows).strip()


def extract_pptx_text(file_bytes: bytes) -> str:
    """Extract readable text from PPTX slides."""
    try:
        presentation = zipfile.ZipFile(io.BytesIO(file_bytes))
    except Exception:
        return ""

    with presentation:
        slides: list[str] = []
        slide_names = sorted(
            name
            for name in presentation.namelist()
            if re.match(r"ppt/slides/slide\d+\.xml$", name)
        )
        for index, slide_name in enumerate(slide_names, start=1):
            try:
                texts = [text.strip() for text in xml_text_nodes(presentation.read(slide_name)) if text.strip()]
            except Exception:
                texts = []
            if texts:
                slides.append(f"Slide {index}:\n" + "\n".join(texts))

    return "\n\n".join(slides).strip()


def extract_rtf_text(file_bytes: bytes) -> str:
    """Best-effort plain text extraction from RTF control markup."""
    text = decode_text(file_bytes)
    if not text:
        return ""

    output: list[str] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char in "{}":
            index += 1
            continue
        if char != "\\":
            output.append(char)
            index += 1
            continue

        index += 1
        if index >= len(text):
            break

        escaped = text[index]
        if escaped in "{}\\":
            output.append(escaped)
            index += 1
            continue
        if escaped == "'":
            hex_value = text[index + 1 : index + 3]
            if len(hex_value) == 2:
                try:
                    output.append(bytes.fromhex(hex_value).decode("latin-1"))
                except Exception:
                    pass
            index += 3
            continue

        start = index
        while index < len(text) and text[index].isalpha():
            index += 1
        control = text[start:index]
        if index < len(text) and text[index] in "-0123456789":
            index += 1
            while index < len(text) and text[index].isdigit():
                index += 1
        if index < len(text) and text[index] == " ":
            index += 1

        if control in {"par", "line"}:
            output.append("\n")
        elif control == "tab":
            output.append("\t")

    return re.sub(r"[ \t]+", " ", "".join(output)).strip()


def sanitize_message_content(message: AnyMessage) -> AnyMessage:
    """Return a copy of a message with content converted to plain text."""
    if isinstance(message.content, str):
        text = normalize_uploaded_file_context(message.content)
        if text == message.content:
            return message
        return message.model_copy(update={"content": text})

    text = content_to_text(message.content)
    text = normalize_uploaded_file_context(text)
    return message.model_copy(update={"content": text})


def get_attached_file_context(request: ModelRequest) -> str:
    """Read hidden uploaded file context sent by the Web UI."""
    context = request.state.get("context") if isinstance(request.state, dict) else None
    if not isinstance(context, dict):
        return ""

    attached_file_context = context.get("attached_file_context")
    if isinstance(attached_file_context, str):
        return attached_file_context.strip()
    return ""


def inject_attached_file_context(
    messages: list[AnyMessage],
    attached_file_context: str,
) -> list[AnyMessage]:
    """Attach hidden file context to the latest human message for model calls."""
    if not attached_file_context:
        return messages

    injected_text = marker_file_context(attached_file_context)

    next_messages = list(messages)
    for index in range(len(next_messages) - 1, -1, -1):
        message = next_messages[index]
        if message.type != "human":
            continue

        content = content_to_text(message.content)
        if "Uploaded file context:" in content or "<<<FILE_TEXT>>>" in content:
            return next_messages

        next_messages[index] = message.model_copy(
            update={"content": f"{content}\n\n{injected_text}".strip()}
        )
        return next_messages

    return next_messages


class LocalLLMMessageFormatMiddleware(AgentMiddleware):
    """Make DeepAgents messages compatible with local OpenAI-compatible servers."""

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Any],
    ) -> ModelResponse | AIMessage:
        system_message = (
            sanitize_message_content(request.system_message)
            if request.system_message is not None
            else None
        )
        messages = [sanitize_message_content(message) for message in request.messages]
        messages = inject_attached_file_context(
            messages,
            get_attached_file_context(request),
        )
        response = await handler(
            request.override(system_message=system_message, messages=messages)
        )
        return clean_model_response(response)


def build_openrouter_model() -> ChatOpenAI:
    """Create a chat model that uses OpenRouter's OpenAI-compatible API."""
    api_key = get_required_env("OPENROUTER_API_KEY")
    model = os.getenv("OPENROUTER_MODEL", "openai/gpt-4.1")
    fallback_models = [
        fallback_model
        for fallback_model in (
            os.getenv("OPENROUTER_MODEL_2"),
            os.getenv("OPENROUTER_MODEL_3"),
        )
        if fallback_model
    ]

    default_headers: dict[str, str] = {}
    if site_url := os.getenv("OPENROUTER_SITE_URL"):
        default_headers["HTTP-Referer"] = site_url
    if app_name := os.getenv("OPENROUTER_APP_NAME"):
        default_headers["X-Title"] = app_name

    return ChatOpenAI(
        api_key=api_key,
        base_url=OPENROUTER_BASE_URL,
        model=model,
        temperature=0.2,
        default_headers=default_headers or None,
        extra_body={"models": fallback_models} if fallback_models else None,
        max_retries=3,
        disable_streaming=True,
    )


def build_local_model() -> ChatOpenAI:
    """Create a chat model for a local OpenAI-compatible LLM server."""
    base_url = normalize_openai_base_url(get_required_env("LOCAL_LLM_BASE_URL"))
    model = os.getenv("LOCAL_LLM_MODEL", "llama3.1")
    api_key = os.getenv("LOCAL_LLM_API_KEY", LOCAL_LLM_DEFAULT_API_KEY)

    return ChatOpenAI(
        api_key=api_key,
        base_url=base_url,
        model=model,
        temperature=0.2,
        max_retries=3,
        disable_streaming=True,
    )


def build_chat_model() -> ChatOpenAI:
    """Create the configured chat model provider."""
    provider = os.getenv("LLM_PROVIDER", "openrouter").strip().lower()
    if provider == "openrouter":
        return build_openrouter_model()
    if provider == "local":
        return build_local_model()

    raise RuntimeError(
        "Unsupported LLM_PROVIDER. Use 'openrouter' or 'local'. "
        f"Received: {provider}"
    )


def build_mcp_client() -> MultiServerMCPClient:
    """Build the MCP client for the Google Forms stdio server."""
    server_path = Path(get_required_env("GOOGLE_FORMS_MCP_PATH")).expanduser()
    if not server_path.exists():
        raise RuntimeError(
            "GOOGLE_FORMS_MCP_PATH does not exist. Build google-forms-mcp and "
            f"set GOOGLE_FORMS_MCP_PATH to its build/index.js file: {server_path}"
        )

    return MultiServerMCPClient(
        {
            "google_forms": {
                "transport": "stdio",
                "command": "node",
                "args": [str(server_path)],
                "env": {
                    "GOOGLE_CLIENT_ID": get_required_env("GOOGLE_CLIENT_ID"),
                    "GOOGLE_CLIENT_SECRET": get_required_env("GOOGLE_CLIENT_SECRET"),
                    "GOOGLE_REFRESH_TOKEN": get_required_env("GOOGLE_REFRESH_TOKEN"),
                },
            }
        },
        tool_name_prefix=True,
    )


async def build_agent() -> Any:
    """Create the Deep Agent with Google Forms MCP tools."""
    model = build_chat_model()
    client = build_mcp_client()
    tools = await client.get_tools()

    return create_deep_agent(
        model=model,
        tools=tools,
        middleware=[LocalLLMMessageFormatMiddleware()],
        backend=FilesystemBackend(root_dir=str(PROJECT_ROOT)),
        skills=[SKILLS_DIR.as_posix()],
        system_prompt=SYSTEM_PROMPT,
    )
