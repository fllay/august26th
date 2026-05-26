"""LangChain Deep Agent wired to the Google Forms MCP server."""

import asyncio
import ast
import base64
import contextvars
import csv
from datetime import datetime, timezone
import html
from httplib2.error import ServerNotFoundError
import io
import json
import math
import os
import re
import threading
import time
from urllib import error as urllib_error
from urllib import request as urllib_request
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any, Sequence
from xml.etree import ElementTree
from PIL import Image

from deepagents import create_deep_agent
from deepagents.backends.filesystem import FilesystemBackend
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials as UserCredentials
from googleapiclient.discovery import build as build_google_api
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaInMemoryUpload
from psycopg import connect as pg_connect
from langchain.agents.middleware.types import AgentMiddleware, ModelRequest, ModelResponse
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI


OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
LOCAL_LLM_DEFAULT_API_KEY = "not-needed"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
SKILLS_DIR = PROJECT_ROOT / "skills"
DEFAULT_GOOGLE_OAUTH_TOKEN_PATH = PROJECT_ROOT / ".data" / "google-oauth.json"
FORM_SHEET_LINKS_PATH = PROJECT_ROOT / ".data" / "form-sheet-links.json"
GOOGLE_APPS_SCRIPT_CONFIG_PATH = PROJECT_ROOT / ".data" / "google-apps-script.json"
GOOGLE_SHEETS_MCP_CREDENTIALS_PATH = PROJECT_ROOT / ".data" / "google-sheets-mcp-credentials.json"
GOOGLE_SHEETS_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]
GOOGLE_WORKSPACE_SCOPES = [
    "https://www.googleapis.com/auth/forms.body",
    "https://www.googleapis.com/auth/forms.responses.readonly",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
GOOGLE_APPS_SCRIPT_SCOPES = [
    "https://www.googleapis.com/auth/forms",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/script.projects",
    "https://www.googleapis.com/auth/script.deployments",
    "https://www.googleapis.com/auth/script.scriptapp",
]
GOOGLE_OAUTH_SESSION_KEY = contextvars.ContextVar[str | None](
    "google_oauth_session_key",
    default=None,
)
BUILT_AGENT: Any | None = None
BUILT_AGENT_LOCK: asyncio.Lock | None = None
FORM_RESPONSE_SYNC_WORKER_STARTED = False
FORM_RESPONSE_SYNC_WORKER_LOCK = threading.Lock()
GOOGLE_SHEETS_MCP_DISABLED_UNTIL_RESTART = False
FORM_RESPONSE_SCHEMA_INITIALIZED = False
FORM_RESPONSE_SCHEMA_LOCK = threading.Lock()
DEFAULT_RESPONDENT_INFO_QUESTIONS = [
    {"title": "ชื่อ-นามสกุล", "type": "text", "required": True},
    {"title": "หน่วยงาน/สถานศึกษา", "type": "text", "required": True},
    {"title": "ตำแหน่ง", "type": "text", "required": True},
    {"title": "จังหวัด", "type": "text", "required": True},
    {"title": "เบอร์โทรศัพท์", "type": "text", "required": True},
    {"title": "อีเมล", "type": "text", "required": True},
    {
        "title": "ประสบการณ์หรือพื้นฐานด้านเครือข่าย/เทคโนโลยี",
        "type": "multiple_choice",
        "required": True,
        "options": [
            "ไม่มีพื้นฐาน",
            "พื้นฐานเล็กน้อย",
            "พื้นฐานปานกลาง",
            "มีประสบการณ์มาก",
        ],
    },
]
RESPONDENT_SECTION_HINTS = (
    "participant information",
    "respondent information",
    "section 1",
    "ข้อมูลผู้เข้าอบรม",
    "ข้อมูลผู้เข้ารับการอบรม",
)

PDF_MIME_TYPE = "application/pdf"
DOC_MIME_TYPE = "application/msword"
DOCX_MIME_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX_MIME_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PPTX_MIME_TYPE = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
RTF_MIME_TYPE = "application/rtf"
IMAGE_MIME_BY_EXTENSION = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
}
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


SYSTEM_PROMPT = """You are a NECTEC workflow agent for Google Forms and Google Sheets.

Your job is to help NECTEC users with Google Forms and Google Sheets workflows.
Work deliberately:
- If a user asks about an uploaded file and the message contains "Uploaded file
  context", treat that context as the already-extracted file text. Answer from
  it directly. Do not say you cannot access the file unless no extracted context
  is present.
- If the message contains <<<FILE_TEXT>>> and <<<END_FILE_TEXT>>> markers, the
  exact uploaded file text is between those markers. For requests like "show all
  text in this file", return that marked text directly.
- If a user provides a Google Sheets spreadsheet URL or spreadsheet ID, treat it
  as a spreadsheet target, not as spreadsheet content or as part of the command
  text. Use Google Sheets tools to inspect it.
- Never invent, shorten, abbreviate, or replace Google Form or Spreadsheet IDs
  with placeholders such as "1aBcD..." or similar. Only use the exact ID
  provided by the user or returned by a successful tool call.
- If a user asks to analyze spreadsheet data, prefer Google Sheets analysis
  behavior over Google Forms creation behavior.
- If a request is clearly about spreadsheet analysis, do not switch into Google
  Forms creation mode and do not ask whether the user wants to create a form
  unless the user explicitly mentions creating one.
- Clarify only when required fields or question details are missing.
- For Google Forms creation:
  - Local form tools are available in this agent: create_form_with_response_sheet
    and list_google_forms.
  - Prefer create_form_with_response_sheet for new forms.
  - If the prompt already contains enough information, include the description
    and generated questions in the create_form_with_response_sheet call so the
    form is created as completely as possible in one tool call.
  - If questions were not included in the create_form_with_response_sheet call,
    add them after creating the form.
  - Do not use the raw MCP create_form tool for new form creation. Use
    create_form_with_response_sheet instead.
  - Prefer concise, user-ready form titles, descriptions, and question labels.
  - Use text questions for open responses and multiple choice questions when the
    user gives options.
  - If the user asks to list or browse forms, use list_google_forms.
- After create_form_with_response_sheet succeeds, reuse the exact returned
    formId value. Never substitute placeholder IDs.
  - After creating or editing a form, report the form title, the questions added,
    and any URL or form ID returned by the tools.
  - After creating a form, automatically create and link its Google Spreadsheet
    response destination when native Apps Script linking is available, and report
    the spreadsheet link back to the user.
  - Never claim a form was created unless a Google Forms MCP tool succeeded.
  - Never say that form-creation tools are unavailable when the request is about
    creating, listing, or syncing Google Forms. Those local form tools are
    available in this agent.
- For Google Sheets analysis:
  - Start spreadsheet inspection with the inspect_spreadsheet_for_analysis tool
    when the user provides a spreadsheet target and wants analysis.
  - If the user sends back a manually linked Google Form response spreadsheet,
    first format the raw response tab into a clean analysis-ready table using
    the local formatting tool before deeper analysis unless the sheet is already
    well-structured.
  - Inspect the spreadsheet structure first.
  - Unless the user narrows the scope, analyze the full used range of all sheet
    tabs that contain data, not just a preview sample.
  - Use the spreadsheet ID or URL directly when present.
  - If the user asks for analysis without specifying a type, choose a sensible
    default analysis from the available columns.
  - Do not ask the user to specify tabs, ranges, columns, chart types, or other
    analysis parameters before you have inspected the spreadsheet with tools.
  - For requests such as "analyze this spreadsheet", "simple summary", or
    similarly broad analysis asks, decide the analysis plan yourself after
    reading the spreadsheet structure.
- Report the spreadsheet/tab used, the row scope, the key findings, and any
    chart or summary tab created.
- For Postgres response-store questions:
  - Use inspect_form_response_database before writing SQL if the user asks
    about the database schema or available tables.
  - Use query_form_response_database for read-only queries against the
    response-store tables when the user asks for stored form/response data.
  - Do not invent database contents. Read from Postgres with tools.
- Reply in the same language as the user's most recent message whenever practical.
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
SPREADSHEET_ANALYSIS_VISUAL_START = "<<<SPREADSHEET_ANALYSIS_VISUAL>>>"
SPREADSHEET_ANALYSIS_VISUAL_END = "<<<END_SPREADSHEET_ANALYSIS_VISUAL>>>"
EMBEDDED_IMAGE_BLOCK_RE = re.compile(
    r"<<<EMBEDDED_IMAGE(?P<meta>[^>]*)>>>\s*(?P<data>[\s\S]*?)\s*<<<END_EMBEDDED_IMAGE>>>",
    re.IGNORECASE,
)
UPLOAD_FILE_HEADER_RE = re.compile(r"^\[(?:Uploaded|Attached) file: .+\]$", re.IGNORECASE)
PAGE_MARKER_RE = re.compile(r"^--\s*\d+\s+of\s+\d+\s*--$", re.IGNORECASE)
SPREADSHEET_URL_RE = re.compile(
    r"https?://docs\.google\.com/spreadsheets/d/[a-zA-Z0-9-_]+(?:/[^\s<>()]*)?(?:\?[^\s<>()]*)?",
    re.IGNORECASE,
)
SPREADSHEET_ID_RE = re.compile(r"\b[a-zA-Z0-9-_]{30,}\b")


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


def strip_embedded_image_blocks(text: str) -> str:
    """Remove serialized embedded-image payloads from text before question parsing."""
    stripped = EMBEDDED_IMAGE_BLOCK_RE.sub("", text)
    return re.sub(r"\n{3,}", "\n\n", stripped).strip()


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


def extract_embedded_file_context(text: str) -> str:
    """Extract already-processed uploaded-file text from a message body."""
    matches = list(FILE_TEXT_RE.finditer(text))
    if not matches:
        return ""
    cleaned_contexts = [
        clean_extracted_file_text(match.group("context"))
        for match in matches
        if clean_extracted_file_text(match.group("context"))
    ]
    if not cleaned_contexts:
        return ""
    return max(cleaned_contexts, key=len)


def strip_embedded_file_context(text: str) -> str:
    """Remove uploaded-file control markers so user intent can be parsed cleanly."""
    normalized = normalize_uploaded_file_context(text)
    stripped = FILE_TEXT_RE.sub("", normalized)
    stripped = stripped.replace(
        "The uploaded file has already been processed by the application. Do not use tools. Do not say you cannot access it.",
        "",
    )
    stripped = stripped.replace(
        "When the user asks for text in the uploaded file, return only the text between FILE_TEXT markers.",
        "",
    )
    return re.sub(r"\n{3,}", "\n\n", stripped).strip()


def extract_latest_docx_bytes(messages: list[AnyMessage]) -> bytes:
    """Return the most recent attached DOCX bytes from the conversation, if present."""
    for message in reversed(messages):
        if message.type != "human":
            continue
        content = message.content
        if not isinstance(content, list):
            continue
        for block in reversed(content):
            if not isinstance(block, dict):
                continue
            if block.get("type") != "file":
                continue
            mime_type = normalize_mime_type(
                str(
                    (block.get("metadata") or {}).get("filename")
                    or (block.get("metadata") or {}).get("name")
                    or ""
                ),
                str(block.get("mimeType") or block.get("mime_type") or ""),
            )
            if mime_type != DOCX_MIME_TYPE:
                continue
            data = block.get("data")
            if not isinstance(data, str) or not data.strip():
                continue
            try:
                return base64.b64decode(data, validate=False)
            except Exception:
                continue
    return b""


def extract_spreadsheet_targets(text: str) -> list[str]:
    """Extract likely Google Sheets URLs or spreadsheet IDs from user text."""
    targets: list[str] = []
    for match in SPREADSHEET_URL_RE.finditer(text):
        targets.append(match.group(0))
    if targets:
        return targets

    if "spreadsheet" not in text.lower() and "sheet" not in text.lower():
        return []

    for match in SPREADSHEET_ID_RE.finditer(text):
        candidate = match.group(0)
        if candidate not in targets:
            targets.append(candidate)
    return targets


def strip_spreadsheet_targets(text: str, targets: list[str]) -> str:
    """Remove spreadsheet URLs/IDs from text so the remaining intent is clearer."""
    stripped = text
    for target in targets:
        stripped = stripped.replace(target, " ")

    stripped = re.sub(r"\s+", " ", stripped).strip(" :\n\t")
    return stripped


def build_spreadsheet_alias_map(targets: list[str]) -> list[tuple[str, str]]:
    """Assign stable, human-readable aliases to spreadsheet targets."""
    aliases: list[tuple[str, str]] = []
    for index, target in enumerate(targets):
        alias = f"TARGET_{chr(ord('A') + index)}"
        aliases.append((alias, target))
    return aliases


def looks_like_spreadsheet_analysis_request(text: str) -> bool:
    """Return whether the user is probably asking to inspect spreadsheet data."""
    lowered = text.lower()
    analysis_keywords = (
        "analy",
        "summary",
        "summarize",
        "insight",
        "review",
        "count",
        "trend",
        "chart",
        "graph",
        "data",
        "sheet",
        "spreadsheet",
        "วิเคราะห์",
        "สรุป",
        "กราฟ",
        "ชาร์ต",
        "ข้อมูล",
        "ชีต",
        "สเปรดชีต",
        "ดูแนวโน้ม",
        "นับ",
        "เปรียบเทียบ",
    )
    return any(keyword in lowered for keyword in analysis_keywords)


def looks_like_database_request(text: str) -> bool:
    """Return whether the user is probably asking about the Postgres response store."""
    lowered = str(text or "").strip().casefold()
    if not lowered:
        return False
    if _extract_requested_form_id(lowered):
        return True
    strong_keywords = (
        "postgres",
        "postgresql",
        "database",
        "sql",
        "ฐานข้อมูล",
        "โพสต์เกรส",
    )
    if any(keyword in lowered for keyword in strong_keywords):
        return True
    return re.search(r"(?<![a-z0-9_])db(?![a-z0-9_])", lowered) is not None


def _markdown_table_cell(value: Any) -> str:
    if value is None:
        return "-"
    text = str(value).strip()
    if not text:
        return "-"
    return text.replace("|", "\\|").replace("\r", " ").replace("\n", "<br />")


def _format_markdown_table(
    headers: Sequence[str],
    rows: Sequence[Sequence[Any]],
) -> str:
    if not headers:
        return ""

    lines = [
        "| " + " | ".join(_markdown_table_cell(header) for header in headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        lines.append(
            "| " + " | ".join(_markdown_table_cell(cell) for cell in row) + " |"
        )
    return "\n".join(lines).strip()


def _extract_requested_form_id(text: str) -> str:
    """Extract an explicit form_id reference from free-form user text."""
    raw_text = str(text or "")
    match = re.search(
        r"\bform(?:\s*[_-]?\s*id)?\s*[:=]\s*([A-Za-z0-9_-]{10,})\b",
        raw_text,
        flags=re.IGNORECASE,
    )
    if match:
        return match.group(1).strip()
    url_match = re.search(
        r"https://docs\.google\.com/forms/d/([A-Za-z0-9_-]{10,})/(?:edit|viewform)\b",
        raw_text,
        flags=re.IGNORECASE,
    )
    if url_match:
        return url_match.group(1).strip()

    lowered = raw_text.casefold()
    contextual_form_markers = (
        "form id",
        "formid",
        "from form",
        "this form",
        "previous form",
        "last form",
        "ฟอร์มนี้",
        "จากฟอร์ม",
        "จากแบบฟอร์ม",
        "จากแบบทดสอบ",
        "แบบฟอร์มนี้",
        "แบบทดสอบนี้",
        "analyze form",
        "query form",
        "ask form",
        "วิเคราะห์ฟอร์ม",
        "วิเคราะห์แบบฟอร์ม",
        "ดูฟอร์ม",
        "ดูข้อมูลฟอร์ม",
    )
    if any(
        marker in lowered for marker in contextual_form_markers
    ):
        bare_id_match = re.search(r"\b([A-Za-z0-9_-]{20,})\b", raw_text)
        if bare_id_match:
            return bare_id_match.group(1).strip()
    return ""


def _extract_form_id_from_message_text(text: str) -> str:
    """Extract a form id from prior thread text such as a creation result message."""
    explicit = _extract_requested_form_id(text)
    if explicit:
        return explicit

    line_match = re.search(
        r"(?:Form ID|รหัสฟอร์ม)\s*:\s*([A-Za-z0-9_-]{10,})",
        str(text or ""),
        flags=re.IGNORECASE,
    )
    if line_match:
        return line_match.group(1).strip()

    edit_url_match = re.search(
        r"https://docs\.google\.com/forms/d/([A-Za-z0-9_-]{10,})/edit\b",
        str(text or ""),
        flags=re.IGNORECASE,
    )
    if edit_url_match:
        return edit_url_match.group(1).strip()
    return ""


def _extract_recent_thread_form_id(messages: list[AnyMessage], before_index: int) -> str:
    """Return the most recent created-form id mentioned earlier in the same thread."""
    for index in range(before_index - 1, -1, -1):
        message = messages[index]
        if message.type not in {"ai", "human"}:
            continue
        form_id = _extract_form_id_from_message_text(content_to_text(message.content))
        if form_id:
            return form_id
    return ""


def looks_like_form_database_link_request(text: str) -> bool:
    """Return whether the user is explicitly asking to sync/link a form into SQL."""
    lowered = str(text or "").casefold()
    link_markers = (
        "link form",
        "link this form",
        "sync form",
        "sync this form",
        "store this form",
        "save this form",
        "add this form",
        "เชื่อมฟอร์ม",
        "เชื่อมแบบฟอร์ม",
        "เชื่อมแบบทดสอบ",
        "ซิงก์ฟอร์ม",
        "ซิงก์แบบฟอร์ม",
        "บันทึกฟอร์ม",
        "เก็บฟอร์ม",
        "เพิ่มฟอร์ม",
    )
    has_database_signal = any(
        marker in lowered
        for marker in (
            "postgres",
            "database",
            "sql",
            "ฐานข้อมูล",
            "โพสต์เกรส",
        )
    ) or bool(re.search(r"\bdb\b", lowered))
    return has_database_signal and any(marker in lowered for marker in link_markers)


def _load_agent_form_catalog() -> list[dict[str, Any]]:
    """Load agent-managed form metadata from Postgres."""
    payload = json.loads(
        query_form_response_database.invoke(
            {
                "sql": (
                    "SELECT form_id, spreadsheet_id, form_title, form_url, spreadsheet_url, updated_at "
                    "FROM agent_forms ORDER BY updated_at DESC NULLS LAST, created_at DESC NULLS LAST"
                ),
                "row_limit": 500,
            }
        )
    )
    rows = payload.get("rows", [])
    return [row for row in rows if isinstance(row, dict)]


def _choose_database_analysis_form(
    request_text: str,
    catalog_rows: list[dict[str, Any]],
    *,
    preferred_form_id: str = "",
) -> dict[str, Any] | None:
    """Resolve which stored form the SQL-backed analysis should target."""
    if not catalog_rows:
        return None

    normalized_preferred_form_id = str(preferred_form_id or "").strip()
    if normalized_preferred_form_id:
        for row in catalog_rows:
            if str(row.get("form_id", "") or "").strip() == normalized_preferred_form_id:
                return row

    explicit_form_id = _extract_requested_form_id(request_text)
    if explicit_form_id:
        for row in catalog_rows:
            if str(row.get("form_id", "") or "").strip() == explicit_form_id:
                return row

    lowered = str(request_text or "").strip().casefold()
    for row in catalog_rows:
        title = str(row.get("form_title", "") or "").strip()
        if title and title.casefold() in lowered:
            return row

    return catalog_rows[0]


def _quote_sql_string_literal(value: str) -> str:
    """Quote a string value for internal read-only SQL composition."""
    return "'" + str(value or "").replace("'", "''") + "'"


def _get_form_sheet_link_details(form_id: str) -> dict[str, Any]:
    """Return locally tracked link metadata for a form id when available."""
    details = _load_form_sheet_links().get(str(form_id or "").strip(), {})
    return dict(details) if isinstance(details, dict) else {}


def _sync_form_id_into_response_store(form_id: str) -> dict[str, Any]:
    """Sync a target form into the SQL response store using known metadata when available."""
    normalized_form_id = str(form_id or "").strip()
    if not normalized_form_id:
        return {
            "ok": False,
            "status": "missing-form-id",
            "error": "form_id is required",
            "store": _describe_form_response_store(),
        }

    details = _get_form_sheet_link_details(normalized_form_id)
    session_key = _resolve_agent_form_session_key(normalized_form_id, details)
    token_session = GOOGLE_OAUTH_SESSION_KEY.set(session_key)
    try:
        return _best_effort_sync_agent_form_responses(
            normalized_form_id,
            spreadsheet_id=str(details.get("spreadsheetId", "") or ""),
            form_title=str(details.get("spreadsheetTitle", "") or ""),
            form_url=str(details.get("formUrl", "") or ""),
            responder_url=str(details.get("responseUrl", "") or ""),
            spreadsheet_url=str(details.get("spreadsheetUrl", "") or ""),
            google_oauth_session_key=session_key or "",
        )
    finally:
        GOOGLE_OAUTH_SESSION_KEY.reset(token_session)


def _build_postgres_form_analysis_snapshot(
    form_row: dict[str, Any],
    *,
    analysis_request: str,
    user_language: str,
) -> tuple[dict[str, Any], int, int]:
    """Build chart-ready analysis payload from SQL-backed form response data."""
    form_id = str(form_row.get("form_id", "") or "").strip()
    spreadsheet_id = str(form_row.get("spreadsheet_id", "") or "").strip()
    form_title = str(form_row.get("form_title", "") or form_id).strip() or form_id
    spreadsheet_url = str(form_row.get("spreadsheet_url", "") or "").strip()
    quoted_form_id = _quote_sql_string_literal(form_id)

    responses_payload = json.loads(
        query_form_response_database.invoke(
            {
                "sql": (
                    "SELECT response_id, created_time, respondent_email "
                    f"FROM form_responses WHERE form_id = {quoted_form_id} "
                    "ORDER BY created_time ASC NULLS LAST, response_id ASC"
                ),
                "row_limit": 10000,
            }
        )
    )
    answers_payload = json.loads(
        query_form_response_database.invoke(
            {
                "sql": (
                    "SELECT response_id, question_title, answer_text "
                    f"FROM form_response_answers WHERE form_id = {quoted_form_id} "
                    "ORDER BY response_id ASC, question_title ASC"
                ),
                "row_limit": 50000,
            }
        )
    )

    response_rows = [
        row for row in responses_payload.get("rows", []) if isinstance(row, dict)
    ]
    answer_rows = [
        row for row in answers_payload.get("rows", []) if isinstance(row, dict)
    ]

    question_titles: list[str] = []
    for row in answer_rows:
        question_title = str(row.get("question_title", "") or "").strip()
        if question_title and question_title not in question_titles:
            question_titles.append(question_title)

    answer_matrix: dict[str, dict[str, str]] = {}
    question_counts: dict[str, dict[str, int]] = {}
    response_ids_with_answers: set[str] = set()
    for row in answer_rows:
        response_id = str(row.get("response_id", "") or "").strip()
        question_title = str(row.get("question_title", "") or "").strip()
        answer_text = str(row.get("answer_text", "") or "").strip()
        if not response_id or not question_title:
            continue
        response_ids_with_answers.add(response_id)
        answer_matrix.setdefault(response_id, {})[question_title] = answer_text
        normalized_answer = answer_text or "-"
        question_counts.setdefault(question_title, {})
        question_counts[question_title][normalized_answer] = (
            question_counts[question_title].get(normalized_answer, 0) + 1
        )

    processed_headers = ["Response ID", "Submitted At", "Respondent Email", *question_titles]
    processed_rows: list[list[str]] = []
    for response in response_rows:
        response_id = str(response.get("response_id", "") or "").strip()
        answer_lookup = answer_matrix.get(response_id, {})
        processed_rows.append(
            [
                response_id,
                str(response.get("created_time", "") or "").strip(),
                str(response.get("respondent_email", "") or "").strip(),
                *[str(answer_lookup.get(question_title, "") or "").strip() for question_title in question_titles],
            ]
        )

    summary_rows: list[list[str]] = []
    for question_title in question_titles:
        counts = question_counts.get(question_title, {})
        total = sum(counts.values())
        if total <= 0:
            continue
        ordered_answers = sorted(
            counts.items(),
            key=lambda item: (-int(item[1]), str(item[0]).casefold()),
        )
        for answer_text, count in ordered_answers:
            percent = round((count / total) * 100, 2)
            summary_rows.append(
                [
                    question_title,
                    answer_text,
                    str(count),
                    f"{percent:.2f}",
                ]
            )

    visual_payload = _build_spreadsheet_visual_payload(
        spreadsheet_id=spreadsheet_id or form_id,
        spreadsheet_title=form_title,
        spreadsheet_url=spreadsheet_url,
        processed_sheet_name="SQL Responses",
        summary_sheet_name="SQL Summary",
        row_count_written=len(processed_rows),
        summary_rows=summary_rows,
        analysis_request=analysis_request,
        processed_headers=processed_headers,
        processed_rows=processed_rows,
        user_language=user_language,
    )
    return visual_payload, len(response_ids_with_answers), len(question_titles)


def looks_like_form_creation_request(text: str) -> bool:
    """Return whether the user is probably asking to create or modify a form."""
    lowered = text.lower()
    if "spreadsheet" in lowered and "analy" in lowered:
        return False
    explicit_form_id = _extract_requested_form_id(text)
    explicit_creation_markers = (
        "create form",
        "create a form",
        "create google form",
        "make form",
        "build form",
        "generate form",
        "new form",
        "สร้างฟอร์ม",
        "สร้างแบบฟอร์ม",
        "สร้างแบบทดสอบ",
        "สร้าง google form",
        "ทำฟอร์ม",
        "ทำแบบฟอร์ม",
        "ทำแบบทดสอบ",
    )
    if explicit_form_id and looks_like_spreadsheet_analysis_request(text):
        return False
    if explicit_form_id:
        if not any(marker in lowered for marker in explicit_creation_markers):
            return False
    if looks_like_spreadsheet_analysis_request(text) and any(
        marker in lowered
        for marker in (
            "this form",
            "previous form",
            "last form",
            "form นี้",
            "ฟอร์มนี้",
            "ฟอร์มก่อนหน้า",
            "แบบฟอร์มนี้",
            "แบบทดสอบนี้",
        )
    ):
        return False

    creation_keywords = (
        "create a form",
        "create form",
        "google form",
        "pre-test",
        "pretest",
        "post-test",
        "posttest",
        "question",
        "multiple-choice",
        "multiple choice",
        "แบบทดสอบ",
        "แบบฟอร์ม",
        "ฟอร์ม",
        "คำถาม",
    )
    if not any(keyword in lowered for keyword in creation_keywords):
        return False

    if re.search(r"\b[A-Za-z0-9_-]{20,}\b", text or "") and not any(
        marker in lowered for marker in explicit_creation_markers
    ):
        return False

    return True


def _trim_form_topic_tail(text: str) -> str:
    """Trim trailing requirement clauses from an inferred topic string."""
    cleaned = re.sub(r"\s+", " ", text).strip().strip(" .,:;-")
    split_patterns = (
        r"\s+with\s+",
        r"\s+including\s+",
        r"\s+having\s+",
        r"\s+and\s+",
        r"\s+จำนวน\s+\d+\s+ข้อ",
        r"\s+โดยมี\s+",
        r"\s+พร้อม\s+",
        r"\s+มี\s+(?:ชื่อ|หน่วยงาน|เบอร์|อีเมล|แบบทดสอบ|คำถาม)",
        r"\s+และ(?:มี|แบบทดสอบ|คำถาม)",
    )
    for pattern in split_patterns:
        parts = re.split(pattern, cleaned, maxsplit=1, flags=re.IGNORECASE)
        if parts:
            cleaned = parts[0].strip().strip(" .,:;-")
    return cleaned


def _strip_leading_title_variant(text: str, variants: tuple[str, ...]) -> str:
    cleaned = str(text or "").strip()
    lowered = cleaned.casefold()
    for variant in variants:
        normalized_variant = variant.casefold()
        if lowered.startswith(normalized_variant):
            return cleaned[len(variant):].strip(" -:|,")
    return cleaned


def _normalize_generated_form_title(title: str) -> str:
    cleaned = re.sub(r"\s+", " ", str(title or "")).strip().strip(" .,:;-")
    if not cleaned:
        return "Generated Google Form"

    # Remove respondent-field suffixes that belong in questions, not in the title.
    cleaned = re.split(
        r"\s+โดยมี(?:ชื่อ|ชื่อ-นามสกุล|หน่วยงาน|เบอร์โทร|เบอร์โทรศัพท์|อีเมล)\b",
        cleaned,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0].strip(" .,:;-")
    cleaned = re.split(
        r"\s+(?:with|including|having)\s+(?:name|full name|department|organization|phone|email)\b",
        cleaned,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0].strip(" .,:;-")

    duplicate_prefix_pairs = (
        ("แบบทดสอบก่อนการอบรม", "แบบทดสอบก่อนอบรม"),
        ("แบบทดสอบก่อนการอบรม", "แบบทดสอบก่อนการอบรม"),
        ("แบบทดสอบหลังการอบรม", "แบบทดสอบหลังอบรม"),
        ("แบบทดสอบหลังการอบรม", "แบบทดสอบหลังการอบรม"),
        ("แบบประเมิน", "แบบประเมิน"),
        ("Pre-test Form", "Pre-test"),
        ("Post-test Form", "Post-test"),
        ("Feedback Form", "Feedback"),
    )
    lowered = cleaned.casefold()
    for primary, duplicate in duplicate_prefix_pairs:
        primary_folded = primary.casefold()
        duplicate_folded = duplicate.casefold()
        doubled = f"{primary_folded} {duplicate_folded}"
        if lowered.startswith(doubled):
            cleaned = f"{primary} {cleaned[len(primary) + 1 + len(duplicate):].strip()}".strip()
            lowered = cleaned.casefold()
            break

    return re.sub(r"\s+", " ", cleaned).strip(" .,:;-") or "Generated Google Form"


def _extract_explicit_form_title(text: str) -> str:
    lines = [line.rstrip() for line in str(text or "").splitlines()]
    for index, raw_line in enumerate(lines):
        line = raw_line.strip()
        if line.lower() == "title:" and index + 1 < len(lines):
            candidate = lines[index + 1].strip()
            if candidate:
                return _normalize_generated_form_title(candidate)
        if line.lower().startswith("title:"):
            candidate = line.split(":", 1)[1].strip()
            if candidate:
                return _normalize_generated_form_title(candidate)
    return ""


def extract_form_topic(text: str) -> str:
    """Extract the main subject/topic of the requested form when possible."""
    lowered = text.lower()
    patterns = (
        r"(?:related to|about|for)\s+([^\n.,]+)",
        r"(?:เกี่ยวกับ|เรื่อง)\s*([^\n.,]+)",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            topic = _trim_form_topic_tail(match.group(1))
            if topic:
                return topic

    cleaned = re.sub(r"\s+", " ", text).strip()
    cleaned = re.sub(
        r"^(create|make|generate|build)\s+(a|an)?\s*(google\s+form|form)\s*",
        "",
        cleaned,
        flags=re.IGNORECASE,
    ).strip(" .,:;-")
    cleaned = re.sub(
        r"^(สร้าง|ทำ|ช่วยสร้าง)\s*(google\s+form|ฟอร์ม|แบบฟอร์ม|แบบทดสอบ)?\s*",
        "",
        cleaned,
        flags=re.IGNORECASE,
    ).strip(" .,:;-")
    return _trim_form_topic_tail(cleaned[:120].strip())


def infer_default_question_count(text: str) -> int:
    """Infer a sensible default question count for short/simple prompts."""
    explicit_count = extract_question_count(text)
    if explicit_count:
        return explicit_count

    lowered = text.casefold()
    if any(keyword in lowered for keyword in ("pre-test", "pretest", "post-test", "posttest", "quiz", "test", "แบบทดสอบ")):
        return 10
    if any(keyword in lowered for keyword in ("feedback", "survey", "satisfaction", "rating", "แบบประเมิน", "ความพึงพอใจ")):
        return 5
    return 0


def infer_form_is_quiz(text: str, questions: list[dict[str, Any]] | None = None) -> bool:
    """Infer whether the requested form should behave as a quiz from user context."""
    lowered = str(text or "").casefold()

    explicit_non_quiz_patterns = (
        r"\bnot a quiz\b",
        r"\bdon't make (?:it )?a quiz\b",
        r"\bdo not make (?:it )?a quiz\b",
        r"\bno grading\b",
        r"\bwithout grading\b",
        r"ไม่ต้องเป็นแบบทดสอบ",
        r"ไม่ต้องเป็นควิซ",
        r"ไม่ต้องให้คะแนน",
        r"ไม่ต้องตรวจคำตอบ",
    )
    if any(re.search(pattern, lowered, re.IGNORECASE) for pattern in explicit_non_quiz_patterns):
        return False

    quiz_keywords = (
        "pre-test",
        "pretest",
        "post-test",
        "posttest",
        "quiz",
        "test",
        "exam",
        "assessment",
        "answer key",
        "graded",
        "grading",
        "คะแนน",
        "เฉลย",
        "คำตอบที่ถูก",
        "ตรวจคำตอบ",
        "แบบทดสอบ",
        "ข้อสอบ",
        "ควิซ",
        "ก่อนเรียน",
        "ก่อนอบรม",
        "หลังเรียน",
        "หลังอบรม",
    )
    if any(keyword in lowered for keyword in quiz_keywords):
        return True

    non_quiz_keywords = (
        "feedback",
        "survey",
        "satisfaction",
        "registration",
        "register",
        "signup",
        "sign-up",
        "application",
        "rsvp",
        "attendance",
        "แบบประเมิน",
        "ความพึงพอใจ",
        "ลงทะเบียน",
        "สมัคร",
        "แบบฟอร์มสมัคร",
    )
    if any(keyword in lowered for keyword in non_quiz_keywords):
        return False

    if questions:
        correct_answer_count = sum(
            1
            for question in questions
            if isinstance(question, dict)
            and isinstance(question.get("correct_answers", []), list)
            and any(str(answer or "").strip() for answer in question.get("correct_answers", []))
        )
        if correct_answer_count > 0:
            title_hints = " ".join(
                str(question.get("title", "") or "")
                for question in questions[:3]
                if isinstance(question, dict)
            ).casefold()
            if any(keyword in title_hints for keyword in ("แบบทดสอบ", "ข้อสอบ", "pre-test", "post-test", "quiz", "test", "exam")):
                return True
            return True

    return False


def extract_form_title(text: str) -> str:
    """Extract a form title from common prompt patterns."""
    explicit_title = _extract_explicit_form_title(text)
    if explicit_title:
        return explicit_title

    topic = extract_form_topic(text)
    lowered = text.casefold()
    if any(keyword in lowered for keyword in ("pre-test", "pretest", "แบบทดสอบก่อน", "แบบทดสอบ")):
        if "thai" in lowered or "ภาษาไทย" in lowered or re.search(r"[\u0E00-\u0E7F]", text):
            normalized_topic = _strip_leading_title_variant(
                topic,
                (
                    "แบบทดสอบก่อนการอบรม",
                    "แบบทดสอบก่อนอบรม",
                    "แบบทดสอบก่อนเรียน",
                    "ข้อสอบก่อนการอบรม",
                    "ข้อสอบก่อนอบรม",
                ),
            )
            return _normalize_generated_form_title(f"แบบทดสอบก่อนการอบรม {normalized_topic}".strip())
        normalized_topic = _strip_leading_title_variant(
            topic,
            ("Pre-test Form", "Pre-test", "Pretest Form", "Pretest", "Test"),
        )
        return _normalize_generated_form_title(f"Pre-test Form - {normalized_topic}".strip(" -"))
    if any(keyword in lowered for keyword in ("post-test", "posttest", "แบบทดสอบหลัง")):
        if "thai" in lowered or "ภาษาไทย" in lowered or re.search(r"[\u0E00-\u0E7F]", text):
            normalized_topic = _strip_leading_title_variant(
                topic,
                (
                    "แบบทดสอบหลังการอบรม",
                    "แบบทดสอบหลังอบรม",
                    "แบบทดสอบหลังเรียน",
                    "ข้อสอบหลังการอบรม",
                    "ข้อสอบหลังอบรม",
                ),
            )
            return _normalize_generated_form_title(f"แบบทดสอบหลังการอบรม {normalized_topic}".strip())
        normalized_topic = _strip_leading_title_variant(
            topic,
            ("Post-test Form", "Post-test", "Posttest Form", "Posttest", "Test"),
        )
        return _normalize_generated_form_title(f"Post-test Form - {normalized_topic}".strip(" -"))
    if any(keyword in lowered for keyword in ("feedback", "survey", "satisfaction", "แบบประเมิน", "ความพึงพอใจ")):
        if "thai" in lowered or "ภาษาไทย" in lowered or re.search(r"[\u0E00-\u0E7F]", text):
            normalized_topic = _strip_leading_title_variant(
                topic,
                ("แบบประเมิน", "แบบสอบถาม", "แบบสำรวจ"),
            )
            return _normalize_generated_form_title(f"แบบประเมิน {normalized_topic}".strip())
        normalized_topic = _strip_leading_title_variant(
            topic,
            ("Feedback Form", "Feedback", "Survey Form", "Survey"),
        )
        return _normalize_generated_form_title(f"Feedback Form - {normalized_topic}".strip(" -"))
    if topic:
        return _normalize_generated_form_title(topic)
    return _normalize_generated_form_title("Generated Google Form")


def _choose_form_title_with_agent(
    source_text: str,
    *,
    user_language: str,
    respondent_questions: list[dict[str, Any]] | None = None,
    source_questions: list[dict[str, Any]] | None = None,
) -> str:
    explicit_title = _extract_explicit_form_title(source_text)
    if explicit_title:
        return explicit_title

    heuristic_title = extract_form_title(source_text)
    normalized_brief = str(source_text or "").strip()
    if not normalized_brief:
        return heuristic_title

    respondent_titles = [
        str(question.get("title", "") or "").strip()
        for question in (respondent_questions or [])
        if str(question.get("title", "") or "").strip()
    ]
    source_question_titles = [
        str(question.get("title", "") or "").strip()
        for question in (source_questions or [])[:8]
        if str(question.get("title", "") or "").strip()
    ]

    prompt_language = "Thai" if user_language == "th" else "English"
    model = build_chat_model()
    response = model.invoke(
        [
            SystemMessage(
                content=(
                    "You choose concise, user-facing Google Form titles. "
                    "Return only the final title text. "
                    "Do not include quotes, bullets, labels, or explanations. "
                    "Do not include respondent-information field names such as name, department, phone, or email in the title. "
                    "Do not duplicate prefixes like pre-test, post-test, feedback, or survey. "
                    "Keep the title clear and natural."
                )
            ),
            HumanMessage(
                content=(
                    f"User language: {prompt_language}\n"
                    f"Original request:\n{normalized_brief}\n\n"
                    f"Heuristic fallback title:\n{heuristic_title}\n\n"
                    + (
                        "Respondent information fields already handled separately:\n"
                        + "\n".join(f"- {title}" for title in respondent_titles)
                        + "\n\n"
                        if respondent_titles
                        else ""
                    )
                    + (
                        "Question titles from the source content:\n"
                        + "\n".join(f"- {title}" for title in source_question_titles)
                        + "\n\n"
                        if source_question_titles
                        else ""
                    )
                    + (
                        "Choose the best actual form title in Thai."
                        if user_language == "th"
                        else "Choose the best actual form title in English."
                    )
                )
            ),
        ]
    )
    candidate = content_to_text(response.content).strip().splitlines()[0].strip("`\"' ")
    normalized_candidate = _normalize_generated_form_title(candidate)
    return normalized_candidate or heuristic_title


def extract_form_description(text: str) -> str:
    """Extract a description block from common prompt patterns."""
    lines = text.splitlines()
    collected: list[str] = []
    capture = False
    for raw_line in lines:
        line = raw_line.strip()
        lowered = line.lower()
        if lowered == "description:":
            capture = True
            continue
        if lowered.startswith("description:"):
            capture = True
            remainder = line.split(":", 1)[1].strip()
            if remainder:
                collected.append(remainder)
            continue
        if capture and (
            lowered.startswith("include these points")
            or lowered.startswith("then generate")
            or lowered.startswith("requirements")
        ):
            break
        if capture:
            if line:
                collected.append(line)
            elif collected:
                break
    description = "\n".join(collected).strip()
    if description:
        return description
    return ""


def infer_default_section_structure(
    text: str,
    respondent_questions: list[dict[str, Any]],
    expected_question_count: int,
) -> dict[str, dict[str, str]]:
    """Infer a simple two-part structure for short natural prompts."""
    if not respondent_questions or expected_question_count <= 0:
        return {}

    lowered = text.casefold()
    is_thai = "thai" in lowered or "ภาษาไทย" in lowered or bool(re.search(r"[\u0E00-\u0E7F]", text))

    if any(keyword in lowered for keyword in ("pre-test", "pretest", "แบบทดสอบก่อน")):
        if is_thai:
            return {
                "section_1": {"title": "ข้อมูลผู้เข้าอบรม"},
                "section_2": {"title": "แบบทดสอบก่อนการอบรม"},
            }
        return {
            "section_1": {"title": "Participant Information"},
            "section_2": {"title": "Pre-test"},
        }

    if any(keyword in lowered for keyword in ("post-test", "posttest", "แบบทดสอบหลัง")):
        if is_thai:
            return {
                "section_1": {"title": "ข้อมูลผู้เข้าอบรม"},
                "section_2": {"title": "แบบทดสอบหลังการอบรม"},
            }
        return {
            "section_1": {"title": "Participant Information"},
            "section_2": {"title": "Post-test"},
        }

    if any(keyword in lowered for keyword in ("feedback", "survey", "แบบประเมิน", "ความพึงพอใจ")):
        if is_thai:
            return {
                "section_1": {"title": "ข้อมูลผู้ตอบแบบประเมิน"},
                "section_2": {"title": "แบบประเมิน"},
            }
        return {
            "section_1": {"title": "Respondent Information"},
            "section_2": {"title": "Feedback Questions"},
        }

    if is_thai:
        return {
            "section_1": {"title": "ข้อมูลผู้เข้าอบรม"},
            "section_2": {"title": "คำถามหลัก"},
        }
    return {
        "section_1": {"title": "Participant Information"},
        "section_2": {"title": "Main Questions"},
    }


def extract_question_count(text: str) -> int | None:
    """Extract requested question count when present."""
    patterns = (
        r"(\d+)\s+required\s+multiple[- ]choice\s+questions",
        r"(\d+)\s+multiple[- ]choice\s+questions",
        r"จำนวน\s+(\d+)\s+ข้อ",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            try:
                return int(match.group(1))
            except ValueError:
                return None
    return None


def prefers_exact_source_following(text: str) -> bool:
    """Return whether the user wants the attached source followed closely."""
    lowered = text.casefold()
    keywords = (
        "follow the file",
        "follow the attached file",
        "follow exactly",
        "use the file as source of truth",
        "use the attached file as source of truth",
        "ยึดตามไฟล์",
        "ยึดตามไฟล์แนบ",
        "ตามไฟล์แนบ",
        "ตามต้นฉบับ",
        "ให้ตรงตามไฟล์",
        "ให้ตรงตามต้นฉบับ",
        "ต้นฉบับหลัก",
    )
    return any(keyword in lowered for keyword in keywords)


def extract_inline_respondent_questions(text: str) -> list[dict[str, Any]]:
    """Extract simple respondent fields mentioned inline in natural prompts."""
    lowered = text.casefold()
    keyword_map: list[tuple[str, str]] = [
        ("name", "Name"),
        ("full name", "Full Name"),
        ("department", "Department"),
        ("organization", "Organization"),
        ("company", "Company"),
        ("school", "School"),
        ("phone", "Phone"),
        ("email", "Email"),
        ("role", "Role"),
        ("position", "Position"),
        ("ชื่อ", "ชื่อ-นามสกุล"),
        ("หน่วยงาน", "หน่วยงาน"),
        ("สถานศึกษา", "สถานศึกษา"),
        ("โทร", "เบอร์โทรศัพท์"),
        ("อีเมล", "อีเมล"),
        ("ตำแหน่ง", "ตำแหน่ง"),
    ]
    matches: list[tuple[int, str]] = []
    for keyword, label in keyword_map:
        position = lowered.find(keyword.casefold())
        if position != -1:
            matches.append((position, label))
    matches.sort(key=lambda item: item[0])

    seen: set[str] = set()
    questions: list[dict[str, Any]] = []
    for _position, label in matches:
        normalized = label.casefold()
        if normalized in seen:
            continue
        questions.append(
            {
                "title": label,
                "type": "text",
                "required": True,
            }
        )
        seen.add(normalized)
    return questions


def should_include_default_respondent_info(text: str) -> bool:
    """Return whether the request likely needs a standard respondent-info block."""
    lowered = text.lower()
    respondent_keywords = (
        "participant information",
        "respondent information",
        "ข้อมูลผู้เข้าอบรม",
        "ข้อมูลผู้เข้ารับการอบรม",
        "pre-test",
        "pretest",
        "training course",
        "อบรม",
        "ผู้เข้าอบรม",
        "ผู้เข้ารับการอบรม",
    )
    return any(keyword in lowered for keyword in respondent_keywords)


def _is_instructional_prompt_line(text: str) -> bool:
    """Return whether a line reads like prompt instruction rather than form content."""
    lowered = text.strip().casefold()
    if not lowered:
        return False
    instructional_prefixes = (
        "add these required",
        "then generate",
        "then create",
        "generate ",
        "create ",
        "include these points",
        "rules for the",
        "requirements",
        "important",
        "do not ",
        "also create",
        "after finishing",
        "pass that exact list",
        "respondent information questions explicitly requested",
        "section structure explicitly requested",
    )
    return lowered.startswith(instructional_prefixes)


def _is_placeholder_content(text: str) -> bool:
    """Return whether a line looks like an unfinished placeholder."""
    lowered = text.strip().casefold()
    placeholder_markers = (
        "following the same pattern",
        "more questions",
        "same pattern",
        "continue with",
        "etc.",
        "...",
    )
    return any(marker in lowered for marker in placeholder_markers)


def extract_requested_respondent_questions(text: str) -> list[dict[str, Any]]:
    """Extract respondent-information questions explicitly requested in the prompt."""
    lines = text.replace("\r\n", "\n").splitlines()
    capture = False
    questions: list[dict[str, Any]] = []
    current_question: dict[str, Any] | None = None

    for raw_line in lines:
        line = raw_line.rstrip()
        stripped = line.strip()
        lowered = stripped.casefold()

        if not capture and any(hint in lowered for hint in RESPONDENT_SECTION_HINTS):
            capture = True
            continue

        if capture and (
            lowered.startswith("then generate")
            or lowered.startswith("section 2")
            or "แบบทดสอบก่อนการอบรม" in lowered
            or lowered.startswith("rules for the")
        ):
            break

        if not capture or not stripped:
            continue
        if _is_instructional_prompt_line(stripped):
            continue

        item_match = re.match(r"^\d+[.)]?\s+(.+)$", stripped)
        if item_match:
            if current_question:
                questions.append(current_question)
            item_text = item_match.group(1).strip()
            parts = re.split(r"\s+[â€”-]\s+", item_text)
            title = parts[0].strip()
            question_type = "text"
            required = True
            for part in parts[1:]:
                lowered_part = part.casefold()
                if "multiple choice" in lowered_part or "multiple-choice" in lowered_part:
                    question_type = "multiple_choice"
                elif "checkbox" in lowered_part:
                    question_type = "checkbox"
                elif "dropdown" in lowered_part or "drop down" in lowered_part:
                    question_type = "dropdown"
                elif "short answer" in lowered_part or "text" in lowered_part:
                    question_type = "text"
                elif "optional" in lowered_part:
                    required = False
                elif "required" in lowered_part:
                    required = True
            current_question = {
                "title": title,
                "type": question_type,
                "required": required,
                "options": [],
            }
            continue

        option_match = re.match(r"^\s*[-*]\s+(.+)$", line)
        if option_match and current_question is not None:
            option = option_match.group(1).strip()
            if option and not _is_instructional_prompt_line(option):
                current_question.setdefault("options", []).append(option)
                if current_question.get("type") == "text":
                    current_question["type"] = "multiple_choice"

    if current_question:
        questions.append(current_question)

    extracted_questions: list[dict[str, Any]] = []
    for index, question in enumerate(questions, start=1):
        if not question.get("title"):
            continue
        extracted_questions.append(_normalize_question_dict(question, index))
    return extracted_questions


def extract_requested_section_structure(text: str) -> dict[str, dict[str, str]]:
    """Extract explicit section titles/descriptions from the prompt when present."""
    lines = text.replace("\r\n", "\n").splitlines()
    sections: dict[str, dict[str, str]] = {}
    current_key: str | None = None

    for raw_line in lines:
        stripped = raw_line.strip()
        if not stripped:
            continue

        match = re.match(r"^(Section\s*[12])\s*:\s*(.+)$", stripped, re.IGNORECASE)
        if match:
            current_key = "section_1" if match.group(1).casefold().startswith("section 1") else "section_2"
            sections[current_key] = {"title": match.group(2).strip()}
            continue

        lowered = stripped.casefold()
        if lowered in {"ข้อมูลผู้เข้าอบรม", "ข้อมูลผู้เข้ารับการอบรม"}:
            current_key = "section_1"
            sections[current_key] = {"title": stripped}
            continue
        if lowered in {"แบบทดสอบก่อนการอบรม", "แบบทดสอบหลังการอบรม"}:
            current_key = "section_2"
            sections[current_key] = {"title": stripped}
            continue

        if current_key and "description" not in sections[current_key]:
            if (
                not re.match(r"^\d+[.)]?\s+", stripped)
                and not stripped.startswith(("-", "*"))
                and not _is_instructional_prompt_line(stripped)
            ):
                sections[current_key]["description"] = stripped

    return sections


def compress_form_creation_request(text: str) -> str:
    """Condense a long form-creation prompt into a compact structured task."""
    title = extract_form_title(text)
    description = extract_form_description(text)
    question_count = infer_default_question_count(text)
    respondent_questions = extract_requested_respondent_questions(text)
    if not respondent_questions:
        respondent_questions = extract_inline_respondent_questions(text)
    section_structure = extract_requested_section_structure(text)
    if not section_structure:
        section_structure = infer_default_section_structure(
            text,
            respondent_questions,
            question_count,
        )
    lowered = text.lower()

    requirement_lines: list[str] = []
    if question_count:
        requirement_lines.append(
            f"- Generate {question_count} required multiple-choice questions."
        )
        requirement_lines.append(
            f"- Pass expected_question_count={question_count} to create_form_with_response_sheet."
        )
    if "thai" in lowered or "ภาษาไทย" in lowered:
        requirement_lines.append("- Write the form and questions in Thai.")
    if "4 choices" in lowered or "4 answer choices" in lowered or "4 choices." in lowered:
        requirement_lines.append("- Each multiple-choice question must have 4 choices.")
    if "do not add short-answer" in lowered or "do not add short answer" in lowered:
        requirement_lines.append("- Do not add short-answer questions.")
    if "do not collect email" in lowered:
        requirement_lines.append("- Do not collect email addresses.")
    if "do not include personal information" in lowered or "do not add personal information" in lowered:
        requirement_lines.append("- Do not include personal information fields.")
    topic_lines: list[str] = []
    for marker in (
        "related to ",
        "about ",
    ):
        marker_index = lowered.find(marker)
        if marker_index != -1:
            topic = text[marker_index + len(marker) :].splitlines()[0].strip().rstrip(".")
            if topic:
                topic_lines.append(f"- Topic: {topic}")
                break

    sections = [
        "FORM_CREATION_TASK",
        "This is a Google Form creation request.",
        (
            "Available local form tools in this agent: create_form_with_response_sheet "
            "and list_google_forms."
        ),
        (
            "Use tools. Prefer one create_form_with_response_sheet call with title, "
            "description, and questions_text when enough details are available."
        ),
    ]
    if title:
        sections.append(f"Title: {title}")
    if description:
        sections.append(f"Description:\n{description}")
    if requirement_lines or topic_lines:
        sections.append("Requirements:")
        sections.extend(requirement_lines)
        sections.extend(topic_lines)
    if respondent_questions:
        sections.append(
            "Respondent information questions explicitly requested by the user. Preserve these first and in this order:"
        )
        sections.append(json.dumps(respondent_questions, ensure_ascii=False))
        sections.append(
            "Pass that exact list in respondent_questions_json when calling create_form_with_response_sheet."
        )
    if section_structure:
        sections.append(
            "Section structure explicitly requested by the user. Preserve these section titles/descriptions and pass them in section_structure_json:"
        )
        sections.append(json.dumps(section_structure, ensure_ascii=False))
    sections.append("Pass the original user request in source_prompt exactly as received.")
    sections.append(
        "If you can infer the questions, prefer questions_text in this markdown-style format:\n"
        "### Question 1\n"
        "- Title: ...\n"
        "- Type: multiple_choice\n"
        "- Required: true\n"
        "- Description: ... (optional)\n"
        "- Options:\n"
        "  - A\n"
        "  - B\n"
        "  - C\n"
        "  - D\n\n"
        "### Question 2\n"
        "- Title: ...\n"
        "- Type: text\n"
        "- Required: true"
    )
    sections.append(
        "If you use questions_json, it must be strict JSON, but questions_text is preferred for long or complex prompts."
    )
    sections.append(
        "Do not say that tools are unavailable for Google Form creation. "
        "This request must use the available local form tools."
    )
    sections.append(
        "Treat imperative prompt lines as instructions only. Do not copy lines like 'Add these required...' or 'Then generate...' into the form."
    )
    sections.append(
        "Generate every requested question explicitly. Never use placeholders such as '... (13 more questions following the same pattern)'."
    )
    sections.append("Never invent or shorten IDs like '1aBcD...'. Use only exact IDs returned by tools.")
    sections.append("Always return the form ID, edit URL, and responder URL after successful creation.")
    sections.append("If any field is missing, ask the minimum necessary clarifying question.")
    return "\n".join(sections)


def extract_spreadsheet_id(target: str) -> str:
    """Return a spreadsheet id from either a full URL or a bare id."""
    if "..." in target:
        raise RuntimeError(
            "Spreadsheet ID appears truncated or placeholder-like. Use the full exact spreadsheet ID."
        )
    match = re.search(
        r"/spreadsheets/d/([a-zA-Z0-9-_]+)",
        target,
        re.IGNORECASE,
    )
    if match:
        return match.group(1)
    return target.strip()


def _load_google_credentials(
    scopes: list[str],
) -> service_account.Credentials | UserCredentials:
    """Load usable Google Workspace credentials from configured auth sources."""
    service_account_path = os.getenv("SERVICE_ACCOUNT_PATH")
    if service_account_path:
        candidate = Path(service_account_path).expanduser()
        if candidate.exists():
            return service_account.Credentials.from_service_account_file(
                str(candidate),
                scopes=scopes,
            )

    token_path = Path(
        os.getenv("TOKEN_PATH")
        or str(get_google_oauth_token_path(discover_single_session=True))
    ).expanduser()
    if token_path.exists():
        payload = json.loads(token_path.read_text(encoding="utf-8"))
        credentials = UserCredentials.from_authorized_user_info(
            payload,
            scopes=scopes,
        )
        if not credentials.valid and credentials.refresh_token:
            credentials.refresh(GoogleAuthRequest())
        return credentials

    refresh_token = load_google_refresh_token(discover_single_session=False)
    if refresh_token:
        credentials = UserCredentials(
            token=None,
            refresh_token=refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=get_required_env("GOOGLE_CLIENT_ID"),
            client_secret=get_required_env("GOOGLE_CLIENT_SECRET"),
            scopes=scopes,
        )
        credentials.refresh(GoogleAuthRequest())
        return credentials

    raise RuntimeError("No Google Workspace credentials are available.")


def _load_google_sheets_credentials() -> service_account.Credentials | UserCredentials:
    """Load usable Google Sheets credentials from configured auth sources."""
    return _load_google_credentials(GOOGLE_SHEETS_SCOPES)


def _load_google_workspace_credentials() -> service_account.Credentials | UserCredentials:
    """Load usable Google Forms/Sheets/Drive credentials from configured auth sources."""
    return _load_google_credentials(GOOGLE_WORKSPACE_SCOPES)


def _load_google_apps_script_credentials() -> service_account.Credentials | UserCredentials:
    """Load credentials with the additional Apps Script scopes required for script execution."""
    return _load_google_credentials(GOOGLE_APPS_SCRIPT_SCOPES)


def _quote_sheet_title(sheet_title: str) -> str:
    escaped = sheet_title.replace("'", "''")
    return f"'{escaped}'"


def _load_form_sheet_links() -> dict[str, dict[str, Any]]:
    """Load the local registry of form-to-sheet links."""
    if not FORM_SHEET_LINKS_PATH.exists():
        return {}

    try:
        payload = json.loads(FORM_SHEET_LINKS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}

    if not isinstance(payload, dict):
        return {}
    return {
        str(form_id): details
        for form_id, details in payload.items()
        if isinstance(form_id, str) and isinstance(details, dict)
    }


def _load_apps_script_config() -> dict[str, Any]:
    """Load persisted Apps Script project metadata used for native form linking."""
    if not GOOGLE_APPS_SCRIPT_CONFIG_PATH.exists():
        return {}

    try:
        payload = json.loads(GOOGLE_APPS_SCRIPT_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}

    return payload if isinstance(payload, dict) else {}


def _save_apps_script_config(config: dict[str, Any]) -> None:
    """Persist Apps Script project metadata used for native form linking."""
    GOOGLE_APPS_SCRIPT_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    GOOGLE_APPS_SCRIPT_CONFIG_PATH.write_text(
        json.dumps(config, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _upsert_apps_script_config(details: dict[str, Any]) -> None:
    """Insert or update persisted Apps Script metadata."""
    existing = _load_apps_script_config()
    existing.update(details)
    _save_apps_script_config(existing)


def _get_configured_apps_script_id() -> str:
    """Return the configured shared Apps Script project id from env or shared config."""
    configured = os.getenv("GOOGLE_APPS_SCRIPT_PROJECT_ID", "").strip()
    if configured:
        return configured
    stored = _load_apps_script_config().get("scriptId", "")
    return str(stored).strip() if stored else ""


def _get_configured_apps_script_deployment_id() -> str:
    """Return the configured shared Apps Script deployment id from env or shared config."""
    configured = os.getenv("GOOGLE_APPS_SCRIPT_DEPLOYMENT_ID", "").strip()
    if configured:
        return configured
    stored = _load_apps_script_config().get("deploymentId", "")
    return str(stored).strip() if stored else ""


def _get_locally_managed_apps_script_id() -> str:
    """Return the locally managed Apps Script project id for per-user fallback runtime."""
    config = _load_apps_script_config()
    session_key = GOOGLE_OAUTH_SESSION_KEY.get()
    if session_key:
        managed_sessions = config.get("managedSessions", {})
        if isinstance(managed_sessions, dict):
            session_entry = managed_sessions.get(session_key, {})
            if isinstance(session_entry, dict):
                stored = session_entry.get("scriptId", "")
                if stored:
                    return str(stored).strip()
    stored = config.get("managedScriptId", "")
    return str(stored).strip() if stored else ""


def _get_locally_managed_apps_script_deployment_id() -> str:
    """Return the locally managed Apps Script deployment id for per-user fallback runtime."""
    config = _load_apps_script_config()
    session_key = GOOGLE_OAUTH_SESSION_KEY.get()
    if session_key:
        managed_sessions = config.get("managedSessions", {})
        if isinstance(managed_sessions, dict):
            session_entry = managed_sessions.get(session_key, {})
            if isinstance(session_entry, dict):
                stored = session_entry.get("deploymentId", "")
                if stored:
                    return str(stored).strip()
    stored = config.get("managedDeploymentId", "")
    return str(stored).strip() if stored else ""


def _save_form_sheet_links(links: dict[str, dict[str, Any]]) -> None:
    """Persist the local registry of form-to-sheet links."""
    FORM_SHEET_LINKS_PATH.parent.mkdir(parents=True, exist_ok=True)
    FORM_SHEET_LINKS_PATH.write_text(
        json.dumps(links, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _upsert_form_sheet_link(form_id: str, details: dict[str, Any]) -> None:
    """Insert or update a form-to-sheet link entry."""
    links = _load_form_sheet_links()
    existing = links.get(form_id, {})
    existing.update(details)
    links[form_id] = existing
    _save_form_sheet_links(links)


def _sanitize_pg_connection_string(conn: str) -> str:
    return (
        conn.strip()
        .replace("postgresql+psycopg", "postgresql")
        .replace("postgresql+asyncpg", "postgresql")
        .replace("postgresql+pg8000", "postgresql")
    )


def _get_form_response_pg_connection_string() -> str:
    configured = os.getenv("FORM_RESPONSE_PG_CONN_STR", "").strip()
    if configured:
        return _sanitize_pg_connection_string(configured)
    fallback = os.getenv("PG_CONN_STR", "").strip()
    if fallback:
        return _sanitize_pg_connection_string(fallback)
    return ""


def _initialize_form_response_pg_schema(connection: Any) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_forms (
                form_id TEXT PRIMARY KEY,
                spreadsheet_id TEXT,
                form_title TEXT,
                form_url TEXT,
                responder_url TEXT,
                spreadsheet_url TEXT,
                google_oauth_session_key TEXT,
                created_at TIMESTAMPTZ NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL
            )
            """
        )
        cursor.execute(
            """
            ALTER TABLE agent_forms
            ADD COLUMN IF NOT EXISTS google_oauth_session_key TEXT
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS form_responses (
                form_id TEXT NOT NULL,
                response_id TEXT NOT NULL,
                created_time TIMESTAMPTZ,
                last_submitted_time TIMESTAMPTZ,
                respondent_email TEXT,
                response_json JSONB NOT NULL,
                synced_at TIMESTAMPTZ NOT NULL,
                PRIMARY KEY (form_id, response_id),
                FOREIGN KEY (form_id) REFERENCES agent_forms(form_id) ON DELETE CASCADE
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS form_response_answers (
                form_id TEXT NOT NULL,
                response_id TEXT NOT NULL,
                question_id TEXT NOT NULL,
                question_title TEXT NOT NULL,
                answer_text TEXT,
                PRIMARY KEY (form_id, response_id, question_id),
                FOREIGN KEY (form_id, response_id)
                    REFERENCES form_responses(form_id, response_id)
                    ON DELETE CASCADE
            )
            """
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_agent_forms_spreadsheet_id ON agent_forms(spreadsheet_id)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_form_response_answers_question_title "
            "ON form_response_answers(form_id, question_title)"
        )


def _open_form_response_db() -> Any:
    global FORM_RESPONSE_SCHEMA_INITIALIZED
    conn_str = _get_form_response_pg_connection_string()
    if not conn_str:
        raise RuntimeError(
            "Postgres response store is not configured. Set FORM_RESPONSE_PG_CONN_STR or PG_CONN_STR."
        )
    connection = pg_connect(conn_str)
    if not FORM_RESPONSE_SCHEMA_INITIALIZED:
        with FORM_RESPONSE_SCHEMA_LOCK:
            if not FORM_RESPONSE_SCHEMA_INITIALIZED:
                _initialize_form_response_pg_schema(connection)
                FORM_RESPONSE_SCHEMA_INITIALIZED = True
    return connection


def _describe_form_response_store() -> str:
    return "postgres"


def _serialize_pg_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, bytes):
        return base64.b64encode(value).decode("ascii")
    return value


def _response_store_schema_payload() -> dict[str, Any]:
    return {
        "database": _describe_form_response_store(),
        "tables": [
            {
                "name": "agent_forms",
                "description": "Forms created and managed by the agent.",
                "columns": [
                    "form_id",
                    "spreadsheet_id",
                    "form_title",
                    "form_url",
                    "responder_url",
                    "spreadsheet_url",
                    "google_oauth_session_key",
                    "created_at",
                    "updated_at",
                ],
            },
            {
                "name": "form_responses",
                "description": "One row per Google Form response synced from the Forms API.",
                "columns": [
                    "form_id",
                    "response_id",
                    "created_time",
                    "last_submitted_time",
                    "respondent_email",
                    "response_json",
                    "synced_at",
                ],
            },
            {
                "name": "form_response_answers",
                "description": "Flattened answer rows per response and question.",
                "columns": [
                    "form_id",
                    "response_id",
                    "question_id",
                    "question_title",
                    "answer_text",
                ],
            },
        ],
        "notes": [
            "Use agent_forms to find forms and linked spreadsheets.",
            "Use form_responses for response-level timestamps and raw response_json.",
            "Use form_response_answers for question/answer level analysis.",
            "respondent_email is only populated when the synced response payload contains it.",
            "Google Forms quiz grading data, when present, stays inside form_responses.response_json.",
            "If response_json.totalScore exists, prefer it as the response-level score source.",
            "response_json.answers is a JSON object keyed by question id, not a JSON array.",
            "To iterate answers from response_json, prefer jsonb_each(response_json->'answers') and treat the value as the answer object.",
            "For score questions, prefer extracting numeric grade values from response_json.answers[question_id].grade.score instead of casting form_response_answers.answer_text.",
            "form_response_answers.answer_text can contain names, free text, labels, or option text and is not guaranteed to be numeric.",
        ],
    }


_FORBIDDEN_SQL_TOKENS = (
    "insert",
    "update",
    "delete",
    "alter",
    "drop",
    "create",
    "truncate",
    "grant",
    "revoke",
    "copy",
    "comment",
    "vacuum",
    "reindex",
    "refresh",
    "analyze",
    "merge",
    "call",
    "execute",
    "prepare",
    "deallocate",
    "begin",
    "commit",
    "rollback",
    "savepoint",
    "lock",
    "set ",
    "reset ",
    "show ",
    "listen",
    "notify",
    "unlisten",
    "pg_sleep",
)


def _validate_readonly_response_store_sql(sql: str) -> str:
    normalized = str(sql or "").strip()
    if not normalized:
        raise RuntimeError("sql is required.")

    stripped = normalized.rstrip().rstrip(";").strip()
    lowered = stripped.casefold()
    if not (lowered.startswith("select ") or lowered.startswith("with ")):
        raise RuntimeError("Only read-only SELECT or WITH queries are allowed.")

    if ";" in stripped:
        raise RuntimeError("Only a single SQL statement is allowed.")

    for token in _FORBIDDEN_SQL_TOKENS:
        escaped = re.escape(token.strip())
        if " " in token or token.endswith(" "):
            pattern = rf"(?<![a-z0-9_]){escaped}(?![a-z0-9_])"
        else:
            pattern = rf"\b{escaped}\b"
        if re.search(pattern, lowered):
            raise RuntimeError(f"Forbidden SQL token detected: {token.strip()}")

    return stripped


def _extract_embedded_readonly_sql(text: str) -> str:
    """Extract a single read-only SQL statement from free-form user text when present."""
    raw_text = str(text or "").strip()
    if not raw_text:
        return ""

    fenced_match = re.search(
        r"```(?:sql)?\s*(select\b.*?|with\b.*?)```",
        raw_text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if fenced_match:
        candidate = fenced_match.group(1).strip()
        try:
            return _validate_readonly_response_store_sql(candidate)
        except Exception:
            return ""

    inline_match = re.search(
        r"(?is)\b(select\b.*|with\b.*)$",
        raw_text,
    )
    if inline_match:
        candidate = inline_match.group(1).strip()
        try:
            return _validate_readonly_response_store_sql(candidate)
        except Exception:
            return ""
    return ""


def _format_query_payload_as_markdown_table(payload: dict[str, Any]) -> str:
    """Render a readonly SQL payload as a markdown table when possible."""
    columns = [
        str(column).strip()
        for column in payload.get("columns", [])
        if str(column).strip()
    ]
    rows = payload.get("rows", [])
    if not columns or not rows:
        return ""

    table_rows: list[list[Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        table_rows.append([row.get(column) for column in columns])
    if not table_rows:
        return ""
    return _format_markdown_table(columns, table_rows)


def _looks_like_form_response_query_request(text: str) -> bool:
    """Return whether the user is asking to inspect response rows for a specific form."""
    lowered = str(text or "").casefold()
    if not lowered:
        return False
    exact_markers = (
        "query",
        "show data",
        "show response",
        "show responses",
        "show answer",
        "show answers",
        "list response",
        "list responses",
        "list answer",
        "list answers",
        "response data",
        "answer data",
        "raw data",
        "ดูข้อมูล",
        "แสดงข้อมูล",
        "ดึงข้อมูล",
        "ดูคำตอบ",
        "แสดงคำตอบ",
        "ข้อมูลคำตอบ",
        "คำตอบของฟอร์ม",
    )
    if any(marker in lowered for marker in exact_markers):
        return True

    action_markers = (
        "show",
        "list",
        "query",
        "fetch",
        "get",
        "inspect",
        "ดู",
        "แสดง",
        "ดึง",
        "ขอ",
    )
    data_markers = (
        "response",
        "responses",
        "answer",
        "answers",
        "row",
        "rows",
        "record",
        "records",
        "data",
        "raw",
        "result",
        "results",
        "คำตอบ",
        "ข้อมูล",
        "รายการ",
        "แถว",
        "ระเบียน",
        "ผลลัพธ์",
    )
    return any(marker in lowered for marker in action_markers) and any(
        marker in lowered for marker in data_markers
    )


def _plan_form_scoped_database_query(text: str) -> dict[str, Any] | None:
    """Choose a deterministic local SQL query pattern for a target form when trivial."""
    if _looks_like_form_response_query_request(text):
        return {
            "kind": "response-rows",
        }
    if _looks_like_score_ranking_request(text) and _looks_like_respondent_identity_request(text):
        return {
            "kind": "top-scorer",
        }
    return None


def _looks_like_score_ranking_request(text: str) -> bool:
    """Return whether the user is asking about scores, top/bottom ranks, or scorers."""
    lowered = str(text or "").casefold()
    if not lowered:
        return False
    score_markers = (
        "score",
        "scores",
        "highest",
        "lowest",
        "top scorer",
        "best score",
        "worst score",
        "คะแนน",
        "คะแนนมากที่สุด",
        "คะแนนน้อยที่สุด",
        "ได้คะแนน",
        "คะแนนสูงสุด",
        "คะแนนต่ำสุด",
    )
    ranking_markers = (
        "top",
        "best",
        "worst",
        "most",
        "least",
        "มากที่สุด",
        "น้อยที่สุด",
        "สูงสุด",
        "ต่ำสุด",
        "อันดับ",
    )
    return any(marker in lowered for marker in score_markers) or (
        any(marker in lowered for marker in ranking_markers)
        and any(marker in lowered for marker in ("score", "scores", "คะแนน"))
    )


def _looks_like_respondent_identity_request(text: str) -> bool:
    """Return whether the user is asking for who/respondent/person identity, not just score rows."""
    lowered = str(text or "").casefold()
    if not lowered:
        return False
    identity_markers = (
        "who",
        "person",
        "respondent",
        "name",
        "ชื่อ",
        "คน",
        "ผู้ตอบ",
        "ใคร",
    )
    return any(marker in lowered for marker in identity_markers)


def _payload_is_effectively_blank(payload: dict[str, Any]) -> bool:
    """Return whether a SQL payload has rows but no meaningful scalar values."""
    rows = payload.get("rows", [])
    if not isinstance(rows, list) or not rows:
        return False
    meaningful = False
    for row in rows:
        if not isinstance(row, dict):
            continue
        for value in row.values():
            if value is None:
                continue
            text = str(value).strip()
            if text and text != "-":
                meaningful = True
                break
        if meaningful:
            break
    return not meaningful


def _payload_has_low_quality_identity(payload: dict[str, Any]) -> bool:
    """Return whether the leading identity-like value is too weak to trust as a person label."""
    rows = payload.get("rows", [])
    columns = [str(column).strip().casefold() for column in payload.get("columns", [])]
    if not isinstance(rows, list) or not rows or not columns:
        return False

    identity_keys = [
        column
        for column in columns
        if column in {"respondent", "name", "respondent_email"} or "name" in column
    ]
    if not identity_keys:
        return False

    first_row = rows[0]
    if not isinstance(first_row, dict):
        return False

    value = None
    for original_key, normalized_key in zip(payload.get("columns", []), columns, strict=False):
        if normalized_key in identity_keys:
            value = first_row.get(original_key)
            break
    if value is None:
        return True

    text = str(value).strip()
    if not text:
        return True
    if re.fullmatch(r"[A-Za-z]", text):
        return True
    if re.fullmatch(r"[-_?]+", text):
        return True
    return False


def _payload_has_missing_score(payload: dict[str, Any]) -> bool:
    """Return whether a score-ranking payload is missing a usable score value."""
    rows = payload.get("rows", [])
    columns = [str(column).strip().casefold() for column in payload.get("columns", [])]
    if not isinstance(rows, list) or not rows or not columns:
        return False

    score_keys = [
        column
        for column in columns
        if column in {"total_score", "score"} or "score" in column or "คะแนน" in column
    ]
    if not score_keys:
        return True

    first_row = rows[0]
    if not isinstance(first_row, dict):
        return True

    value = None
    for original_key, normalized_key in zip(payload.get("columns", []), columns, strict=False):
        if normalized_key in score_keys:
            value = first_row.get(original_key)
            break
    if value is None:
        return True

    text = str(value).strip()
    if not text or text == "-":
        return True
    return False


def _rename_identity_column(payload: dict[str, Any], new_name: str) -> dict[str, Any]:
    """Rename respondent-like output columns for cleaner chat rendering."""
    columns = payload.get("columns", [])
    rows = payload.get("rows", [])
    if not isinstance(columns, list) or not isinstance(rows, list):
        return payload

    renamed_columns = list(columns)
    source_column = None
    for index, column in enumerate(columns):
        normalized = str(column).strip().casefold()
        if normalized in {"respondent", "respondent_email"}:
            renamed_columns[index] = new_name
            source_column = column
            break
    if source_column is None:
        return payload

    renamed_rows: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            renamed_rows.append(row)
            continue
        updated = dict(row)
        updated[new_name] = updated.pop(source_column, None)
        renamed_rows.append(updated)

    updated_payload = dict(payload)
    updated_payload["columns"] = renamed_columns
    updated_payload["rows"] = renamed_rows
    return updated_payload


def _normalize_identity_title(text: str) -> str:
    """Normalize a question title for identity-field matching."""
    normalized = str(text or "").casefold()
    normalized = re.sub(r"[\s_\-/\\|():]+", "", normalized)
    return normalized


def _find_likely_identity_question_titles(form_id: str) -> list[str]:
    """Return likely respondent-identity question titles for a form, ordered by usefulness."""
    payload = _execute_readonly_response_store_query(
        (
            "SELECT question_title, COUNT(*) AS usage_count "
            "FROM form_response_answers "
            f"WHERE form_id = {_quote_sql_string_literal(form_id)} "
            "GROUP BY question_title "
            "ORDER BY usage_count DESC, question_title ASC"
        ),
        row_limit=500,
    )
    rows = payload.get("rows", [])
    scored: list[tuple[int, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        title = str(row.get("question_title", "") or "").strip()
        if not title:
            continue
        lowered = title.casefold()
        normalized_title = _normalize_identity_title(title)
        score = 0
        if "ชื่อนามสกุล" in normalized_title or "fullname" in normalized_title:
            score += 100
        if normalized_title == "ชื่อ" or "firstname" in normalized_title or "lastname" in normalized_title:
            score += 70
        elif "ชื่อ" in title:
            score += 60
        if "email" in lowered or "อีเมล" in title:
            score += 40
        if "หน่วยงาน" in title or "organization" in lowered or "department" in lowered:
            score += 15
        if "phone" in lowered or "โทร" in title:
            score -= 20
        if "score" in lowered or "คะแนน" in title:
            score -= 20
        if score > 0:
            scored.append((score, title))
    scored.sort(key=lambda item: (-item[0], item[1].casefold()))
    if not scored:
        return []

    highest_score = scored[0][0]
    ordered: list[str] = []
    seen: set[str] = set()
    for _score, title in scored:
        if _score < highest_score:
            break
        key = title.casefold()
        if key in seen:
            continue
        seen.add(key)
        ordered.append(title)
    return ordered


def _load_form_structure_context(form_id: str) -> dict[str, Any]:
    """Inspect stored structure for one form so NL-to-SQL can use actual question ids and titles."""
    normalized_form_id = str(form_id or "").strip()
    if not normalized_form_id:
        return {}

    title_payload = _execute_readonly_response_store_query(
        (
            "SELECT question_id, question_title, COUNT(*) AS usage_count "
            "FROM form_response_answers "
            f"WHERE form_id = {_quote_sql_string_literal(normalized_form_id)} "
            "GROUP BY question_id, question_title "
            "ORDER BY usage_count DESC, question_title ASC"
        ),
        row_limit=500,
    )
    sample_payload = _execute_readonly_response_store_query(
        (
            "SELECT response_json "
            "FROM form_responses "
            f"WHERE form_id = {_quote_sql_string_literal(normalized_form_id)} "
            "AND response_json IS NOT NULL "
            "ORDER BY created_time DESC NULLS LAST, response_id DESC "
            "LIMIT 1"
        ),
        row_limit=1,
    )

    question_map: list[dict[str, Any]] = []
    for row in title_payload.get("rows", []):
        if not isinstance(row, dict):
            continue
        question_id = str(row.get("question_id", "") or "").strip()
        question_title = str(row.get("question_title", "") or "").strip()
        if not question_id or not question_title:
            continue
        question_map.append(
            {
                "question_id": question_id,
                "question_title": question_title,
                "usage_count": row.get("usage_count"),
            }
        )

    sample_response_json = None
    sample_answer_keys: list[str] = []
    sample_grade_keys: list[str] = []
    sample_text_answer_keys: list[str] = []
    sample_answer_shapes: list[dict[str, Any]] = []
    rows = sample_payload.get("rows", [])
    if rows and isinstance(rows[0], dict):
        raw_response_json = rows[0].get("response_json")
        if isinstance(raw_response_json, dict):
            sample_response_json = raw_response_json
            answers = raw_response_json.get("answers")
            if isinstance(answers, dict):
                sample_answer_keys = list(answers.keys())[:20]
                for question_id, answer_obj in list(answers.items())[:5]:
                    if not isinstance(answer_obj, dict):
                        continue
                    grade = answer_obj.get("grade")
                    text_answers = answer_obj.get("textAnswers")
                    if isinstance(grade, dict):
                        for key in grade.keys():
                            key_text = str(key).strip()
                            if key_text and key_text not in sample_grade_keys:
                                sample_grade_keys.append(key_text)
                    if isinstance(text_answers, dict):
                        for key in text_answers.keys():
                            key_text = str(key).strip()
                            if key_text and key_text not in sample_text_answer_keys:
                                sample_text_answer_keys.append(key_text)
                    sample_answer_shapes.append(
                        {
                            "question_id": str(question_id),
                            "keys": sorted(str(key) for key in answer_obj.keys()),
                            "grade_keys": sorted(str(key) for key in grade.keys()) if isinstance(grade, dict) else [],
                            "text_answers_keys": sorted(str(key) for key in text_answers.keys()) if isinstance(text_answers, dict) else [],
                        }
                    )

    return {
        "form_id": normalized_form_id,
        "question_map": question_map,
        "likely_identity_titles": _find_likely_identity_question_titles(normalized_form_id),
        "sample_answer_keys": sample_answer_keys,
        "sample_grade_keys": sample_grade_keys,
        "sample_text_answer_keys": sample_text_answer_keys,
        "sample_answer_shapes": sample_answer_shapes,
    }


def _build_top_scorer_fallback_sql(form_id: str, *, prefer_identity: bool) -> str:
    """Build deterministic SQL for top scorer lookup using stored response JSON grades."""
    identity_titles = _find_likely_identity_question_titles(form_id)
    identity_alias = "name" if prefer_identity else "respondent"
    identity_clause = "ranked.response_id"
    identity_join = ""
    identity_order_clause = "ranked.response_id ASC"
    if prefer_identity and identity_titles:
        quoted_titles = ", ".join(_quote_sql_string_literal(title) for title in identity_titles[:8])
        valid_identity_sql = (
            "NULLIF(BTRIM(fra.answer_text), '') IS NOT NULL "
            "AND LENGTH(BTRIM(fra.answer_text)) > 1 "
            "AND lower(BTRIM(fra.answer_text)) NOT IN ('a', 'test', 'na', 'n/a', 'none', 'null', '-', '?') "
            "AND BTRIM(fra.answer_text) !~ '^[A-Za-z]$'"
        )
        identity_join = (
            " LEFT JOIN ("
            "   SELECT DISTINCT ON (fra.response_id) fra.response_id, fra.answer_text AS fallback_name "
            "   FROM form_response_answers fra "
            f"   WHERE fra.form_id = {_quote_sql_string_literal(form_id)} "
            f"     AND fra.question_title IN ({quoted_titles}) "
            f"     AND {valid_identity_sql} "
            "   ORDER BY fra.response_id, CASE "
            "     WHEN regexp_replace(lower(fra.question_title), '[[:space:]_\\-/\\\\|():]+', '', 'g') IN ('ชื่อนามสกุล', 'fullname') THEN 0 "
            "     WHEN regexp_replace(lower(fra.question_title), '[[:space:]_\\-/\\\\|():]+', '', 'g') IN ('ชื่อ', 'firstname', 'lastname') THEN 1 "
            "     WHEN lower(fra.question_title) LIKE '%email%' OR fra.question_title LIKE '%อีเมล%' THEN 2 "
            "     ELSE 3 "
            "   END, fra.question_title ASC"
            " ) ident ON ident.response_id = ranked.response_id"
        )
        identity_clause = "COALESCE(ident.fallback_name, NULLIF(ranked.respondent_email, ''), ranked.response_id)"
        identity_order_clause = (
            "CASE WHEN ident.fallback_name IS NULL AND NULLIF(ranked.respondent_email, '') IS NULL THEN 1 ELSE 0 END, "
            "COALESCE(ident.fallback_name, NULLIF(ranked.respondent_email, ''), ranked.response_id) ASC"
        )

    return (
        "WITH scores AS ("
        " SELECT fr.response_id, "
        "        fr.respondent_email, "
        "        COALESCE("
        "          NULLIF(fr.response_json->>'totalScore', '')::numeric, "
        "          ("
        "            SELECT SUM((answer.answer_obj->'grade'->>'score')::numeric) "
            "            FROM jsonb_each(fr.response_json->'answers') AS answer(question_id, answer_obj) "
        "            WHERE answer.answer_obj->'grade'->>'score' IS NOT NULL"
        "          )"
        "        ) AS total_score "
        " FROM form_responses fr "
        f" WHERE fr.form_id = {_quote_sql_string_literal(form_id)} "
        "), ranked AS ("
        " SELECT response_id, respondent_email, total_score "
        " FROM scores "
        " WHERE total_score IS NOT NULL "
        " ORDER BY total_score DESC, response_id ASC "
        " LIMIT 50"
        ") "
        f"SELECT {identity_clause} AS {identity_alias}, ranked.total_score "
        "FROM ranked "
        f"{identity_join} "
        f"ORDER BY ranked.total_score DESC, {identity_order_clause} "
        "LIMIT 1"
    )


def _generate_readonly_response_store_sql_from_nl(
    request_text: str,
    *,
    target_form_id: str = "",
    user_language: str = "en",
) -> str:
    """Use the configured chat model to translate a natural-language DB request into read-only SQL."""
    normalized_request = str(request_text or "").strip()
    if not normalized_request:
        raise RuntimeError("request_text is required")

    schema_payload = _response_store_schema_payload()
    form_context_payload = (
        _load_form_structure_context(target_form_id.strip())
        if target_form_id.strip()
        else {}
    )
    guidance_lines = [
        "You translate a natural-language request into one PostgreSQL read-only query.",
        "Return only SQL.",
        "Use only SELECT or WITH.",
        "Never write INSERT, UPDATE, DELETE, ALTER, DROP, CREATE, TRUNCATE, COPY, CALL, EXECUTE, or transaction control.",
        "Use only the tables and columns listed in the schema.",
        "Prefer concise queries that answer the user's question directly.",
        "If the request clearly targets one form and a target form id is provided, use it in the WHERE clause.",
        "Do not include markdown fences, commentary, labels, or explanations.",
        "Do not cast form_response_answers.answer_text to numeric unless the SQL first proves the values are numeric.",
        "If the request is about quiz scores, totals, highest score, lowest score, top scorer, or score ranking, prefer form_responses.response_json and extract score values from response_json.answers JSON.",
        "response_json.answers is an object keyed by question id, not an array. Do not use jsonb_array_elements on response_json.answers.",
        "When iterating response_json.answers, prefer jsonb_each(response_json->'answers') AS answer(question_id, answer_obj).",
        "When aggregating quiz scores, sum numeric values from answer_obj->'grade'->>'score' per response.",
        "If form-specific structure context is provided, use the actual question titles and question ids from that context instead of guessing labels like Name or Email.",
    ]
    if user_language == "th":
        guidance_lines.append(
            "คำขอของผู้ใช้อาจเป็นภาษาไทย แต่ให้ตอบกลับเป็น SQL อย่างเดียว"
        )

    prompt_parts = [
        f"Schema:\n{json.dumps(schema_payload, ensure_ascii=False, indent=2)}",
    ]
    if target_form_id.strip():
        prompt_parts.append(f"Preferred target form_id: {target_form_id.strip()}")
    if form_context_payload:
        prompt_parts.append(
            "Form structure context:\n"
            f"{json.dumps(form_context_payload, ensure_ascii=False, indent=2)}"
        )
    prompt_parts.append(f"User request:\n{normalized_request}")

    model = build_chat_model()
    response = model.invoke(
        [
            SystemMessage(content="\n".join(guidance_lines)),
            HumanMessage(content="\n\n".join(prompt_parts)),
        ]
    )
    sql = content_to_text(getattr(response, "content", response)).strip()
    sql = re.sub(r"^```(?:sql)?\s*|\s*```$", "", sql, flags=re.IGNORECASE | re.DOTALL).strip()
    return _validate_readonly_response_store_sql(sql)


def _generate_readonly_response_store_sql_retry_on_error(
    request_text: str,
    *,
    target_form_id: str = "",
    user_language: str = "en",
    failed_sql: str,
    error_text: str,
) -> str:
    """Regenerate SQL after a database execution error using the failure details as constraints."""
    normalized_request = str(request_text or "").strip()
    schema_payload = _response_store_schema_payload()
    form_context_payload = (
        _load_form_structure_context(target_form_id.strip())
        if target_form_id.strip()
        else {}
    )
    prompt_parts = [
        f"Schema:\n{json.dumps(schema_payload, ensure_ascii=False, indent=2)}",
    ]
    if target_form_id.strip():
        prompt_parts.append(f"Preferred target form_id: {target_form_id.strip()}")
    if form_context_payload:
        prompt_parts.append(
            "Form structure context:\n"
            f"{json.dumps(form_context_payload, ensure_ascii=False, indent=2)}"
        )
    prompt_parts.extend(
        [
            f"User request:\n{normalized_request}",
            f"Previous SQL that failed:\n{failed_sql}",
            f"Database error:\n{error_text}",
        ]
    )

    guidance_lines = [
        "The previous PostgreSQL query failed.",
        "Generate one corrected read-only PostgreSQL query.",
        "Return only SQL.",
        "Use only SELECT or WITH.",
        "Do not repeat the same mistake.",
        "If the error shows invalid numeric casting, avoid casting free-text answer columns and prefer numeric grade data from response_json.answers[*].grade.score.",
        "If the error says it cannot extract elements from an object, treat response_json.answers as a JSON object and iterate it with jsonb_each, not jsonb_array_elements.",
        "Do not include markdown fences, commentary, labels, or explanations.",
    ]
    if user_language == "th":
        guidance_lines.append("คำขอของผู้ใช้อาจเป็นภาษาไทย แต่ให้ตอบกลับเป็น SQL อย่างเดียว")

    model = build_chat_model()
    response = model.invoke(
        [
            SystemMessage(content="\n".join(guidance_lines)),
            HumanMessage(content="\n\n".join(prompt_parts)),
        ]
    )
    sql = content_to_text(getattr(response, "content", response)).strip()
    sql = re.sub(r"^```(?:sql)?\s*|\s*```$", "", sql, flags=re.IGNORECASE | re.DOTALL).strip()
    return _validate_readonly_response_store_sql(sql)


def _generate_readonly_response_store_sql_retry_on_blank_result(
    request_text: str,
    *,
    target_form_id: str = "",
    user_language: str = "en",
    failed_sql: str,
) -> str:
    """Regenerate SQL when the first result shape is syntactically valid but semantically blank."""
    normalized_request = str(request_text or "").strip()
    schema_payload = _response_store_schema_payload()
    form_context_payload = (
        _load_form_structure_context(target_form_id.strip())
        if target_form_id.strip()
        else {}
    )
    prompt_parts = [
        f"Schema:\n{json.dumps(schema_payload, ensure_ascii=False, indent=2)}",
    ]
    if target_form_id.strip():
        prompt_parts.append(f"Preferred target form_id: {target_form_id.strip()}")
    if form_context_payload:
        prompt_parts.append(
            "Form structure context:\n"
            f"{json.dumps(form_context_payload, ensure_ascii=False, indent=2)}"
        )
    prompt_parts.extend(
        [
            f"User request:\n{normalized_request}",
            f"Previous SQL that returned blank/null results:\n{failed_sql}",
            "The previous query returned rows but the important answer fields were blank or null.",
        ]
    )

    guidance_lines = [
        "Generate one corrected read-only PostgreSQL query.",
        "Return only SQL.",
        "Use only SELECT or WITH.",
        "For score-ranking questions, exclude rows where score is null.",
        "If the user is asking for a person or respondent, return a non-empty respondent identifier.",
        "Prefer COALESCE(NULLIF(fr.respondent_email, ''), fallback_name, fr.response_id) for respondent identity when email may be empty.",
        "A fallback_name can come from form_response_answers for likely identity questions such as titles containing name, email, ชื่อ, อีเมล, or หน่วยงาน when available.",
        "Do not include markdown fences, commentary, labels, or explanations.",
    ]
    if user_language == "th":
        guidance_lines.append("คำขอของผู้ใช้อาจเป็นภาษาไทย แต่ให้ตอบกลับเป็น SQL อย่างเดียว")

    model = build_chat_model()
    response = model.invoke(
        [
            SystemMessage(content="\n".join(guidance_lines)),
            HumanMessage(content="\n\n".join(prompt_parts)),
        ]
    )
    sql = content_to_text(getattr(response, "content", response)).strip()
    sql = re.sub(r"^```(?:sql)?\s*|\s*```$", "", sql, flags=re.IGNORECASE | re.DOTALL).strip()
    return _validate_readonly_response_store_sql(sql)


def _execute_readonly_response_store_query(sql: str, row_limit: int = 200) -> dict[str, Any]:
    normalized_sql = _validate_readonly_response_store_sql(sql)
    normalized_limit = max(1, min(int(row_limit), 500))

    with _open_form_response_db() as connection:
        with connection.cursor() as cursor:
            cursor.execute("SET TRANSACTION READ ONLY")
            cursor.execute("SET LOCAL statement_timeout = 5000")
            cursor.execute(normalized_sql)
            rows = cursor.fetchmany(normalized_limit + 1)
            columns = [desc.name for desc in (cursor.description or [])]

    truncated = len(rows) > normalized_limit
    if truncated:
        rows = rows[:normalized_limit]

    serialized_rows = [
        {
            column: _serialize_pg_value(value)
            for column, value in zip(columns, row, strict=False)
        }
        for row in rows
    ]

    return {
        "database": _describe_form_response_store(),
        "sql": normalized_sql,
        "rowCount": len(serialized_rows),
        "truncated": truncated,
        "rowLimit": normalized_limit,
        "columns": columns,
        "rows": serialized_rows,
    }


def _run_nl_to_sql_response_store_query(
    request_text: str,
    *,
    target_form_id: str = "",
    user_language: str = "en",
    row_limit: int = 200,
) -> tuple[str, dict[str, Any]]:
    """Generate and execute read-only SQL, retrying once with error-aware guidance when useful."""
    normalized_target_form_id = str(target_form_id or "").strip()
    score_ranking_request = _looks_like_score_ranking_request(request_text)
    respondent_identity_request = _looks_like_respondent_identity_request(request_text)
    sql = _generate_readonly_response_store_sql_from_nl(
        request_text,
        target_form_id=normalized_target_form_id,
        user_language=user_language,
    )
    try:
        payload = _execute_readonly_response_store_query(sql, row_limit=row_limit)
        if score_ranking_request:
            if _payload_is_effectively_blank(payload) or _payload_has_missing_score(payload):
                retry_sql = _generate_readonly_response_store_sql_retry_on_blank_result(
                    request_text,
                    target_form_id=normalized_target_form_id,
                    user_language=user_language,
                    failed_sql=sql,
                )
                retry_payload = _execute_readonly_response_store_query(retry_sql, row_limit=row_limit)
                if not _payload_is_effectively_blank(retry_payload) and not _payload_has_missing_score(retry_payload):
                    return retry_sql, retry_payload
                if normalized_target_form_id:
                    fallback_sql = _build_top_scorer_fallback_sql(
                        normalized_target_form_id,
                        prefer_identity=respondent_identity_request,
                    )
                    fallback_payload = _execute_readonly_response_store_query(
                        fallback_sql,
                        row_limit=row_limit,
                    )
                    if respondent_identity_request:
                        fallback_payload = _rename_identity_column(fallback_payload, "name")
                    return fallback_sql, fallback_payload
            elif (
                normalized_target_form_id
                and (
                    (
                        respondent_identity_request
                        and all(
                            "respondent" not in str(column).casefold()
                            and "email" not in str(column).casefold()
                            and "name" not in str(column).casefold()
                            for column in payload.get("columns", [])
                        )
                    )
                    or _payload_has_low_quality_identity(payload)
                    or _payload_has_missing_score(payload)
                )
            ):
                fallback_sql = _build_top_scorer_fallback_sql(
                    normalized_target_form_id,
                    prefer_identity=True,
                )
                fallback_payload = _execute_readonly_response_store_query(
                    fallback_sql,
                    row_limit=row_limit,
                )
                fallback_payload = _rename_identity_column(fallback_payload, "name")
                return fallback_sql, fallback_payload
        if respondent_identity_request:
            payload = _rename_identity_column(payload, "name")
        return sql, payload
    except Exception as exc:
        error_text = str(exc)
        likely_cast_error = any(
            marker in error_text.casefold()
            for marker in (
                "invalid input syntax for type integer",
                "invalid input syntax for type numeric",
                "cannot cast",
                "operator does not exist",
                "cannot extract elements from an object",
            )
        )
        if not likely_cast_error:
            raise
        retry_sql = _generate_readonly_response_store_sql_retry_on_error(
            request_text,
            target_form_id=normalized_target_form_id,
            user_language=user_language,
            failed_sql=sql,
            error_text=error_text,
        )
        try:
            payload = _execute_readonly_response_store_query(retry_sql, row_limit=row_limit)
            if (
                score_ranking_request
                and normalized_target_form_id
                and (_payload_is_effectively_blank(payload) or _payload_has_missing_score(payload))
            ):
                fallback_sql = _build_top_scorer_fallback_sql(
                    normalized_target_form_id,
                    prefer_identity=respondent_identity_request,
                )
                fallback_payload = _execute_readonly_response_store_query(
                    fallback_sql,
                    row_limit=row_limit,
                )
                if respondent_identity_request:
                    fallback_payload = _rename_identity_column(fallback_payload, "name")
                return fallback_sql, fallback_payload
            return retry_sql, payload
        except Exception:
            if score_ranking_request and normalized_target_form_id:
                fallback_sql = _build_top_scorer_fallback_sql(
                    normalized_target_form_id,
                    prefer_identity=respondent_identity_request,
                )
                fallback_payload = _execute_readonly_response_store_query(
                    fallback_sql,
                    row_limit=row_limit,
                )
                if respondent_identity_request:
                    fallback_payload = _rename_identity_column(fallback_payload, "name")
                return fallback_sql, fallback_payload
            raise


def _upsert_agent_form_record(
    form_id: str,
    *,
    spreadsheet_id: str = "",
    form_title: str = "",
    form_url: str = "",
    responder_url: str = "",
    spreadsheet_url: str = "",
    google_oauth_session_key: str = "",
) -> None:
    if not form_id.strip():
        return

    now = datetime.now(timezone.utc).isoformat()
    with _open_form_response_db() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO agent_forms (
                    form_id, spreadsheet_id, form_title, form_url, responder_url, spreadsheet_url,
                    google_oauth_session_key, created_at, updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT(form_id) DO UPDATE SET
                    spreadsheet_id = excluded.spreadsheet_id,
                    form_title = excluded.form_title,
                    form_url = excluded.form_url,
                    responder_url = excluded.responder_url,
                    spreadsheet_url = excluded.spreadsheet_url,
                    google_oauth_session_key = excluded.google_oauth_session_key,
                    updated_at = excluded.updated_at
                """,
                (
                    form_id.strip(),
                    spreadsheet_id.strip(),
                    form_title.strip(),
                    form_url.strip(),
                    responder_url.strip(),
                    spreadsheet_url.strip(),
                    _sanitize_google_oauth_session_key(google_oauth_session_key) or "",
                    now,
                    now,
                ),
            )


def _find_agent_form_by_spreadsheet_id(spreadsheet_id: str) -> tuple[str, dict[str, Any]]:
    normalized = spreadsheet_id.strip()
    if not normalized:
        return "", {}

    for form_id, details in _load_form_sheet_links().items():
        if str(details.get("spreadsheetId", "") or "").strip() == normalized:
            payload = dict(details)
            payload["formId"] = form_id
            return form_id, payload
    return "", {}


def _resolve_agent_form_session_key(
    form_id: str,
    details: dict[str, Any],
) -> str | None:
    candidates = [
        details.get("googleOauthSessionKey"),
        details.get("google_oauth_session_key"),
    ]
    for candidate in candidates:
        normalized = _sanitize_google_oauth_session_key(candidate)
        if normalized:
            return normalized

    try:
        with _open_form_response_db() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT google_oauth_session_key FROM agent_forms WHERE form_id = %s",
                    (form_id,),
                )
                row = cursor.fetchone()
    except Exception:
        row = None

    if row and len(row) >= 1:
        normalized = _sanitize_google_oauth_session_key(row[0])
        if normalized:
            return normalized
    return _sanitize_google_oauth_session_key(GOOGLE_OAUTH_SESSION_KEY.get())


def _best_effort_sync_agent_form_responses(
    form_id: str,
    *,
    spreadsheet_id: str = "",
    form_title: str = "",
    form_url: str = "",
    responder_url: str = "",
    spreadsheet_url: str = "",
    google_oauth_session_key: str = "",
) -> dict[str, Any]:
    try:
        return _sync_agent_form_responses_to_sql(
            form_id,
            spreadsheet_id=spreadsheet_id,
            form_title=form_title,
            form_url=form_url,
            responder_url=responder_url,
            spreadsheet_url=spreadsheet_url,
            google_oauth_session_key=google_oauth_session_key,
        )
    except Exception as exc:
        return {
            "ok": False,
            "status": "sync-failed",
            "error": str(exc),
            "formId": form_id.strip(),
            "store": _describe_form_response_store(),
        }


def _best_effort_sync_agent_form_responses_by_spreadsheet(spreadsheet_id: str) -> dict[str, Any]:
    form_id, details = _find_agent_form_by_spreadsheet_id(spreadsheet_id)
    if not form_id:
        return {"ok": False, "status": "not-agent-managed", "error": "No agent-managed form matched the spreadsheet."}

    session_key = _resolve_agent_form_session_key(form_id, details)
    token_session = GOOGLE_OAUTH_SESSION_KEY.set(session_key)
    try:
        result = _best_effort_sync_agent_form_responses(
            form_id,
            spreadsheet_id=str(details.get("spreadsheetId", "") or ""),
            form_title=str(details.get("spreadsheetTitle", "") or ""),
            form_url=str(details.get("formUrl", "") or ""),
            responder_url=str(details.get("responseUrl", "") or ""),
            spreadsheet_url=str(details.get("spreadsheetUrl", "") or ""),
            google_oauth_session_key=session_key or "",
        )
        if session_key:
            _upsert_form_sheet_link(
                form_id,
                {
                    "googleOauthSessionKey": session_key,
                },
            )
        return result
    finally:
        GOOGLE_OAUTH_SESSION_KEY.reset(token_session)


def _iter_agent_managed_form_records() -> list[tuple[str, dict[str, Any]]]:
    return [
        (form_id, details)
        for form_id, details in _load_form_sheet_links().items()
        if isinstance(form_id, str) and form_id.strip() and isinstance(details, dict)
    ]


def _run_form_response_sync_cycle() -> None:
    for form_id, details in _iter_agent_managed_form_records():
        session_key = _resolve_agent_form_session_key(form_id, details)
        token_session = GOOGLE_OAUTH_SESSION_KEY.set(session_key)
        try:
            _best_effort_sync_agent_form_responses(
                form_id,
                spreadsheet_id=str(details.get("spreadsheetId", "") or ""),
                form_title=str(details.get("spreadsheetTitle", "") or ""),
                form_url=str(details.get("formUrl", "") or ""),
                responder_url=str(details.get("responseUrl", "") or ""),
                spreadsheet_url=str(details.get("spreadsheetUrl", "") or ""),
                google_oauth_session_key=session_key or "",
            )
        finally:
            GOOGLE_OAUTH_SESSION_KEY.reset(token_session)


def _form_response_sync_worker() -> None:
    interval_seconds = max(
        15,
        int(float(os.getenv("FORM_RESPONSE_SYNC_INTERVAL_SECONDS", "30") or "30")),
    )
    while True:
        try:
            _run_form_response_sync_cycle()
        except Exception:
            pass
        time.sleep(interval_seconds)


def _ensure_form_response_sync_worker_started() -> None:
    global FORM_RESPONSE_SYNC_WORKER_STARTED
    with FORM_RESPONSE_SYNC_WORKER_LOCK:
        if FORM_RESPONSE_SYNC_WORKER_STARTED:
            return
        worker = threading.Thread(
            target=_form_response_sync_worker,
            name="form-response-sync-worker",
            daemon=True,
        )
        worker.start()
        FORM_RESPONSE_SYNC_WORKER_STARTED = True


def _list_google_form_responses(forms_service: Any, form_id: str) -> list[dict[str, Any]]:
    responses: list[dict[str, Any]] = []
    page_token = ""
    while True:
        request = forms_service.forms().responses().list(
            formId=form_id,
            pageSize=5000,
            pageToken=page_token or None,
        )
        payload = request.execute()
        page_responses = payload.get("responses", []) or []
        responses.extend(
            response for response in page_responses if isinstance(response, dict)
        )
        page_token = str(payload.get("nextPageToken", "") or "").strip()
        if not page_token:
            break
    return responses


def _sync_agent_form_responses_to_sql(
    form_id: str,
    *,
    spreadsheet_id: str = "",
    form_title: str = "",
    form_url: str = "",
    responder_url: str = "",
    spreadsheet_url: str = "",
    google_oauth_session_key: str = "",
) -> dict[str, Any]:
    normalized_form_id = form_id.strip()
    if not normalized_form_id:
        return {"ok": False, "status": "missing-form-id", "error": "form_id is required"}

    forms_service = _build_forms_service()
    form_payload = forms_service.forms().get(formId=normalized_form_id).execute()
    resolved_form_title = str(form_payload.get("info", {}).get("title", "") or "").strip()
    resolved_form_url = form_url.strip() or f"https://docs.google.com/forms/d/{normalized_form_id}/edit"
    _upsert_agent_form_record(
        normalized_form_id,
        spreadsheet_id=spreadsheet_id,
        form_title=resolved_form_title or form_title,
        form_url=resolved_form_url,
        responder_url=responder_url,
        spreadsheet_url=spreadsheet_url,
        google_oauth_session_key=google_oauth_session_key,
    )
    responses = _list_google_form_responses(forms_service, normalized_form_id)
    question_map = _extract_form_question_map(form_payload)
    question_lookup = {
        question["questionId"]: question["title"]
        for question in question_map
        if isinstance(question.get("questionId"), str)
    }
    synced_at = datetime.now(timezone.utc).isoformat()

    with _open_form_response_db() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM form_response_answers WHERE form_id = %s",
                (normalized_form_id,),
            )
            cursor.execute(
                "DELETE FROM form_responses WHERE form_id = %s",
                (normalized_form_id,),
            )

            response_rows: list[tuple[str, str, str, str, str, str, str]] = []
            answer_rows: list[tuple[str, str, str, str, str]] = []
            for response in responses:
                response_id = str(response.get("responseId", "") or "").strip()
                if not response_id:
                    continue
                created_time = str(response.get("createTime", "") or "").strip()
                last_submitted_time = str(response.get("lastSubmittedTime", "") or "").strip()
                respondent_email = str(response.get("respondentEmail", "") or "").strip()
                response_rows.append(
                    (
                        normalized_form_id,
                        response_id,
                        created_time,
                        last_submitted_time,
                        respondent_email,
                        json.dumps(response, ensure_ascii=False),
                        synced_at,
                    )
                )
                answers = response.get("answers", {})
                if not isinstance(answers, dict):
                    continue
                for question_id, answer in answers.items():
                    if not isinstance(question_id, str):
                        continue
                    if not isinstance(answer, dict):
                        answer = {}
                    answer_rows.append(
                        (
                            normalized_form_id,
                            response_id,
                            question_id,
                            str(question_lookup.get(question_id, question_id) or question_id),
                            _stringify_form_answer(answer),
                        )
                    )

            if response_rows:
                cursor.executemany(
                    """
                    INSERT INTO form_responses (
                        form_id, response_id, created_time, last_submitted_time,
                        respondent_email, response_json, synced_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s)
                    """,
                    response_rows,
                )
            if answer_rows:
                cursor.executemany(
                    """
                    INSERT INTO form_response_answers (
                        form_id, response_id, question_id, question_title, answer_text
                    )
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    answer_rows,
                )

    return {
        "ok": True,
        "status": "synced",
        "formId": normalized_form_id,
        "responseCount": len(response_rows),
        "answerCount": len(answer_rows),
        "store": _describe_form_response_store(),
        "syncedAt": synced_at,
    }


def _get_latest_form_sheet_link() -> tuple[str, dict[str, Any]]:
    """Return the most recently linked form entry, if any."""
    links = _load_form_sheet_links()
    if not links:
        return "", {}

    def sort_key(item: tuple[str, dict[str, Any]]) -> str:
        _, details = item
        linked_at = str(details.get("linkedAt", "") or "")
        return linked_at

    form_id, details = max(links.items(), key=sort_key)
    return form_id, details


def _find_form_id_by_spreadsheet_id(spreadsheet_id: str) -> str:
    """Find a linked form id from a spreadsheet id in the local registry."""
    normalized = spreadsheet_id.strip()
    if not normalized:
        return ""

    for form_id, details in _load_form_sheet_links().items():
        if str(details.get("spreadsheetId", "") or "").strip() == normalized:
            return form_id
    return ""


def _build_forms_service() -> Any:
    """Create a Google Forms API client."""
    credentials = _load_google_workspace_credentials()
    return build_google_api(
        "forms",
        "v1",
        credentials=credentials,
        cache_discovery=False,
    )


def _build_sheets_service() -> Any:
    """Create a Google Sheets API client."""
    credentials = _load_google_workspace_credentials()
    return build_google_api(
        "sheets",
        "v4",
        credentials=credentials,
        cache_discovery=False,
    )


def _build_drive_service() -> Any:
    """Create a Google Drive API client."""
    credentials = _load_google_workspace_credentials()
    return build_google_api(
        "drive",
        "v3",
        credentials=credentials,
        cache_discovery=False,
    )


def _build_apps_script_service() -> Any:
    """Create a Google Apps Script API client."""
    credentials = _load_google_apps_script_credentials()
    return build_google_api(
        "script",
        "v1",
        credentials=credentials,
        cache_discovery=False,
    )


def _apps_script_project_url(script_id: str) -> str:
    """Return the Apps Script editor URL for a script project."""
    return f"https://script.google.com/home/projects/{script_id}/edit"


def _derive_response_spreadsheet_title(form_title: str) -> str:
    """Return a simple linked-response spreadsheet title for a form."""
    cleaned_title = str(form_title or "").strip() or "Google Form"
    if any("\u0E00" <= char <= "\u0E7F" for char in cleaned_title):
        return f"{cleaned_title} - คำตอบแบบฟอร์ม"
    return f"{cleaned_title} - Form Responses"


def _create_response_spreadsheet(form_title: str) -> dict[str, str]:
    """Create a Google Spreadsheet that will hold linked form responses."""
    sheets_service = _build_sheets_service()
    spreadsheet_title = _derive_response_spreadsheet_title(form_title)
    response = sheets_service.spreadsheets().create(
        body={"properties": {"title": spreadsheet_title}},
        fields="spreadsheetId,spreadsheetUrl,properties.title",
    ).execute()
    spreadsheet_id = str(response.get("spreadsheetId", "") or "").strip()
    if not spreadsheet_id:
        raise RuntimeError("Google Sheets API did not return a spreadsheetId.")

    spreadsheet_url = str(response.get("spreadsheetUrl", "") or "").strip()
    if not spreadsheet_url:
        spreadsheet_url = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit"

    resolved_title = str(response.get("properties", {}).get("title", "") or "").strip()
    return {
        "spreadsheetId": spreadsheet_id,
        "spreadsheetTitle": resolved_title or spreadsheet_title,
        "spreadsheetUrl": spreadsheet_url,
    }


def _build_native_linker_script_files() -> list[dict[str, Any]]:
    """Return Apps Script files that can set a form's native Sheets destination."""
    code_source = """
function openFormWithRetry_(formId) {
  let form = null;
  let lastError = null;
  for (let attempt = 0; attempt < 5; attempt += 1) {
    try {
      form = FormApp.openById(formId);
      return form;
    } catch (err) {
      lastError = err;
      Utilities.sleep(1500);
    }
  }
  throw lastError || new Error('Unable to open form by id.');
}

function linkFormToSheet(formId, spreadsheetId) {
  if (!formId || !spreadsheetId) {
    throw new Error('formId and spreadsheetId are required.');
  }

  const form = openFormWithRetry_(formId);
  let lastError = null;
  for (let attempt = 0; attempt < 5; attempt += 1) {
    try {
      form.setDestination(FormApp.DestinationType.SPREADSHEET, spreadsheetId);
      if (String(form.getDestinationId() || '') === String(spreadsheetId)) {
        break;
      }
      throw new Error('Destination did not match requested spreadsheet yet.');
    } catch (err) {
      lastError = err;
      Utilities.sleep(1500);
    }
  }
  if (String(form.getDestinationId() || '') !== String(spreadsheetId)) {
    throw lastError || new Error('Unable to link form to the requested spreadsheet.');
  }

  return {
    formId: form.getId(),
    destinationId: form.getDestinationId(),
    destinationType: String(form.getDestinationType()),
    editUrl: form.getEditUrl(),
  };
}

function inspectFormDestination(formId) {
  if (!formId) {
    throw new Error('formId is required.');
  }

  const form = openFormWithRetry_(formId);
  return {
    formId: form.getId(),
    destinationId: String(form.getDestinationId() || ''),
    destinationType: String(form.getDestinationType() || ''),
    editUrl: form.getEditUrl(),
  };
}

function ping() {
  return {
    ok: true,
    timestamp: new Date().toISOString(),
  };
}

function insertFormImages(formId, placements) {
  if (!formId) {
    throw new Error('formId is required.');
  }
  if (!Array.isArray(placements)) {
    throw new Error('placements must be an array.');
  }

  const form = openFormWithRetry_(formId);
  const orderedPlacements = placements
    .filter(p => p && p.base64 && p.mimeType)
    .sort((a, b) => (Number(a.index || 0) - Number(b.index || 0)));

  const created = [];
  let offset = 0;
  orderedPlacements.forEach((placement, placementIndex) => {
    const bytes = Utilities.base64Decode(String(placement.base64 || ''));
    const blob = Utilities.newBlob(
      bytes,
      String(placement.mimeType || 'application/octet-stream'),
      String(placement.name || ('form-image-' + (placementIndex + 1)))
    );

    const item = form.addImageItem().setImage(blob);
    if (placement.title) {
      item.setTitle(String(placement.title));
    }
    if (placement.helpText) {
      item.setHelpText(String(placement.helpText));
    }
    if (placement.width) {
      item.setWidth(Number(placement.width));
    }

    const items = form.getItems();
    const maxIndex = Math.max(0, items.length - 1);
    const requestedIndex = Number(placement.index || 0) + offset;
    const targetIndex = Math.max(0, Math.min(maxIndex, requestedIndex));
    form.moveItem(item.getIndex(), targetIndex);
    offset += 1;

    created.push({
      itemId: item.getId(),
      index: item.getIndex(),
      title: item.getTitle(),
    });
  });

  return {
    ok: true,
    createdCount: created.length,
    created: created,
  };
}
""".strip()
    manifest = {
        "timeZone": "Asia/Bangkok",
        "exceptionLogging": "STACKDRIVER",
        "runtimeVersion": "V8",
        "executionApi": {"access": "ANYONE"},
        "oauthScopes": [
            "https://www.googleapis.com/auth/forms",
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/script.scriptapp",
        ],
    }
    return [
        {"name": "Code", "type": "SERVER_JS", "source": code_source},
        {
            "name": "appsscript",
            "type": "JSON",
            "source": json.dumps(manifest, ensure_ascii=False, indent=2),
        },
    ]


def _ensure_native_linker_project(script_service: Any) -> tuple[str, bool]:
    """Return a reusable Apps Script project id, creating one if needed."""
    script_id = _get_configured_apps_script_id()
    if script_id:
        return script_id, False

    response = script_service.projects().create(
        body={"title": "Google Form Agent Native Linker"}
    ).execute()
    script_id = str(response.get("scriptId", "") or "").strip()
    if not script_id:
        raise RuntimeError("Apps Script API did not return a scriptId.")

    _upsert_apps_script_config(
        {
            "scriptId": script_id,
            "scriptUrl": _apps_script_project_url(script_id),
            "createdAt": datetime.now(timezone.utc).isoformat(),
        }
    )
    return script_id, True


def _update_native_linker_project_content(script_service: Any, script_id: str) -> None:
    """Push the native-link helper source into the Apps Script project."""
    script_service.projects().updateContent(
        scriptId=script_id,
        body={"files": _build_native_linker_script_files()},
    ).execute()


def _create_native_linker_deployment(
    script_service: Any,
    script_id: str,
    *,
    persist_shared_config: bool = True,
) -> str:
    """Create a fresh API executable deployment for the native-link script."""
    version = script_service.projects().versions().create(
        scriptId=script_id,
        body={"description": "Google Form Agent native linker runtime"},
    ).execute()
    version_number = version.get("versionNumber")
    if not isinstance(version_number, int):
        raise RuntimeError("Apps Script API did not return a valid version number.")

    deployment = script_service.projects().deployments().create(
        scriptId=script_id,
        body={
            "versionNumber": version_number,
            "manifestFileName": "appsscript",
            "description": "Google Form Agent native linker runtime",
        },
    ).execute()
    deployment_id = str(deployment.get("deploymentId", "") or "").strip()
    if not deployment_id:
        raise RuntimeError("Apps Script API did not return a deploymentId.")

    if persist_shared_config:
        _upsert_apps_script_config(
            {
                "scriptId": script_id,
                "scriptUrl": _apps_script_project_url(script_id),
                "deploymentId": deployment_id,
                "versionNumber": version_number,
                "updatedAt": datetime.now(timezone.utc).isoformat(),
            }
        )
    return deployment_id


def _wait_for_native_linker_runtime_ready(
    script_service: Any,
    deployment_id: str,
    *,
    attempts: int = 20,
    delay_seconds: float = 2.0,
) -> None:
    """Wait until a freshly deployed Apps Script runtime can answer Execution API calls."""
    last_error: str = ""
    for attempt in range(attempts):
        try:
            response = script_service.scripts().run(
                scriptId=deployment_id,
                body={"function": "ping", "parameters": []},
            ).execute()
            if not (isinstance(response, dict) and response.get("error")):
                return
            last_error = json.dumps(response.get("error"), ensure_ascii=False)
        except Exception as exc:  # pragma: no cover - depends on Google API runtime
            last_error = str(exc)
        if attempt < attempts - 1:
            time.sleep(delay_seconds)
    raise RuntimeError(
        "Apps Script runtime was deployed but did not become ready for execution. "
        f"Last error: {last_error}"
    )


def _build_apps_script_run_body(
    function_name: str,
    parameters: list[Any],
) -> dict[str, Any]:
    """Build an Execution API payload."""
    return {
        "function": function_name,
        "parameters": parameters,
    }


def _ensure_native_linker_deployment(script_service: Any) -> dict[str, str]:
    """Ensure an Apps Script project and API deployment exist for native linking."""
    script_id, created = _ensure_native_linker_project(script_service)
    _update_native_linker_project_content(script_service, script_id)
    deployment_id = _create_native_linker_deployment(script_service, script_id)
    _wait_for_native_linker_runtime_ready(script_service, deployment_id)

    return {
        "scriptId": script_id,
        "deploymentId": deployment_id,
        "scriptUrl": _apps_script_project_url(script_id),
    }


def _ensure_locally_managed_native_linker_deployment(script_service: Any) -> dict[str, str]:
    """Ensure a per-user managed Apps Script project and API deployment exist."""
    session_key = GOOGLE_OAUTH_SESSION_KEY.get()
    script_id = _get_locally_managed_apps_script_id()
    if not script_id:
        response = script_service.projects().create(
            body={"title": "Google Form Agent Native Linker (Managed Fallback)"}
        ).execute()
        script_id = str(response.get("scriptId", "") or "").strip()
        if not script_id:
            raise RuntimeError("Apps Script API did not return a managed fallback scriptId.")
        created_at = datetime.now(timezone.utc).isoformat()
        if session_key:
            config = _load_apps_script_config()
            managed_sessions = config.get("managedSessions", {})
            if not isinstance(managed_sessions, dict):
                managed_sessions = {}
            session_entry = managed_sessions.get(session_key, {})
            if not isinstance(session_entry, dict):
                session_entry = {}
            session_entry.update(
                {
                    "scriptId": script_id,
                    "scriptUrl": _apps_script_project_url(script_id),
                    "createdAt": created_at,
                }
            )
            managed_sessions[session_key] = session_entry
            config["managedSessions"] = managed_sessions
            _save_apps_script_config(config)
        else:
            _upsert_apps_script_config(
                {
                    "managedScriptId": script_id,
                    "managedScriptUrl": _apps_script_project_url(script_id),
                    "managedCreatedAt": created_at,
                }
            )

    _update_native_linker_project_content(script_service, script_id)
    deployment_id = _create_native_linker_deployment(
        script_service,
        script_id,
        persist_shared_config=False,
    )
    _wait_for_native_linker_runtime_ready(script_service, script_id)
    updated_at = datetime.now(timezone.utc).isoformat()
    if session_key:
        config = _load_apps_script_config()
        managed_sessions = config.get("managedSessions", {})
        if not isinstance(managed_sessions, dict):
            managed_sessions = {}
        session_entry = managed_sessions.get(session_key, {})
        if not isinstance(session_entry, dict):
            session_entry = {}
        session_entry.update(
            {
                "scriptId": script_id,
                "deploymentId": deployment_id,
                "scriptUrl": _apps_script_project_url(script_id),
                "updatedAt": updated_at,
            }
        )
        managed_sessions[session_key] = session_entry
        config["managedSessions"] = managed_sessions
        _save_apps_script_config(config)
    else:
        _upsert_apps_script_config(
            {
                "managedScriptId": script_id,
                "managedDeploymentId": deployment_id,
                "managedScriptUrl": _apps_script_project_url(script_id),
                "managedUpdatedAt": updated_at,
            }
        )
    return {
        "scriptId": script_id,
        "deploymentId": deployment_id,
        "scriptUrl": _apps_script_project_url(script_id),
    }


def _get_shared_native_linker_runtime() -> dict[str, str]:
    """Return the shared Apps Script runtime configuration for native linking."""
    script_id = _get_configured_apps_script_id()
    deployment_id = _get_configured_apps_script_deployment_id()
    if not script_id or not deployment_id:
        return {}
    return {
        "scriptId": script_id,
        "deploymentId": deployment_id,
        "scriptUrl": _apps_script_project_url(script_id),
    }


def _get_missing_shared_runtime_guidance() -> str:
    """Return setup guidance when no shared Apps Script runtime is configured."""
    return (
        "Configure a shared Apps Script runtime before using automatic linking or image "
        "insertion. Set GOOGLE_APPS_SCRIPT_PROJECT_ID and GOOGLE_APPS_SCRIPT_DEPLOYMENT_ID "
        "for the shared API executable, or configure GOOGLE_APPS_SCRIPT_WEB_APP_URL, "
        "GOOGLE_APPS_SCRIPT_SHARED_SECRET, and GOOGLE_APPS_SCRIPT_ACTOR_EMAIL for the shared "
        "web-app path."
    )


def _get_native_link_webapp_config() -> dict[str, str]:
    """Return optional shared Apps Script web app configuration for native linking."""
    web_app_url = os.getenv("GOOGLE_APPS_SCRIPT_WEB_APP_URL", "").strip()
    shared_secret = os.getenv("GOOGLE_APPS_SCRIPT_SHARED_SECRET", "").strip()
    actor_email = os.getenv("GOOGLE_APPS_SCRIPT_ACTOR_EMAIL", "").strip()
    if not web_app_url or not shared_secret or not actor_email:
        return {}
    return {
        "webAppUrl": web_app_url,
        "sharedSecret": shared_secret,
        "actorEmail": actor_email,
    }


def _share_drive_file_with_actor(file_id: str, actor_email: str) -> None:
    """Grant the configured Apps Script actor access to a Drive file."""
    drive_service = _build_drive_service()
    drive_service.permissions().create(
        fileId=file_id,
        sendNotificationEmail=False,
        body={
            "type": "user",
            "role": "writer",
            "emailAddress": actor_email,
        },
    ).execute()


def _make_drive_file_public(file_id: str) -> None:
    """Allow Google Forms to fetch a Drive-hosted image via public URL."""
    drive_service = _build_drive_service()
    drive_service.permissions().create(
        fileId=file_id,
        body={
            "type": "anyone",
            "role": "reader",
        },
        fields="id",
    ).execute()


def _upload_support_image_to_drive(image: dict[str, Any], fallback_name: str) -> dict[str, Any]:
    """Upload an embedded image to Drive and return a form-usable source URI."""
    mime_type = str(image.get("mime_type", "") or "application/octet-stream").strip()
    data_base64 = str(image.get("data_base64", "") or "").strip()
    if not data_base64:
        return image

    try:
        image_bytes = base64.b64decode(data_base64, validate=False)
    except Exception:
        return image

    extension = Path(str(image.get("name", "") or fallback_name)).suffix or ""
    drive_name = str(image.get("name", "") or fallback_name).strip() or fallback_name

    updated = dict(image)
    updated["inline_source_uri"] = f"data:{mime_type};base64,{data_base64}"

    try:
        drive_service = _build_drive_service()
        media = MediaInMemoryUpload(image_bytes, mimetype=mime_type, resumable=False)
        uploaded = drive_service.files().create(
            body={
                "name": drive_name,
                "mimeType": mime_type,
            },
            media_body=media,
            fields="id,webContentLink,webViewLink",
        ).execute()
        file_id = str(uploaded.get("id", "") or "").strip()
        if file_id:
            _make_drive_file_public(file_id)
            public_source_uri = f"https://drive.google.com/uc?export=download&id={file_id}"
            fallback_source_uri = str(uploaded.get("webContentLink", "") or "").strip()
            if fallback_source_uri:
                updated["fallback_source_uri"] = fallback_source_uri
            updated["public_source_uri"] = public_source_uri
            updated["source_uri"] = public_source_uri
            updated["drive_file_id"] = file_id
    except Exception:
        pass

    if not str(updated.get("source_uri", "") or "").strip():
        updated["source_uri"] = str(updated.get("inline_source_uri", "") or "").strip()

    if extension and "name" not in updated:
        updated["name"] = f"{fallback_name}{extension}"
    return updated


def _combine_embedded_images(
    images: list[dict[str, Any]],
    fallback_name: str,
) -> list[dict[str, Any]]:
    """Combine multiple embedded images into a single side-by-side PNG for one form choice."""
    valid_images = [
        image
        for image in images
        if isinstance(image, dict) and str(image.get("data_base64", "") or "").strip()
    ]
    if len(valid_images) <= 1:
        return valid_images

    decoded_images: list[Image.Image] = []
    image_refs: list[dict[str, Any]] = []
    try:
        for image in valid_images:
            raw = base64.b64decode(str(image.get("data_base64", "") or "").strip(), validate=False)
            pil_image = Image.open(io.BytesIO(raw)).convert("RGBA")
            decoded_images.append(pil_image)
            image_refs.append(image)

        padding = 16
        max_height = max(image.height for image in decoded_images)
        total_width = sum(image.width for image in decoded_images) + padding * (len(decoded_images) - 1)
        canvas = Image.new("RGBA", (total_width, max_height), (255, 255, 255, 0))

        cursor_x = 0
        for image in decoded_images:
            offset_y = max(0, (max_height - image.height) // 2)
            canvas.alpha_composite(image, (cursor_x, offset_y))
            cursor_x += image.width + padding

        output = io.BytesIO()
        canvas.save(output, format="PNG")
        combined_base64 = base64.b64encode(output.getvalue()).decode("ascii")
        alt_parts = [
            str(image.get("alt_text", "") or "").strip()
            for image in image_refs
            if str(image.get("alt_text", "") or "").strip()
        ]
        return [
            {
                "name": f"{Path(fallback_name).stem}.png",
                "mime_type": "image/png",
                "data_base64": combined_base64,
                "alt_text": " | ".join(alt_parts).strip(),
                "width": total_width,
            }
        ]
    except Exception:
        return valid_images
    finally:
        for image in decoded_images:
            try:
                image.close()
            except Exception:
                pass


def _materialize_image_list(
    images: Any,
    fallback_prefix: str,
) -> list[dict[str, Any]]:
    """Upload a list of embedded images and attach source URIs."""
    if not isinstance(images, list):
        return []

    uploaded_images: list[dict[str, Any]] = []
    for image_index, image in enumerate(images, 1):
        if not isinstance(image, dict):
            continue
        fallback_name = f"{fallback_prefix}-{image_index}"
        uploaded_images.append(_upload_support_image_to_drive(image, fallback_name))
    return uploaded_images


def _materialize_question_images(questions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Upload embedded images for questions and options and replace them with source URIs."""
    materialized: list[dict[str, Any]] = []
    for question_index, question in enumerate(questions, 1):
        updated_question = dict(question)
        updated_question["images"] = _materialize_image_list(
            question.get("images", []),
            f"form-question-image-{question_index}",
        )

        options = question.get("options", [])
        if isinstance(options, list):
            updated_options: list[Any] = []
            for option_index, option in enumerate(options, 1):
                if not isinstance(option, dict):
                    updated_options.append(option)
                    continue

                updated_option = dict(option)
                option_images = []
                if isinstance(option.get("images", []), list):
                    option_images.extend(option.get("images", []))
                if isinstance(option.get("extra_images", []), list):
                    option_images.extend(option.get("extra_images", []))
                combined_option_images = _combine_embedded_images(
                    option_images,
                    f"form-option-image-{question_index}-{option_index}",
                )
                updated_option["images"] = _materialize_image_list(
                    combined_option_images,
                    f"form-option-image-{question_index}-{option_index}",
                )
                updated_option["extra_images"] = []
                updated_options.append(updated_option)
            updated_question["options"] = updated_options

        materialized.append(updated_question)
    return materialized


def _strip_images_from_questions_for_rest(
    questions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return a copy of the questions with only REST-compatible images preserved."""
    def keep_rest_image(image: Any) -> list[dict[str, Any]]:
        if not isinstance(image, dict):
            return []
        source_uri = str(image.get("source_uri", "") or "").strip()
        if (
            source_uri
            and len(source_uri) <= 2048
            and source_uri.startswith(("http://", "https://"))
        ):
            return [image]
        return []

    stripped_questions: list[dict[str, Any]] = []
    for question in questions:
        updated_question = dict(question)
        question_images = question.get("images", [])
        if isinstance(question_images, list) and question_images:
            first_question_image = question_images[0]
            updated_question["images"] = keep_rest_image(first_question_image)
        else:
            updated_question["images"] = []
        options = question.get("options", [])
        if isinstance(options, list):
            updated_options: list[Any] = []
            for option in options:
                if not isinstance(option, dict):
                    updated_options.append(option)
                    continue
                updated_option = dict(option)
                option_images = option.get("images", [])
                if isinstance(option_images, list) and option_images:
                    first_image = option_images[0]
                    updated_option["images"] = keep_rest_image(first_image)
                else:
                    updated_option["images"] = []
                updated_option["extra_images"] = []
                updated_options.append(updated_option)
            updated_question["options"] = updated_options
        stripped_questions.append(updated_question)
    return stripped_questions


def _build_apps_script_image_placements(
    questions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build a placement plan for inserting non-REST-compatible images via Apps Script blobs."""
    placements: list[dict[str, Any]] = []
    base_item_index = 0
    for question in questions:
        question_title = str(question.get("title", "") or "").strip()
        question_images = question.get("images", [])
        if isinstance(question_images, list):
            extra_question_images = question_images[1:] if len(question_images) > 1 else []
            for image_index, image in enumerate(extra_question_images, 2):
                if not isinstance(image, dict):
                    continue
                base64_data = str(image.get("data_base64", "") or "").strip()
                mime_type = str(image.get("mime_type", "") or "").strip()
                if not base64_data or not mime_type:
                    continue
                placements.append(
                    {
                        "index": base_item_index,
                        "title": str(image.get("alt_text", "") or "").strip()
                        or f"Image for {question_title}",
                        "helpText": question_title,
                        "base64": base64_data,
                        "mimeType": mime_type,
                        "name": str(image.get("name", "") or f"question-image-{base_item_index + 1}-{image_index}").strip(),
                        "width": image.get("width"),
                    }
                )

        options = question.get("options", [])
        if isinstance(options, list):
            for option_index, option in enumerate(options):
                if not isinstance(option, dict):
                    continue
                option_label = str(option.get("label", "") or _option_label_for_index(option_index)).strip()
                option_value = str(option.get("value", "") or "").strip()
                option_images = option.get("extra_images", [])
                if not isinstance(option_images, list):
                    continue
                for image_index, image in enumerate(option_images, 1):
                    if not isinstance(image, dict):
                        continue
                    base64_data = str(image.get("data_base64", "") or "").strip()
                    mime_type = str(image.get("mime_type", "") or "").strip()
                    if not base64_data or not mime_type:
                        continue
                    placements.append(
                        {
                            "index": base_item_index + 1,
                            "title": str(image.get("alt_text", "") or "").strip()
                            or f"Choice {option_label}",
                            "helpText": f"{question_title}\nChoice {option_label}: {option_value}".strip(),
                            "base64": base64_data,
                            "mimeType": mime_type,
                            "name": str(
                                image.get("name", "")
                                or f"option-image-{base_item_index + 1}-{option_label}-extra-{image_index}"
                            ).strip(),
                            "width": image.get("width"),
                        }
                    )

        base_item_index += 1

    return placements


def _share_native_link_targets_with_actor(form_id: str, spreadsheet_id: str, actor_email: str) -> None:
    """Share both form and spreadsheet with the shared Apps Script actor account."""
    _share_drive_file_with_actor(form_id, actor_email)
    _share_drive_file_with_actor(spreadsheet_id, actor_email)


def _link_form_to_sheet_via_webapp(
    form_id: str,
    spreadsheet_id: str,
    web_app_url: str,
    shared_secret: str,
) -> dict[str, Any]:
    """Call a shared Apps Script web app that performs native form-to-sheet linking."""
    payload = json.dumps(
        {
            "secret": shared_secret,
            "formId": form_id,
            "spreadsheetId": spreadsheet_id,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib_request.Request(
        web_app_url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib_request.urlopen(request, timeout=60) as response:
            raw_body = response.read().decode("utf-8", errors="replace")
    except urllib_error.HTTPError as exc:
        raw_body = exc.read().decode("utf-8", errors="replace")
        return {
            "ok": False,
            "status": "native-link-webapp-http-error",
            "error": f"Apps Script web app returned HTTP {exc.code}: {raw_body}",
        }
    except Exception as exc:
        return {
            "ok": False,
            "status": "native-link-webapp-error",
            "error": str(exc),
        }

    try:
        result = json.loads(raw_body)
    except json.JSONDecodeError:
        return {
            "ok": False,
            "status": "native-link-webapp-invalid-response",
            "error": f"Apps Script web app returned non-JSON content: {raw_body}",
        }

    destination_id = str(result.get("destinationId", "") or "").strip()
    success = bool(result.get("ok")) and destination_id == spreadsheet_id
    return {
        "ok": success,
        "status": "linked" if success else str(result.get("status", "") or "native-link-webapp-failed"),
        "destinationId": destination_id,
        "destinationType": str(result.get("destinationType", "") or "").strip(),
        "editUrl": str(result.get("editUrl", "") or "").strip(),
        "raw": result,
        "webAppUrl": web_app_url,
    }


def _link_form_to_sheet_natively(form_id: str, spreadsheet_id: str) -> dict[str, Any]:
    """Attempt to set the form's native Google Sheets response destination."""
    web_app_config = _get_native_link_webapp_config()
    webapp_failure: dict[str, Any] | None = None
    if web_app_config:
        try:
            _share_native_link_targets_with_actor(
                form_id=form_id,
                spreadsheet_id=spreadsheet_id,
                actor_email=web_app_config["actorEmail"],
            )
        except Exception as exc:
            webapp_failure = {
                "ok": False,
                "status": "native-link-share-failed",
                "error": str(exc),
                "guidance": (
                    "The backend could not share the created form and sheet with the shared "
                    "Apps Script actor account required for web-app-based native linking."
                ),
            }
        else:
            webapp_result = _link_form_to_sheet_via_webapp(
                form_id=form_id,
                spreadsheet_id=spreadsheet_id,
                web_app_url=web_app_config["webAppUrl"],
                shared_secret=web_app_config["sharedSecret"],
            )
            if webapp_result.get("ok"):
                webapp_result["mode"] = "web-app"
                return webapp_result
            webapp_failure = {
                **webapp_result,
                "mode": "web-app",
                "guidance": (
                    str(webapp_result.get("guidance", "") or "").strip()
                    or "Shared Apps Script web app native linking failed."
                ),
            }

    script_service = _build_apps_script_service()
    runtime = _get_shared_native_linker_runtime()

    if not runtime:
        result = {
            "ok": False,
            "status": "shared-runtime-not-configured",
            "error": "No shared Apps Script runtime is configured.",
            "guidance": _get_missing_shared_runtime_guidance(),
        }
        if webapp_failure:
            result["webAppFailure"] = webapp_failure
        return result

    def _verify_destination_with_runtime(
        target_runtime: dict[str, str],
        *,
        mode: str,
    ) -> dict[str, Any] | None:
        deployment_id = str(target_runtime.get("deploymentId", "") or "").strip()
        if not deployment_id:
            return None
        try:
            verification_response = script_service.scripts().run(
                scriptId=deployment_id,
                body=_build_apps_script_run_body(
                    "inspectFormDestination",
                    [form_id],
                ),
            ).execute()
        except Exception:
            return None

        if isinstance(verification_response, dict) and verification_response.get("error"):
            return None

        verification_result = (
            verification_response.get("response", {}).get("result", {})
            if isinstance(verification_response, dict)
            else {}
        )
        destination_id = str(verification_result.get("destinationId", "") or "").strip()
        if destination_id != spreadsheet_id:
            return None
        return {
            "ok": True,
            "status": "linked",
            "mode": mode,
            "scriptId": str(target_runtime.get("scriptId", "") or "").strip(),
            "deploymentId": str(target_runtime.get("deploymentId", "") or "").strip(),
            "scriptUrl": str(target_runtime.get("scriptUrl", "") or "").strip(),
            "destinationId": destination_id,
            "destinationType": str(
                verification_result.get("destinationType", "") or ""
            ).strip(),
            "editUrl": str(verification_result.get("editUrl", "") or "").strip(),
            "raw": verification_result,
        }

    def _try_shared_runtime_or_webapp_fallback(
        primary_error: str,
        primary_guidance: str,
    ) -> dict[str, Any] | None:
        shared_runtime = _get_shared_native_linker_runtime()
        if shared_runtime and shared_runtime.get("deploymentId") != runtime.get("deploymentId"):
            try:
                shared_response = script_service.scripts().run(
                    scriptId=shared_runtime["deploymentId"],
                    body=_build_apps_script_run_body(
                        "linkFormToSheet",
                        [form_id, spreadsheet_id],
                    ),
                ).execute()
            except HttpError as shared_exc:  # pragma: no cover - depends on Google API runtime
                shared_message = _describe_apps_script_http_error(shared_exc)
                verified = _verify_destination_with_runtime(
                    shared_runtime,
                    mode="shared-runtime-fallback-verified",
                )
                if verified:
                    return verified
            except Exception as shared_exc:  # pragma: no cover - depends on Google API runtime
                shared_message = str(shared_exc)
                verified = _verify_destination_with_runtime(
                    shared_runtime,
                    mode="shared-runtime-fallback-verified",
                )
                if verified:
                    return verified
            else:
                if isinstance(shared_response, dict) and shared_response.get("error"):
                    shared_message = json.dumps(shared_response.get("error"), ensure_ascii=False)
                    verified = _verify_destination_with_runtime(
                        shared_runtime,
                        mode="shared-runtime-fallback-verified",
                    )
                    if verified:
                        return verified
                else:
                    shared_result = (
                        shared_response.get("response", {}).get("result", {})
                        if isinstance(shared_response, dict)
                        else {}
                    )
                    shared_destination_id = str(
                        shared_result.get("destinationId", "") or ""
                    ).strip()
                    if shared_destination_id == spreadsheet_id:
                        return {
                            "ok": True,
                            "status": "linked",
                            "mode": "shared-runtime-fallback",
                            "scriptId": shared_runtime["scriptId"],
                            "deploymentId": shared_runtime["deploymentId"],
                            "scriptUrl": shared_runtime["scriptUrl"],
                            "destinationId": shared_destination_id,
                            "destinationType": str(
                                shared_result.get("destinationType", "") or ""
                            ).strip(),
                            "editUrl": str(shared_result.get("editUrl", "") or "").strip(),
                            "raw": shared_result,
                        }
                    verified = _verify_destination_with_runtime(
                        shared_runtime,
                        mode="shared-runtime-fallback-verified",
                    )
                    if verified:
                        return verified
                    shared_message = json.dumps(shared_result, ensure_ascii=False)
            return {
                "ok": False,
                "status": "native-link-failed",
                "error": primary_error,
                "guidance": primary_guidance,
                "sharedFallbackError": shared_message,
                "scriptId": runtime.get("scriptId", ""),
                "deploymentId": runtime.get("deploymentId", ""),
                "scriptUrl": runtime.get("scriptUrl", ""),
            }
        else:
            return {
                "ok": False,
                "status": "native-link-failed",
                "error": primary_error,
                "guidance": primary_guidance,
                "scriptId": runtime.get("scriptId", ""),
                "deploymentId": runtime.get("deploymentId", ""),
                "scriptUrl": runtime.get("scriptUrl", ""),
            }

    response: dict[str, Any] | None = None
    last_error_result: dict[str, Any] | None = None
    for attempt in range(3):
        try:
            response = script_service.scripts().run(
                scriptId=runtime["deploymentId"],
                body=_build_apps_script_run_body(
                    "linkFormToSheet",
                    [form_id, spreadsheet_id],
                ),
            ).execute()
            break
        except HttpError as exc:  # pragma: no cover - depends on Google API runtime
            message = _describe_apps_script_http_error(exc)
            verified = _verify_destination_with_runtime(
                runtime,
                mode="verified-current-runtime",
            )
            if verified:
                return verified
            guidance = (
                "Native linking requires the configured shared Apps Script API executable to use "
                "the same standard Google Cloud project as this app's OAuth client, with the Apps "
                "Script API enabled. Reconnect Google after adding the new script scopes if needed."
            )
            last_error_result = {
                "ok": False,
                "status": "native-link-failed",
                "mode": "shared-runtime",
                "error": message,
                "guidance": guidance,
                "scriptId": runtime["scriptId"],
                "deploymentId": runtime["deploymentId"],
                "scriptUrl": runtime["scriptUrl"],
            }
            if webapp_failure:
                last_error_result["webAppFailure"] = webapp_failure
            if "Requested entity was not found" not in message or attempt == 2:
                return last_error_result
            time.sleep(2)
        except Exception as exc:  # pragma: no cover - depends on Google API runtime
            message = str(exc)
            verified = _verify_destination_with_runtime(
                runtime,
                mode="verified-current-runtime",
            )
            if verified:
                return verified
            guidance = (
                "Native linking requires an Apps Script API executable that shares the same "
                "standard Google Cloud project as this app's OAuth client, with the Apps Script "
                "API enabled. Reconnect Google after adding the new script scopes if needed."
            )
            last_error_result = {
                "ok": False,
                "status": "native-link-failed",
                "mode": "shared-runtime",
                "error": message,
                "guidance": guidance,
                "scriptId": runtime["scriptId"],
                "deploymentId": runtime["deploymentId"],
                "scriptUrl": runtime["scriptUrl"],
            }
            if webapp_failure:
                last_error_result["webAppFailure"] = webapp_failure
            if "Requested entity was not found" not in message or attempt == 2:
                return last_error_result
            time.sleep(2)

    if response is None:
        if last_error_result:
            return last_error_result
        fallback_result = {
            "ok": False,
            "status": "native-link-failed",
            "mode": "shared-runtime",
            "error": "Unknown native-link failure.",
            "guidance": "The Apps Script runtime could not complete native form-to-sheet linking.",
            "scriptId": runtime["scriptId"],
            "deploymentId": runtime["deploymentId"],
            "scriptUrl": runtime["scriptUrl"],
        }
        if webapp_failure:
            fallback_result["webAppFailure"] = webapp_failure
        return fallback_result

    if isinstance(response, dict) and response.get("error"):
        error_payload = json.dumps(response.get("error"), ensure_ascii=False)
        verified = _verify_destination_with_runtime(
            runtime,
            mode="verified-current-runtime",
        )
        if verified:
            return verified
        guidance = (
            "The Apps Script runtime executed but did not complete the native link. "
            "Verify the script project's Cloud project setup and OAuth scopes."
        )
        error_result = {
            "ok": False,
            "status": "native-link-failed",
            "mode": "shared-runtime",
            "error": error_payload,
            "guidance": guidance,
            "scriptId": runtime["scriptId"],
            "deploymentId": runtime["deploymentId"],
            "scriptUrl": runtime["scriptUrl"],
        }
        if webapp_failure:
            error_result["webAppFailure"] = webapp_failure
        return error_result

    result = response.get("response", {}).get("result", {}) if isinstance(response, dict) else {}
    destination_id = str(result.get("destinationId", "") or "").strip()
    success_result = {
        "ok": destination_id == spreadsheet_id,
        "status": "linked" if destination_id == spreadsheet_id else "mismatch",
        "scriptId": runtime["scriptId"],
        "deploymentId": runtime["deploymentId"],
        "scriptUrl": runtime["scriptUrl"],
        "destinationId": destination_id,
        "destinationType": str(result.get("destinationType", "") or "").strip(),
        "editUrl": str(result.get("editUrl", "") or "").strip(),
        "raw": result,
    }
    if webapp_failure:
        success_result["webAppFailure"] = webapp_failure
    return success_result


def _insert_form_images_via_apps_script(
    form_id: str,
    placements: list[dict[str, Any]],
) -> dict[str, Any]:
    """Insert images into a Google Form using Apps Script blobs."""
    if not placements:
        return {"ok": True, "createdCount": 0, "created": []}

    script_service = _build_apps_script_service()
    runtime = _get_shared_native_linker_runtime()
    if not runtime:
        return {
            "ok": False,
            "status": "shared-runtime-not-configured",
            "error": "No shared Apps Script runtime is configured.",
            "guidance": _get_missing_shared_runtime_guidance(),
        }

    response: dict[str, Any] | None = None
    last_error_result: dict[str, Any] | None = None
    for attempt in range(3):
        try:
            response = script_service.scripts().run(
                scriptId=runtime["deploymentId"],
                body=_build_apps_script_run_body(
                    "insertFormImages",
                    [form_id, placements],
                ),
            ).execute()
            break
        except HttpError as exc:  # pragma: no cover - depends on Google API runtime
            message = _describe_apps_script_http_error(exc)
            guidance = "The Apps Script runtime could not insert images into the Google Form."
            last_error_result = {
                "ok": False,
                "status": "apps-script-image-insert-failed",
                "error": message,
                "guidance": guidance,
                "scriptId": runtime.get("scriptId", ""),
                "deploymentId": runtime.get("deploymentId", ""),
            }
            if "Requested entity was not found" not in message or attempt == 2:
                return last_error_result
            time.sleep(2)
        except Exception as exc:  # pragma: no cover - depends on Google API runtime
            message = str(exc)
            guidance = "The Apps Script runtime could not insert images into the Google Form."
            last_error_result = {
                "ok": False,
                "status": "apps-script-image-insert-failed",
                "error": message,
                "guidance": guidance,
                "scriptId": runtime.get("scriptId", ""),
                "deploymentId": runtime.get("deploymentId", ""),
            }
            if "Requested entity was not found" not in message or attempt == 2:
                return last_error_result
            time.sleep(2)

    if response is None:
        return last_error_result or {
            "ok": False,
            "status": "apps-script-image-insert-failed",
            "error": "Unknown Apps Script image insertion failure.",
            "guidance": "The Apps Script runtime could not insert images into the Google Form.",
            "scriptId": runtime.get("scriptId", ""),
            "deploymentId": runtime.get("deploymentId", ""),
        }

    if isinstance(response, dict) and response.get("error"):
        error_payload = json.dumps(response.get("error"), ensure_ascii=False)
        guidance = "The Apps Script runtime ran but did not complete image insertion."
        return {
            "ok": False,
            "status": "apps-script-image-insert-failed",
            "error": error_payload,
            "guidance": guidance,
            "scriptId": runtime.get("scriptId", ""),
            "deploymentId": runtime.get("deploymentId", ""),
        }

    result = response.get("response", {}).get("result", {}) if isinstance(response, dict) else {}
    created_count = int(result.get("createdCount", 0) or 0)
    return {
        "ok": True,
        "status": "images-inserted",
        "createdCount": created_count,
        "created": result.get("created", []),
        "scriptId": runtime.get("scriptId", ""),
        "deploymentId": runtime.get("deploymentId", ""),
        "scriptUrl": runtime.get("scriptUrl", ""),
    }


def _extract_form_question_map(form_payload: dict[str, Any]) -> list[dict[str, str]]:
    """Collect question IDs and titles from a Form resource."""
    question_map: list[dict[str, str]] = []
    for item in form_payload.get("items", []):
        if not isinstance(item, dict):
            continue
        question_item = item.get("questionItem")
        if not isinstance(question_item, dict):
            continue
        question = question_item.get("question")
        if not isinstance(question, dict):
            continue
        question_id = question.get("questionId")
        title = item.get("title")
        if isinstance(question_id, str) and question_id and isinstance(title, str) and title:
            question_map.append({"questionId": question_id, "title": title})
    return question_map


def _stringify_form_answer(answer: dict[str, Any]) -> str:
    """Convert a Google Forms answer payload into a readable string."""
    text_answers = answer.get("textAnswers", {}).get("answers", [])
    if isinstance(text_answers, list) and text_answers:
        values = [
            entry.get("value", "").strip()
            for entry in text_answers
            if isinstance(entry, dict) and isinstance(entry.get("value"), str)
        ]
        values = [value for value in values if value]
        if values:
            return "; ".join(values)

    file_answers = answer.get("fileUploadAnswers", {}).get("answers", [])
    if isinstance(file_answers, list) and file_answers:
        file_ids = [
            entry.get("fileId", "").strip()
            for entry in file_answers
            if isinstance(entry, dict) and isinstance(entry.get("fileId"), str)
        ]
        file_ids = [file_id for file_id in file_ids if file_id]
        if file_ids:
            return "; ".join(file_ids)

    grade = answer.get("grade")
    if isinstance(grade, dict):
        score = grade.get("score")
        if score is not None:
            return str(score)

    return ""


def _build_response_rows(
    form_payload: dict[str, Any],
    responses_payload: dict[str, Any],
) -> list[list[str]]:
    """Build spreadsheet rows from a form definition and responses list."""
    question_map = _extract_form_question_map(form_payload)
    headers = ["Response ID", "Created Time", "Last Submitted Time", "Respondent Email"]
    headers.extend(question["title"] for question in question_map)

    rows: list[list[str]] = [headers]
    for response in responses_payload.get("responses", []) or []:
        if not isinstance(response, dict):
            continue
        answers = response.get("answers", {})
        if not isinstance(answers, dict):
            answers = {}

        row = [
            str(response.get("responseId", "") or ""),
            str(response.get("createTime", "") or ""),
            str(response.get("lastSubmittedTime", "") or ""),
            str(response.get("respondentEmail", "") or ""),
        ]
        for question in question_map:
            row.append(_stringify_form_answer(answers.get(question["questionId"], {})))
        rows.append(row)

    return rows


def _parse_questions_json(questions_json: str) -> list[dict[str, Any]]:
    """Parse a JSON question payload into a normalized list."""
    raw_payload = questions_json.strip()
    if not raw_payload:
        return []

    if raw_payload.startswith("```"):
        fenced_match = re.match(
            r"^```(?:json|python|py|javascript|js)?\s*(?P<body>[\s\S]*?)\s*```$",
            raw_payload,
            re.IGNORECASE,
        )
        if fenced_match:
            raw_payload = fenced_match.group("body").strip()

    try:
        payload = json.loads(raw_payload)
    except json.JSONDecodeError as exc:
        try:
            payload = ast.literal_eval(raw_payload)
        except (ValueError, SyntaxError) as literal_exc:
            payload = _recover_question_payload(raw_payload)
            if payload is None:
                raise RuntimeError(
                    "questions_json must be valid JSON or a Python-style list/dict literal: "
                    f"{exc}"
                ) from literal_exc

    if isinstance(payload, dict):
        questions = payload.get("questions", [])
    else:
        questions = payload

    if not isinstance(questions, list):
        raise RuntimeError("questions_json must be a JSON array or an object with a 'questions' array.")

    normalized_questions: list[dict[str, Any]] = []
    for index, raw_question in enumerate(questions, start=1):
        if not isinstance(raw_question, dict):
            raise RuntimeError(f"Question {index} must be a JSON object.")
        normalized_questions.append(_normalize_question_dict(raw_question, index))

    return normalized_questions


def _normalize_question_dict(raw_question: dict[str, Any], index: int) -> dict[str, Any]:
    """Normalize one question dictionary into the internal shape."""
    title = str(raw_question.get("title", "") or "").strip()
    if not title:
        raise RuntimeError(f"Question {index} is missing a title.")

    question_type = str(
        raw_question.get("type", "multiple_choice") or "multiple_choice"
    ).strip().lower()
    required = bool(raw_question.get("required", True))
    options = raw_question.get("options", [])
    help_text = str(
        raw_question.get("description", "") or raw_question.get("help_text", "") or ""
    ).strip()
    images = raw_question.get("images", [])
    correct_answers = raw_question.get("correct_answers", [])
    point_value = raw_question.get("point_value", 1)

    normalized_correct_answers = (
        [
            str(answer).strip()
            for answer in correct_answers
            if str(answer).strip()
        ]
        if isinstance(correct_answers, list)
        else []
    )
    try:
        normalized_point_value = max(1, int(point_value or 1))
    except Exception:
        normalized_point_value = 1

    return {
        "title": title,
        "type": question_type,
        "required": required,
        "options": options if isinstance(options, list) else [],
        "description": help_text,
        "images": images if isinstance(images, list) else [],
        "correct_answers": normalized_correct_answers,
        "point_value": normalized_point_value,
    }


def _parse_questions_text(questions_text: str) -> list[dict[str, Any]]:
    """Parse a plain-text question specification into structured questions."""
    raw_text = questions_text.strip()
    if not raw_text:
        return []

    normalized_text = raw_text.replace("\r\n", "\n")
    blocks = re.split(r"\n\s*---+\s*\n|\n\s*\n", normalized_text)
    questions: list[dict[str, Any]] = []

    for block in blocks:
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if not lines:
            continue

        question: dict[str, Any] = {
            "type": "multiple_choice",
            "required": True,
            "options": [],
            "description": "",
        }

        for line in lines:
            lowered = line.lower()
            if lowered.startswith("question:"):
                question["title"] = line.split(":", 1)[1].strip()
                continue
            if lowered.startswith("title:"):
                question["title"] = line.split(":", 1)[1].strip()
                continue
            if lowered.startswith("type:"):
                question["type"] = line.split(":", 1)[1].strip().lower()
                continue
            if lowered.startswith("required:"):
                value = line.split(":", 1)[1].strip().lower()
                question["required"] = value not in {"false", "no", "0"}
                continue
            if lowered.startswith("description:") or lowered.startswith("help_text:"):
                question["description"] = line.split(":", 1)[1].strip()
                continue
            if lowered.startswith("option:"):
                option = line.split(":", 1)[1].strip()
                if option and not _is_placeholder_content(option):
                    question["options"].append(option)
                continue
            bullet_match = re.match(r"^((?:[-*]|\d+[.)]|[A-Za-z][.)]|[ก-ฮ][.)]))\s+(.+)$", line)
            if bullet_match:
                option = bullet_match.group(2).strip()
                if option and not _is_placeholder_content(option):
                    question["options"].append(option)
                continue
            if "title" not in question and not _is_instructional_prompt_line(line):
                question["title"] = line

        if "title" not in question or not str(question["title"]).strip():
            continue
        questions.append(_normalize_question_dict(question, len(questions) + 1))

    return questions


def _parse_questions_text_rich(questions_text: str) -> list[dict[str, Any]]:
    """Parse a human-readable markdown/plain-text question specification."""
    raw_text = questions_text.strip()
    if not raw_text:
        return []

    normalized_text = raw_text.replace("\r\n", "\n")
    heading_blocks = re.split(
        r"\n(?=#{1,6}\s*(?:question|q(?:uestion)?\s*\d+))",
        normalized_text,
        flags=re.IGNORECASE,
    )
    blocks = (
        heading_blocks
        if len(heading_blocks) > 1
        else re.split(r"\n\s*---+\s*\n|\n\s*\n", normalized_text)
    )
    questions: list[dict[str, Any]] = []

    for block in blocks:
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if not lines:
            continue

        question: dict[str, Any] = {
            "type": "multiple_choice",
            "required": True,
            "options": [],
            "description": "",
        }
        collecting_options = False

        for line in lines:
            cleaned_line = re.sub(r"^#{1,6}\s*", "", line).strip()
            if re.match(
                r"^(?:question|q(?:uestion)?\s*\d+)\b",
                cleaned_line,
                re.IGNORECASE,
            ):
                title_after_colon = cleaned_line.split(":", 1)
                if len(title_after_colon) == 2 and title_after_colon[1].strip():
                    question["title"] = title_after_colon[1].strip()
                collecting_options = False
                continue

            normalized_line = re.sub(r"^[-*]\s*", "", cleaned_line).strip()
            lowered = normalized_line.lower()

            if lowered.startswith("question:"):
                title_value = normalized_line.split(":", 1)[1].strip()
                if title_value and not _is_instructional_prompt_line(title_value):
                    question["title"] = title_value
                collecting_options = False
                continue
            if lowered.startswith("title:"):
                title_value = normalized_line.split(":", 1)[1].strip()
                if title_value and not _is_instructional_prompt_line(title_value):
                    question["title"] = title_value
                collecting_options = False
                continue
            if lowered.startswith("type:"):
                question["type"] = normalized_line.split(":", 1)[1].strip().lower()
                collecting_options = False
                continue
            if lowered.startswith("required:"):
                value = normalized_line.split(":", 1)[1].strip().lower()
                question["required"] = value not in {"false", "no", "0"}
                collecting_options = False
                continue
            if lowered.startswith("description:") or lowered.startswith("help_text:"):
                description_value = normalized_line.split(":", 1)[1].strip()
                if description_value and not _is_instructional_prompt_line(description_value):
                    question["description"] = description_value
                collecting_options = False
                continue
            if lowered.startswith("option:"):
                option = normalized_line.split(":", 1)[1].strip()
                if option and not _is_placeholder_content(option):
                    question["options"].append(option)
                collecting_options = False
                continue
            if lowered.startswith("options:"):
                collecting_options = True
                continue

            bullet_match = re.match(
                r"^((?:[-*]|\d+[.)]|[A-Za-z][.)]|[\u0E01-\u0E2E][.)]))\s+(.+)$",
                cleaned_line,
            )
            if bullet_match:
                option = bullet_match.group(2).strip()
                if option and collecting_options and not _is_placeholder_content(option):
                    question["options"].append(option)
                    continue
                if option and "title" not in question and not _is_instructional_prompt_line(option):
                    question["title"] = option
                    continue

            if collecting_options:
                option = normalized_line.strip()
                if option and not _is_placeholder_content(option):
                    question["options"].append(option)
                    continue

            if "title" not in question and not _is_instructional_prompt_line(normalized_line):
                question["title"] = normalized_line

        if "title" not in question or not str(question["title"]).strip():
            continue
        questions.append(_normalize_question_dict(question, len(questions) + 1))

    return questions


def extract_questions_from_reference_text(reference_text: str) -> list[dict[str, Any]]:
    """Extract concrete questions from uploaded reference text when possible."""
    def infer_choice_count_from_reference(text: str) -> int | None:
        thai_choice_markers = ("ก.", "ข.", "ค.", "ง.")
        if all(marker in text for marker in thai_choice_markers):
            return 4
        english_choice_markers = ("A.", "B.", "C.", "D.")
        if all(marker in text for marker in english_choice_markers):
            return 4
        return None

    inferred_choice_count = infer_choice_count_from_reference(reference_text)

    def split_inline_choice_line(text: str) -> list[str]:
        segments = re.split(
            r"(?=(?:[A-Da-d]|[\u0E01-\u0E2E])[.)]\s*)",
            text.strip(),
        )
        options: list[str] = []
        for segment in segments:
            normalized = segment.strip()
            if not normalized:
                continue
            match = re.match(r"^(?:[A-Da-d]|[\u0E01-\u0E2E])[.)]\s*(.*)$", normalized)
            if not match:
                continue
            option_text = match.group(1).strip()
            options.append(option_text)
        return options

    def is_valid_extracted_question(question: dict[str, Any]) -> bool:
        title = str(question.get("title", "") or "").strip()
        if not title:
            return False

        lowered_title = title.casefold()
        if lowered_title in {
            "แบบทดสอบก่อนเรียน",
            "แบบทดสอบก่อนการอบรม",
            "แบบทดสอบหลังเรียน",
            "แบบทดสอบหลังการอบรม",
            "pre-test",
            "post-test",
            "participant information",
            "respondent information",
        }:
            return False

        question_type = str(question.get("type", "multiple_choice") or "multiple_choice").strip().lower()
        options = [
            str(option).strip()
            for option in question.get("options", [])
            if str(option).strip()
        ]
        if question_type in {"text", "short_answer"}:
            return True
        if question_type in {"multiple_choice", "multiple-choice", "radio", "checkbox", "checkboxes", "dropdown", "drop_down"}:
            return len(options) >= 1
        return True

    def normalize_extracted_questions(questions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for question in questions:
            normalized_question = dict(question)
            question_type = str(normalized_question.get("type", "multiple_choice") or "multiple_choice").strip().lower()
            correct_answers = [
                str(answer).strip()
                for answer in normalized_question.get("correct_answers", [])
                if str(answer).strip()
            ] if isinstance(normalized_question.get("correct_answers", []), list) else []
            options = [
                str(option).strip()
                for option in normalized_question.get("options", [])
                if str(option).strip()
            ]
            expanded_options: list[str] = []
            for option in options:
                inline_split = split_inline_choice_line(option)
                if len(inline_split) >= 2:
                    expanded_options.extend(inline_split)
                else:
                    expanded_options.append(option)
            options = expanded_options
            if inferred_choice_count and question_type in {
                "multiple_choice",
                "multiple-choice",
                "radio",
            }:
                options = options[:inferred_choice_count]
            if correct_answers:
                normalized_option_map = {
                    _normalize_match_text(option): option
                    for option in options
                    if str(option).strip()
                }
                normalized_question["correct_answers"] = [
                    normalized_option_map.get(_normalize_match_text(answer), answer)
                    for answer in correct_answers
                    if _normalize_match_text(answer) in normalized_option_map
                ]
            if question_type in {
                "multiple_choice",
                "multiple-choice",
                "radio",
                "checkbox",
                "checkboxes",
                "dropdown",
                "drop_down",
            } and not options:
                normalized_question["type"] = "text"
            elif question_type in {
                "multiple_choice",
                "multiple-choice",
                "radio",
            } and len(options) == 1:
                normalized_question["type"] = "text"
            normalized_question["options"] = options
            if is_valid_extracted_question(normalized_question):
                normalized.append(normalized_question)
        return normalized

    def parse_thai_exam_questions(text: str) -> list[dict[str, Any]]:
        lines = [line.rstrip() for line in text.replace("\r\n", "\n").splitlines()]
        questions: list[dict[str, Any]] = []
        current_question: dict[str, Any] | None = None

        question_start_re = re.compile(r"^\s*(\d+)[.)]\s+(.+)$")
        option_start_re = re.compile(r"^\s*([A-Da-d]|[\u0E01-\u0E2E])[.)]\s*(.*)$")
        answer_blank_re = re.compile(
            r"^\s*(?:[._\-]{5,}|[.â€¦Â·â€¢\s]{8,})\s*$"
        )

        def is_skippable_line(text: str) -> bool:
            if not text:
                return True
            if text in {"[Table]"} or text.startswith("[Embedded image"):
                return True
            if _is_visual_separator_line(text):
                return True
            return _is_instructional_prompt_line(text)

        def next_meaningful_lines(start_index: int, limit: int = 8) -> list[str]:
            meaningful: list[str] = []
            for candidate in lines[start_index : start_index + limit]:
                stripped_candidate = candidate.strip()
                if is_skippable_line(stripped_candidate):
                    continue
                meaningful.append(stripped_candidate)
            return meaningful

        def looks_like_question_sublist(start_index: int) -> bool:
            lookahead = next_meaningful_lines(start_index)
            numbered_prefix_count = 0
            option_marker_found = False
            for candidate in lookahead:
                if option_start_re.match(candidate):
                    option_marker_found = True
                    break
                numbered_match = question_start_re.match(candidate)
                if numbered_match and int(numbered_match.group(1)) <= 5:
                    numbered_prefix_count += 1
                    continue
                break
            return numbered_prefix_count >= 2 and option_marker_found

        def begins_numbered_subitem_sequence(start_index: int) -> bool:
            lookahead = next_meaningful_lines(start_index)
            numbered_prefix_count = 0
            option_marker_found = False
            for candidate in lookahead:
                if option_start_re.match(candidate):
                    option_marker_found = True
                    break
                numbered_match = question_start_re.match(candidate)
                if numbered_match and int(numbered_match.group(1)) <= 5:
                    numbered_prefix_count += 1
                    continue
                break
            return numbered_prefix_count >= 1 and option_marker_found

        def looks_like_question_title(start_index: int) -> bool:
            lookahead = next_meaningful_lines(start_index)
            if not lookahead:
                return False
            first = lookahead[0]
            if option_start_re.match(first):
                return False
            if answer_blank_re.match(first):
                return False

            option_marker_count = 0
            numbered_subitem_count = 0
            for candidate in lookahead[1:]:
                if option_start_re.match(candidate):
                    option_marker_count += 1
                    if option_marker_count >= 2:
                        return True
                    if numbered_subitem_count >= 2:
                        return True
                    continue
                if answer_blank_re.match(candidate):
                    return True
                numbered_match = question_start_re.match(candidate)
                if numbered_match and int(numbered_match.group(1)) <= 5:
                    numbered_subitem_count += 1
                    continue
                break
            return False

        def flush_current() -> None:
            nonlocal current_question
            if current_question and is_valid_extracted_question(current_question):
                questions.append(
                    _normalize_question_dict(current_question, len(questions) + 1)
                )
            current_question = None

        for line_index, raw_line in enumerate(lines):
            stripped = raw_line.strip()
            if is_skippable_line(stripped):
                continue

            question_match = question_start_re.match(stripped)
            if question_match:
                if (
                    current_question is not None
                    and not current_question.get("options")
                    and int(question_match.group(1)) <= 5
                    and (
                        looks_like_question_sublist(line_index)
                        or bool(current_question.get("_collect_numbered_subitems"))
                    )
                ):
                    description = str(current_question.get("description", "") or "").strip()
                    addition = stripped
                    current_question["description"] = (
                        f"{description}\n{addition}".strip() if description else addition
                    )
                    current_question["_collect_numbered_subitems"] = True
                    continue
                flush_current()
                current_question = {
                    "title": question_match.group(2).strip(),
                    "type": "multiple_choice",
                    "required": True,
                    "options": [],
                    "description": "",
                    "_collect_numbered_subitems": begins_numbered_subitem_sequence(
                        line_index
                    ),
                }
                continue

            if current_question is None:
                lookahead_after_current = next_meaningful_lines(line_index + 1, limit=4)
                if lookahead_after_current and question_start_re.match(lookahead_after_current[0]):
                    continue
                if looks_like_question_title(line_index):
                    current_question = {
                        "title": stripped,
                        "type": "multiple_choice",
                        "required": True,
                        "options": [],
                        "description": "",
                        "_collect_numbered_subitems": begins_numbered_subitem_sequence(
                            line_index + 1
                        ),
                    }
                continue

            option_match = option_start_re.match(stripped)
            if option_match:
                option_label = option_match.group(1).strip()
                option_text = option_match.group(2).strip()
                if option_text and not _is_placeholder_content(option_text):
                    inline_split = split_inline_choice_line(stripped)
                    if len(inline_split) >= 2:
                        current_question["options"].extend(
                            option for option in inline_split if option and not _is_placeholder_content(option)
                        )
                    else:
                        current_question["options"].append(option_text)
                else:
                    current_question["options"].append(f"ตัวเลือก {option_label} (จากภาพ)")
                continue

            if answer_blank_re.match(stripped):
                current_question["type"] = "text"
                continue

            if current_question.get("options"):
                flush_current()
                if looks_like_question_title(line_index):
                    current_question = {
                        "title": stripped,
                        "type": "multiple_choice",
                        "required": True,
                        "options": [],
                        "description": "",
                        "_collect_numbered_subitems": begins_numbered_subitem_sequence(
                            line_index + 1
                        ),
                    }
                continue

            if looks_like_question_title(line_index):
                flush_current()
                current_question = {
                    "title": stripped,
                    "type": "multiple_choice",
                    "required": True,
                    "options": [],
                    "description": "",
                    "_collect_numbered_subitems": begins_numbered_subitem_sequence(
                        line_index + 1
                    ),
                }
                continue

            if not current_question.get("options"):
                description = str(current_question.get("description", "") or "").strip()
                current_question["description"] = (
                    f"{description}\n{stripped}".strip() if description else stripped
                )
                continue

            title = str(current_question.get("title", "") or "").strip()
            current_question["title"] = f"{title} {stripped}".strip()

        flush_current()
        return questions

    text = strip_embedded_image_blocks(clean_extracted_file_text(reference_text))
    if not text:
        return []

    thai_exam_parsed = parse_thai_exam_questions(text)
    if thai_exam_parsed:
        return normalize_extracted_questions(thai_exam_parsed)

    parsed = _parse_questions_text_rich(text)
    if parsed:
        return normalize_extracted_questions(parsed)

    lines = [line.rstrip() for line in text.replace("\r\n", "\n").splitlines()]
    questions: list[dict[str, Any]] = []
    current_question: dict[str, Any] | None = None

    question_start_re = re.compile(r"^\s*(\d+)[.)]\s+(.+)$")
    option_re = re.compile(
        r"^\s*(?:[-*]|\(?[A-Da-d]\)?[.)]|[\u0E01-\u0E2E][.)]|\d+[.)])\s+(.+)$"
    )

    for raw_line in lines:
        stripped = raw_line.strip()
        if not stripped or _is_instructional_prompt_line(stripped):
            continue

        question_match = question_start_re.match(stripped)
        if question_match:
            candidate_title = question_match.group(2).strip()
            if current_question and str(current_question.get("title", "")).strip():
                questions.append(
                    _normalize_question_dict(current_question, len(questions) + 1)
                )
            current_question = {
                "title": candidate_title,
                "type": "multiple_choice",
                "required": True,
                "options": [],
                "description": "",
            }
            continue

        if current_question is None:
            continue

        option_match = option_re.match(stripped)
        if option_match:
            option = option_match.group(1).strip()
            if option and not _is_placeholder_content(option):
                current_question.setdefault("options", []).append(option)
            continue

        if not current_question.get("description") and stripped:
            current_question["description"] = stripped

    if current_question and str(current_question.get("title", "")).strip():
        questions.append(_normalize_question_dict(current_question, len(questions) + 1))

    return normalize_extracted_questions(questions)


def _parse_questions_input(questions_json: str, questions_text: str) -> list[dict[str, Any]]:
    """Parse questions from structured JSON first, then plain-text fallback."""
    if questions_json.strip():
        try:
            return _parse_questions_json(questions_json)
        except RuntimeError:
            fallback_text = questions_text.strip() or questions_json
            parsed_fallback = _parse_questions_text_rich(fallback_text)
            if parsed_fallback:
                return parsed_fallback
            raise
    return _parse_questions_text_rich(questions_text)


def _parse_respondent_questions_input(respondent_questions_json: str) -> list[dict[str, Any]]:
    """Parse structured respondent questions passed from the compressed prompt."""
    payload = respondent_questions_json.strip()
    if not payload:
        return []

    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        try:
            parsed = ast.literal_eval(payload)
        except (ValueError, SyntaxError) as exc:
            raise RuntimeError(
                f"respondent_questions_json must be valid JSON or Python-style list literal: {exc}"
            ) from exc

    if not isinstance(parsed, list):
        raise RuntimeError("respondent_questions_json must be a list of question objects.")

    return [
        _normalize_question_dict(question, index)
        for index, question in enumerate(parsed, start=1)
        if isinstance(question, dict)
    ]


def _parse_section_structure_input(section_structure_json: str) -> dict[str, dict[str, str]]:
    """Parse prompt-derived section metadata passed to the tool."""
    payload = section_structure_json.strip()
    if not payload:
        return {}

    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        try:
            parsed = ast.literal_eval(payload)
        except (ValueError, SyntaxError) as exc:
            raise RuntimeError(
                f"section_structure_json must be valid JSON or Python-style dict literal: {exc}"
            ) from exc

    if not isinstance(parsed, dict):
        raise RuntimeError("section_structure_json must be an object keyed by section names.")

    normalized: dict[str, dict[str, str]] = {}
    for key, value in parsed.items():
        if not isinstance(value, dict):
            continue
        title = str(value.get("title", "") or "").strip()
        description = str(value.get("description", "") or "").strip()
        if title or description:
            normalized[str(key)] = {
                "title": title,
                "description": description,
            }
    return normalized


def _recover_question_payload(raw_payload: str) -> list[dict[str, Any]] | dict[str, Any] | None:
    """Best-effort recovery for malformed question payloads from local models."""
    extracted_objects = _extract_braced_objects(raw_payload)
    recovered_questions: list[dict[str, Any]] = []
    for chunk in extracted_objects:
        parsed = _parse_single_question_object(chunk)
        if isinstance(parsed, dict):
            recovered_questions.append(parsed)

    if recovered_questions:
        return recovered_questions
    return None


def _extract_braced_objects(text: str) -> list[str]:
    """Extract top-level {...} objects from a malformed list-like payload."""
    objects: list[str] = []
    depth = 0
    start_index: int | None = None
    in_string = False
    string_quote = ""
    escaping = False

    for index, char in enumerate(text):
        if in_string:
            if escaping:
                escaping = False
            elif char == "\\":
                escaping = True
            elif char == string_quote:
                in_string = False
            continue

        if char in {"'", '"'}:
            in_string = True
            string_quote = char
            continue

        if char == "{":
            if depth == 0:
                start_index = index
            depth += 1
            continue

        if char == "}":
            if depth == 0:
                continue
            depth -= 1
            if depth == 0 and start_index is not None:
                objects.append(text[start_index : index + 1])
                start_index = None

    return objects


def _parse_single_question_object(chunk: str) -> dict[str, Any] | None:
    """Parse a single recovered question object chunk."""
    try:
        parsed = json.loads(chunk)
    except json.JSONDecodeError:
        try:
            parsed = ast.literal_eval(chunk)
        except (ValueError, SyntaxError):
            return None

    if isinstance(parsed, dict):
        return parsed
    return None


def _build_form_item(question: dict[str, Any], include_grading: bool = True) -> dict[str, Any]:
    """Build a Google Forms item payload from a normalized question."""
    question_type = str(question.get("type", "multiple_choice") or "multiple_choice").strip().lower()
    required = bool(question.get("required", True))
    item: dict[str, Any] = {"title": _sanitize_display_text(question["title"])}
    description = _sanitize_multiline_display_text(question.get("description", ""))
    question_images = [
        image
        for image in question.get("images", [])
        if isinstance(image, dict) and str(image.get("source_uri", "") or "").strip()
    ]
    correct_answers = [
        _sanitize_display_text(answer)
        for answer in question.get("correct_answers", [])
        if _sanitize_display_text(answer)
    ] if isinstance(question.get("correct_answers", []), list) else []
    point_value = max(1, int(question.get("point_value", 1) or 1))
    if description:
        item["description"] = description
    if question_type in {"section", "section_break", "page_break"}:
        item["pageBreakItem"] = {}
        return item

    if question_type in {"multiple_choice", "multiple-choice", "radio"}:
        options_payload, answer_value_map = _build_unique_choice_options(
            question.get("options", [])
        )
        if len(options_payload) < 2:
            raise RuntimeError(f"Multiple-choice question '{question['title']}' needs at least 2 options.")
        question_payload: dict[str, Any] = {
            "required": required,
            "choiceQuestion": {
                "type": "RADIO",
                "options": options_payload,
            },
        }
        if include_grading and correct_answers:
            remapped_correct_answers = _remap_correct_answers_for_choice_values(
                correct_answers,
                answer_value_map,
            )
            if remapped_correct_answers:
                question_payload["grading"] = {
                    "pointValue": point_value,
                    "correctAnswers": {
                        "answers": [{"value": answer} for answer in remapped_correct_answers]
                    },
                }
        item["questionItem"] = {
            "question": question_payload
        }
        if question_images:
            first_image = question_images[0]
            item["questionItem"]["image"] = {
                "sourceUri": str(first_image.get("source_uri", "") or "").strip()
            }
            alt_text = str(first_image.get("alt_text", "") or "").strip()
            if alt_text:
                item["questionItem"]["image"]["altText"] = alt_text
        return item

    if question_type in {"checkbox", "checkboxes"}:
        options_payload, answer_value_map = _build_unique_choice_options(
            question.get("options", [])
        )
        if len(options_payload) < 2:
            raise RuntimeError(f"Checkbox question '{question['title']}' needs at least 2 options.")
        question_payload = {
            "required": required,
            "choiceQuestion": {
                "type": "CHECKBOX",
                "options": options_payload,
            },
        }
        if include_grading and correct_answers:
            remapped_correct_answers = _remap_correct_answers_for_choice_values(
                correct_answers,
                answer_value_map,
            )
            if remapped_correct_answers:
                question_payload["grading"] = {
                    "pointValue": point_value,
                    "correctAnswers": {
                        "answers": [{"value": answer} for answer in remapped_correct_answers]
                    },
                }
        item["questionItem"] = {
            "question": question_payload
        }
        if question_images:
            first_image = question_images[0]
            item["questionItem"]["image"] = {
                "sourceUri": str(first_image.get("source_uri", "") or "").strip()
            }
            alt_text = str(first_image.get("alt_text", "") or "").strip()
            if alt_text:
                item["questionItem"]["image"]["altText"] = alt_text
        return item

    if question_type in {"dropdown", "drop_down"}:
        options_payload, answer_value_map = _build_unique_choice_options(
            question.get("options", [])
        )
        if len(options_payload) < 2:
            raise RuntimeError(f"Dropdown question '{question['title']}' needs at least 2 options.")
        question_payload = {
            "required": required,
            "choiceQuestion": {
                "type": "DROP_DOWN",
                "options": options_payload,
            },
        }
        if include_grading and correct_answers:
            remapped_correct_answers = _remap_correct_answers_for_choice_values(
                correct_answers,
                answer_value_map,
            )
            if remapped_correct_answers:
                question_payload["grading"] = {
                    "pointValue": point_value,
                    "correctAnswers": {
                        "answers": [{"value": answer} for answer in remapped_correct_answers]
                    },
                }
        item["questionItem"] = {
            "question": question_payload
        }
        if question_images:
            first_image = question_images[0]
            item["questionItem"]["image"] = {
                "sourceUri": str(first_image.get("source_uri", "") or "").strip()
            }
            alt_text = str(first_image.get("alt_text", "") or "").strip()
            if alt_text:
                item["questionItem"]["image"]["altText"] = alt_text
        return item

    item["questionItem"] = {
        "question": {
            "required": required,
            "textQuestion": {},
        }
    }
    if question_images:
        first_image = question_images[0]
        item["questionItem"]["image"] = {
            "sourceUri": str(first_image.get("source_uri", "") or "").strip()
        }
        alt_text = str(first_image.get("alt_text", "") or "").strip()
        if alt_text:
            item["questionItem"]["image"]["altText"] = alt_text
    return item


def _build_form_items(question: dict[str, Any], include_grading: bool = True) -> list[dict[str, Any]]:
    """Build one or more Google Forms item payloads from a normalized question."""
    images = [
        image
        for image in question.get("images", [])
        if isinstance(image, dict) and str(image.get("source_uri", "") or "").strip()
    ]
    items: list[dict[str, Any]] = []
    extra_question_images = images[1:] if images else []
    for image in extra_question_images:
        image_item: dict[str, Any] = {
            "title": _sanitize_display_text(image.get("alt_text", ""))
            or _sanitize_display_text(f"Image for {str(question.get('title', '') or '').strip()}"),
            "imageItem": {
                "image": {
                    "sourceUri": str(image.get("source_uri", "") or "").strip(),
                }
            },
        }
        width = image.get("width")
        if isinstance(width, int) and 0 < width <= 740:
            image_item["imageItem"]["image"]["properties"] = {"width": width}
        alt_text = str(image.get("alt_text", "") or "").strip()
        if alt_text:
            image_item["imageItem"]["image"]["altText"] = alt_text
        items.append(image_item)

    for option_index, option in enumerate(question.get("options", [])):
        if not isinstance(option, dict):
            continue
        extra_images = option.get("extra_images", [])
        if not isinstance(extra_images, list):
            continue
        option_label = str(option.get("label", "") or _option_label_for_index(option_index)).strip()
        option_value = _sanitize_display_text(option.get("value", ""))
        for image in extra_images:
            if not isinstance(image, dict) or not str(image.get("source_uri", "") or "").strip():
                continue
            image_item: dict[str, Any] = {
                "title": _sanitize_display_text(image.get("alt_text", ""))
                or _sanitize_display_text(f"Additional image for choice {option_label}"),
                "description": _sanitize_display_text(f"Choice {option_label}: {option_value}"),
                "imageItem": {
                    "image": {
                        "sourceUri": str(image.get("source_uri", "") or "").strip(),
                    }
                },
            }
            width = image.get("width")
            if isinstance(width, int) and 0 < width <= 740:
                image_item["imageItem"]["image"]["properties"] = {"width": width}
            alt_text = str(image.get("alt_text", "") or "").strip()
            if alt_text:
                image_item["imageItem"]["image"]["altText"] = alt_text
            items.append(image_item)

    primary_item = _build_form_item(question, include_grading=include_grading)
    items.append(primary_item)
    return items


def _ensure_default_respondent_questions(
    title: str,
    description: str,
    questions: list[dict[str, Any]],
    respondent_questions: list[dict[str, Any]],
    section_structure: dict[str, dict[str, str]],
) -> list[dict[str, Any]]:
    """Ensure explicitly requested respondent questions stay first and in order."""
    if not respondent_questions:
        return questions

    respondent_titles = {
        str(question["title"]).strip().casefold(): question
        for question in respondent_questions
    }
    existing_by_title = {
        str(question.get("title", "") or "").strip().casefold(): question
        for question in questions
        if isinstance(question, dict)
    }

    resolved_respondent_questions = [
        existing_by_title.get(
            str(default_question["title"]).strip().casefold(),
            default_question,
        )
        for default_question in respondent_questions
    ]
    remaining_questions = [
        question
        for question in questions
        if str(question.get("title", "") or "").strip().casefold()
        not in respondent_titles
    ]
    merged_questions: list[dict[str, Any]] = []

    section_one_meta = section_structure.get("section_1", {})
    section_two_meta = section_structure.get("section_2", {})

    if section_one_meta.get("title"):
        merged_questions.append(
            {
                "title": section_one_meta["title"],
                "type": "section",
                "required": False,
                "description": section_one_meta.get("description", ""),
            }
        )
    merged_questions.extend(resolved_respondent_questions)
    if section_two_meta.get("title"):
        merged_questions.append(
            {
                "title": section_two_meta["title"],
                "type": "section",
                "required": False,
                "description": section_two_meta.get("description", ""),
            }
        )
    merged_questions.extend(remaining_questions)
    return merged_questions


def _apply_form_batch_updates(
    forms_service: Any,
    form_id: str,
    description: str,
    questions: list[dict[str, Any]],
    is_quiz: bool = False,
) -> None:
    """Apply form description and items in a single batchUpdate call."""
    requests: list[dict[str, Any]] = []
    if is_quiz:
        requests.append(
            {
                "updateSettings": {
                    "settings": {
                        "quizSettings": {
                            "isQuiz": True,
                        }
                    },
                    "updateMask": "quizSettings.isQuiz",
                }
            }
        )

    if description.strip():
        requests.append(
            {
                "updateFormInfo": {
                    "info": {"description": description.strip()},
                    "updateMask": "description",
                }
            }
        )

    item_index = 0
    for question in questions:
        for item in _build_form_items(question, include_grading=is_quiz):
            requests.append(
                {
                    "createItem": {
                        "item": item,
                        "location": {"index": item_index},
                    }
                }
            )
            item_index += 1

    if not requests:
        return

    requests = _sanitize_forms_payload(requests)

    forms_service.forms().batchUpdate(
        formId=form_id,
        body={"requests": requests},
    ).execute()


def _count_non_section_questions(questions: list[dict[str, Any]]) -> int:
    """Count actual form questions, excluding section/page breaks."""
    return sum(
        1
        for question in questions
        if str(question.get("type", "") or "").strip().lower()
        not in {"section", "section_break", "page_break"}
    )


def _normalize_header_cell(value: str, fallback_index: int) -> str:
    """Normalize a header cell while keeping it readable for analysis."""
    cleaned = re.sub(r"\s+", " ", str(value or "").strip())
    cleaned = cleaned.strip(":-")
    return cleaned or f"Column {fallback_index}"


def _dedupe_headers(headers: list[str]) -> list[str]:
    """Make header names unique while preserving order."""
    seen: dict[str, int] = {}
    deduped: list[str] = []
    for header in headers:
        count = seen.get(header, 0) + 1
        seen[header] = count
        deduped.append(header if count == 1 else f"{header} ({count})")
    return deduped


def _is_effectively_blank_row(row: list[Any]) -> bool:
    """Return whether a row has no meaningful values."""
    return not any(str(cell or "").strip() for cell in row)


def _guess_header_row_index(rows: list[list[Any]]) -> int:
    """Choose the likeliest header row from raw sheet values."""
    for index, row in enumerate(rows[:10]):
        non_empty = [str(cell or "").strip() for cell in row if str(cell or "").strip()]
        if len(non_empty) >= 2:
            return index
    return 0


def _build_analysis_ready_table(rows: list[list[Any]]) -> tuple[list[str], list[list[str]], int]:
    """Convert a raw response tab into normalized headers and cleaned rows."""
    if not rows:
        return [], [], 0

    header_row_index = _guess_header_row_index(rows)
    raw_headers = rows[header_row_index] if header_row_index < len(rows) else []
    normalized_headers = _dedupe_headers(
        [
            _normalize_header_cell(value, index + 1)
            for index, value in enumerate(raw_headers)
        ]
    )
    width = len(normalized_headers)
    cleaned_rows: list[list[str]] = []
    for row in rows[header_row_index + 1 :]:
        normalized_row = [str(cell or "").strip() for cell in row[:width]]
        if len(normalized_row) < width:
            normalized_row.extend([""] * (width - len(normalized_row)))
        if _is_effectively_blank_row(normalized_row):
            continue
        cleaned_rows.append(normalized_row)

    return normalized_headers, cleaned_rows, header_row_index


def _contains_thai(text: str) -> bool:
    return bool(re.search(r"[\u0E00-\u0E7F]", text or ""))


def infer_user_language(text: str) -> str:
    """Infer whether the user's latest message is primarily Thai or English."""
    if _contains_thai(text):
        return "th"
    return "en"


def _sanitize_sheet_title(title: str, fallback: str) -> str:
    """Sanitize a generated sheet title to comply with Google Sheets limits."""
    cleaned = re.sub(r"[:\\/?*\[\]]", " ", str(title or "")).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    if not cleaned:
        cleaned = fallback
    return cleaned[:100].strip()


def _looks_like_generated_analysis_sheet(title: str) -> bool:
    lowered = str(title or "").casefold()
    markers = (
        "processed responses",
        "analysis ready",
        "response details",
        "question summary",
        "analysis summary",
        "คำตอบที่จัดรูปแบบ",
        "รายละเอียดคำตอบ",
        "สรุปคำตอบ",
        "สรุปรายข้อ",
    )
    return any(marker in lowered for marker in markers)


def _derive_analysis_sheet_names(
    source_title: str,
    headers: list[str],
    requested_output_sheet_name: str,
) -> tuple[str, str]:
    """Derive user-friendly sheet names from the source tab and detected language."""
    if requested_output_sheet_name.strip():
        detailed_name = _sanitize_sheet_title(
            requested_output_sheet_name.strip(),
            "Response Details",
        )
        summary_name = _sanitize_sheet_title(
            (
                f"{detailed_name} - สรุปคำตอบรายข้อ"
                if _contains_thai(detailed_name)
                else f"{detailed_name} - Question Summary"
            ),
            "Question Summary" if not _contains_thai(detailed_name) else "สรุปคำตอบรายข้อ",
        )
        return detailed_name, summary_name

    language_seed = " ".join([source_title, *headers[:5]])
    is_thai = _contains_thai(language_seed)
    base_title = source_title.strip() or ("คำตอบแบบฟอร์ม" if is_thai else "Form Responses")
    if is_thai:
        detailed_name = _sanitize_sheet_title(
            f"{base_title} - รายละเอียดคำตอบ",
            "รายละเอียดคำตอบ",
        )
        summary_name = _sanitize_sheet_title(
            f"{base_title} - สรุปคำตอบรายข้อ",
            "สรุปคำตอบรายข้อ",
        )
    else:
        detailed_name = _sanitize_sheet_title(
            f"{base_title} - Response Details",
            "Response Details",
        )
        summary_name = _sanitize_sheet_title(
            f"{base_title} - Question Summary",
            "Question Summary",
        )
    return detailed_name, summary_name


def _derive_postprocess_sheet_names(
    source_title: str,
    headers: list[str],
    requested_output_sheet_name: str,
) -> tuple[str, str, str]:
    """Derive sheet names for cleaned raw responses, long-form details, and summary."""
    language_seed = " ".join([source_title, *headers[:5]])
    is_thai = _contains_thai(language_seed)
    base_title = source_title.strip() or ("คำตอบแบบฟอร์ม" if is_thai else "Form Responses")

    if requested_output_sheet_name.strip():
        processed_name = _sanitize_sheet_title(
            requested_output_sheet_name.strip(),
            "Processed Responses" if not is_thai else "คำตอบที่จัดรูปแบบ",
        )
    else:
        processed_name = _sanitize_sheet_title(
            (
                f"{base_title} - คำตอบที่จัดรูปแบบ"
                if is_thai
                else f"{base_title} - Processed Responses"
            ),
            "Processed Responses" if not is_thai else "คำตอบที่จัดรูปแบบ",
        )

    detail_name = _sanitize_sheet_title(
        (
            f"{base_title} - รายละเอียดคำตอบ"
            if is_thai
            else f"{base_title} - Response Details"
        ),
        "Response Details" if not is_thai else "รายละเอียดคำตอบ",
    )
    summary_name = _sanitize_sheet_title(
        (
            f"{base_title} - สรุปคำตอบรายข้อ"
            if is_thai
            else f"{base_title} - Question Summary"
        ),
        "Question Summary" if not is_thai else "สรุปคำตอบรายข้อ",
    )
    return processed_name, detail_name, summary_name


def _is_timestamp_header(header: str) -> bool:
    lowered = header.strip().casefold()
    return any(
        keyword in lowered
        for keyword in (
            "timestamp",
            "submitted at",
            "submit time",
            "date",
            "time",
            "วันที่",
            "เวลา",
            "วันเวลา",
        )
    )


def _is_metadata_header(header: str) -> bool:
    lowered = header.strip().casefold()
    respondent_titles = {
        str(question.get("title", "") or "").strip().casefold()
        for question in DEFAULT_RESPONDENT_INFO_QUESTIONS
    }
    if lowered in respondent_titles:
        return True
    return any(
        keyword in lowered
        for keyword in (
            "name",
            "email",
            "phone",
            "department",
            "organization",
            "position",
            "province",
            "school",
            "unit",
            "agency",
            "respondent",
            "participant",
            "ชื่อ",
            "อีเมล",
            "เบอร์",
            "โทร",
            "หน่วยงาน",
            "สถานศึกษา",
            "ตำแหน่ง",
            "จังหวัด",
            "ผู้เข้าอบรม",
            "ผู้เข้ารับการอบรม",
            "รุ่น",
            "cohort",
            "batch",
            "class",
            "session",
            "คะแนน",
            "score",
            "result",
            "total score",
        )
    )


def _classify_analysis_columns(headers: list[str]) -> tuple[list[int], list[int], int | None]:
    """Split cleaned headers into metadata columns and question columns."""
    metadata_indices: list[int] = []
    question_indices: list[int] = []
    timestamp_index: int | None = None

    for index, header in enumerate(headers):
        if _is_timestamp_header(header):
            timestamp_index = index
            continue
        if _is_metadata_header(header):
            metadata_indices.append(index)
        else:
            question_indices.append(index)

    if not question_indices:
        for index, _header in enumerate(headers):
            if index == timestamp_index or index in metadata_indices:
                continue
            question_indices.append(index)

    return metadata_indices, question_indices, timestamp_index


def _build_normalized_analysis_rows(
    headers: list[str],
    cleaned_rows: list[list[str]],
) -> tuple[list[str], list[list[str]], list[list[str]]]:
    """Create long-form analysis rows plus question summary rows."""
    metadata_indices, question_indices, timestamp_index = _classify_analysis_columns(headers)
    metadata_headers = [headers[index] for index in metadata_indices]

    analysis_headers = [
        "Response ID",
        "Timestamp",
        *metadata_headers,
        "Question",
        "Answer",
        "Answer Type",
        "Answer Length",
    ]
    analysis_rows: list[list[str]] = []
    summary_counts: dict[tuple[str, str], int] = {}
    question_totals: dict[str, int] = {}

    for row_number, row in enumerate(cleaned_rows, start=1):
        timestamp_value = row[timestamp_index] if timestamp_index is not None and timestamp_index < len(row) else ""
        metadata_values = [
            row[index] if index < len(row) else ""
            for index in metadata_indices
        ]
        for question_index in question_indices:
            if question_index >= len(row):
                continue
            answer = str(row[question_index] or "").strip()
            if not answer:
                continue
            question = headers[question_index]
            answer_type = "MULTIPLE_CHOICE" if len(answer.split(",")) <= 4 and len(answer) <= 120 else "SHORT_ANSWER"
            analysis_rows.append(
                [
                    str(row_number),
                    timestamp_value,
                    *metadata_values,
                    question,
                    answer,
                    answer_type,
                    str(len(answer)),
                ]
            )
            summary_counts[(question, answer)] = summary_counts.get((question, answer), 0) + 1
            question_totals[question] = question_totals.get(question, 0) + 1

    summary_headers = ["Question", "Answer", "Count", "Percent"]
    summary_rows: list[list[str]] = []
    for (question, answer), count in sorted(
        summary_counts.items(),
        key=lambda item: (item[0][0].casefold(), -item[1], item[0][1].casefold()),
    ):
        total = max(question_totals.get(question, 0), 1)
        percent = round((count / total) * 100, 2)
        summary_rows.append([question, answer, str(count), f"{percent:.2f}%"])

    return analysis_headers, analysis_rows, summary_rows


def _build_processed_response_table(
    headers: list[str],
    cleaned_rows: list[list[str]],
) -> tuple[list[str], list[list[str]]]:
    """Build a more useful wide processed-response sheet with derived helper columns."""
    metadata_indices, question_indices, timestamp_index = _classify_analysis_columns(headers)
    prefer_thai = _contains_thai(" ".join(headers[: min(len(headers), 8)]))

    processed_headers: list[str] = [
        "รหัสคำตอบ" if prefer_thai else "Response ID",
    ]
    if timestamp_index is not None and timestamp_index < len(headers):
        processed_headers.append(headers[timestamp_index])
    processed_headers.extend(
        [
            "จำนวนข้อที่ตอบ" if prefer_thai else "Answered Questions",
            "อัตราการตอบ" if prefer_thai else "Completion Rate",
        ]
    )
    processed_headers.extend(headers[index] for index in metadata_indices)
    processed_headers.extend(headers[index] for index in question_indices)

    total_question_count = max(len(question_indices), 1)
    processed_rows: list[list[str]] = []
    for row_number, row in enumerate(cleaned_rows, start=1):
        answered_count = 0
        question_values: list[str] = []
        for index in question_indices:
            value = row[index] if index < len(row) else ""
            normalized_value = str(value or "").strip()
            question_values.append(normalized_value)
            if normalized_value:
                answered_count += 1

        completion_rate = round((answered_count / total_question_count) * 100, 1)
        processed_row: list[str] = [str(row_number)]
        if timestamp_index is not None and timestamp_index < len(headers):
            processed_row.append(row[timestamp_index] if timestamp_index < len(row) else "")
        processed_row.extend(
            [
                str(answered_count),
                f"{completion_rate:.1f}%",
            ]
        )
        processed_row.extend(
            row[index] if index < len(row) else ""
            for index in metadata_indices
        )
        processed_row.extend(question_values)
        processed_rows.append(processed_row)

    return processed_headers, processed_rows


def _ensure_sheet_exists(
    service: Any,
    spreadsheet_id: str,
    sheet_title: str,
    existing_sheets: list[dict[str, Any]],
) -> None:
    """Create the destination sheet when it does not already exist."""
    existing_titles = {
        str(sheet.get("properties", {}).get("title", "") or "")
        for sheet in existing_sheets
        if isinstance(sheet, dict)
    }
    if sheet_title in existing_titles:
        return

    service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={
            "requests": [
                {
                    "addSheet": {
                        "properties": {
                            "title": sheet_title,
                        }
                    }
                }
            ]
        },
    ).execute()


def _pick_postprocess_source_sheet(sheets: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Pick the most likely raw response sheet, even if it is currently empty."""
    fallback_sheet: dict[str, Any] | None = None
    for sheet in sheets:
        title = str(sheet.get("properties", {}).get("title", "") or "")
        if not title or _looks_like_generated_analysis_sheet(title):
            continue
        lowered = title.casefold()
        if any(
            marker in lowered
            for marker in (
                "form responses",
                "response",
                "responses",
                "คำตอบแบบฟอร์ม",
                "คำตอบ",
            )
        ):
            return sheet
        if fallback_sheet is None:
            fallback_sheet = sheet
    return fallback_sheet


def _initialize_empty_postprocess_tabs(
    service: Any,
    spreadsheet_id: str,
    spreadsheet_title: str,
    source_title: str,
    existing_sheets: list[dict[str, Any]],
    output_sheet_name: str,
) -> dict[str, Any]:
    """Create ready-to-use analysis tabs even when the linked response sheet is still empty."""
    prefer_thai = _contains_thai(source_title or spreadsheet_title)
    processed_sheet_name, detail_sheet_name, summary_sheet_name = _derive_postprocess_sheet_names(
        source_title or spreadsheet_title,
        [],
        output_sheet_name,
    )
    _ensure_sheet_exists(service, spreadsheet_id, processed_sheet_name, existing_sheets)
    _delete_sheet_if_exists(service, spreadsheet_id, detail_sheet_name, existing_sheets)
    _ensure_sheet_exists(service, spreadsheet_id, summary_sheet_name, existing_sheets)

    service.spreadsheets().values().clear(
        spreadsheetId=spreadsheet_id,
        range=_quote_sheet_title(processed_sheet_name),
        body={},
    ).execute()
    service.spreadsheets().values().clear(
        spreadsheetId=spreadsheet_id,
        range=_quote_sheet_title(summary_sheet_name),
        body={},
    ).execute()

    waiting_message = (
        "ยังไม่มีคำตอบในฟอร์ม ชีตนี้จะถูกเติมข้อมูลหลังมีการส่งคำตอบครั้งแรก"
        if prefer_thai
        else "No form responses yet. This sheet will populate after the first submission."
    )
    processed_headers = (
        ["รหัสคำตอบ", "จำนวนข้อที่ตอบ", "อัตราการตอบ"]
        if prefer_thai
        else ["Response ID", "Answered Questions", "Completion Rate"]
    )
    service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=f"{_quote_sheet_title(processed_sheet_name)}!A1",
        valueInputOption="RAW",
        body={"values": [processed_headers, [waiting_message, "", ""]]},
    ).execute()
    service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=f"{_quote_sheet_title(summary_sheet_name)}!A1",
        valueInputOption="RAW",
        body={"values": [["Question", "Answer", "Count", "Percent"]]},
    ).execute()

    return {
        "ok": True,
        "status": "formatted",
        "spreadsheetId": spreadsheet_id,
        "spreadsheetTitle": spreadsheet_title,
        "sourceSheet": source_title,
        "outputSheet": processed_sheet_name,
        "detailSheet": "",
        "summarySheet": summary_sheet_name,
        "headerRowIndex": 0,
        "columnCount": 0,
        "rowCountWritten": 0,
        "detailColumnCount": 0,
        "detailRowCountWritten": 0,
        "questionSummaryRowCount": 0,
        "rawHeaders": [],
        "processedHeaders": processed_headers,
        "analysisHeaders": [
            "Response ID",
            "Timestamp",
            "Question",
            "Answer",
            "Answer Type",
            "Answer Length",
        ],
        "note": waiting_message,
    }


def _delete_sheet_if_exists(
    service: Any,
    spreadsheet_id: str,
    sheet_title: str,
    existing_sheets: list[dict[str, Any]],
) -> None:
    """Delete the destination sheet when it already exists."""
    for sheet in existing_sheets:
        properties = sheet.get("properties", {}) if isinstance(sheet, dict) else {}
        if str(properties.get("title", "") or "") != sheet_title:
            continue
        sheet_id = properties.get("sheetId")
        if sheet_id is None:
            return
        service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={
                "requests": [
                    {
                        "deleteSheet": {
                            "sheetId": int(sheet_id),
                        }
                    }
                ]
            },
        ).execute()
        return


def _generate_missing_questions(
    title: str,
    description: str,
    existing_questions: list[dict[str, Any]],
    respondent_questions: list[dict[str, Any]],
    missing_count: int,
    source_prompt: str = "",
) -> list[dict[str, Any]]:
    """Generate any missing main questions using the configured chat model."""
    if missing_count <= 0:
        return []

    respondent_titles = {
        str(question.get("title", "") or "").strip().casefold()
        for question in respondent_questions
    }
    existing_main_titles = [
        str(question.get("title", "") or "").strip()
        for question in existing_questions
        if str(question.get("type", "") or "").strip().lower()
        not in {"section", "section_break", "page_break"}
        and str(question.get("title", "") or "").strip().casefold()
        not in respondent_titles
    ]

    model = build_chat_model()
    response = model.invoke(
        [
            SystemMessage(
                content=(
                    "You generate missing Google Form questions. "
                    "Return only question blocks in markdown. "
                    "Do not include explanations, notes, placeholders, or summaries. "
                    "Every question must be complete."
                )
            ),
            HumanMessage(
                content=(
                    (
                        f"Original user brief:\n{source_prompt.strip()}\n\n"
                        if source_prompt.strip()
                        else ""
                    )
                    +
                    f"Form title: {title.strip()}\n"
                    f"Form description:\n{description.strip()}\n\n"
                    f"Need exactly {missing_count} additional main questions.\n"
                    "Write them in Thai.\n"
                    "Every question must be multiple_choice.\n"
                    "Every question must be required.\n"
                    "Every question must have exactly 4 answer choices.\n"
                    "Do not repeat these existing main question titles:\n"
                    + "\n".join(f"- {question_title}" for question_title in existing_main_titles)
                    + "\n\n"
                    "Return only in this format:\n"
                    "### Question 1\n"
                    "- Title: ...\n"
                    "- Type: multiple_choice\n"
                    "- Required: true\n"
                    "- Options:\n"
                    "  - ...\n"
                    "  - ...\n"
                    "  - ...\n"
                    "  - ...\n\n"
                    "Continue until all requested questions are produced."
                )
            ),
        ]
    )

    generated_text = content_to_text(response.content).strip()
    generated_questions = _parse_questions_text_rich(generated_text)
    filtered_questions: list[dict[str, Any]] = []
    seen_titles = {title.casefold() for title in existing_main_titles}
    for question in generated_questions:
        question_title = str(question.get("title", "") or "").strip()
        if not question_title or question_title.casefold() in seen_titles:
            continue
        if str(question.get("type", "") or "").strip().lower() not in {
            "multiple_choice",
            "multiple-choice",
            "radio",
        }:
            continue
        options = [
            str(option).strip()
            for option in question.get("options", [])
            if str(option).strip()
        ]
        if len(options) < 4:
            continue
        normalized_question = dict(question)
        normalized_question["options"] = options[:4]
        filtered_questions.append(normalized_question)
        seen_titles.add(question_title.casefold())
        if len(filtered_questions) >= missing_count:
            break

    return filtered_questions


def _generate_full_question_set_from_brief(
    title: str,
    description: str,
    source_prompt: str,
    respondent_questions: list[dict[str, Any]],
    expected_question_count: int,
) -> list[dict[str, Any]]:
    """Generate a complete main-question set from the original prompt when partial content is unusable."""
    if expected_question_count <= 0:
        return []

    model = build_chat_model()
    respondent_titles = "\n".join(
        f"- {str(question.get('title', '')).strip()}"
        for question in respondent_questions
        if str(question.get("title", "")).strip()
    )
    response = model.invoke(
        [
            SystemMessage(
                content=(
                    "You generate complete Google Form quiz questions. "
                    "Return only question blocks in markdown. "
                    "Do not include explanations, notes, placeholders, or summaries."
                )
            ),
            HumanMessage(
                content=(
                    f"Original user brief:\n{source_prompt.strip() or title.strip()}\n\n"
                    f"Form title: {title.strip()}\n"
                    f"Form description:\n{description.strip()}\n\n"
                    f"Need exactly {expected_question_count} main questions.\n"
                    "Write them in Thai.\n"
                    "Every question must be multiple_choice.\n"
                    "Every question must be required.\n"
                    "Every question must have exactly 4 answer choices.\n"
                    "Do not include respondent information questions in this set.\n"
                    + (
                        f"Respondent information fields already handled separately:\n{respondent_titles}\n\n"
                        if respondent_titles
                        else ""
                    )
                    + "Return only in this format:\n"
                    "### Question 1\n"
                    "- Title: ...\n"
                    "- Type: multiple_choice\n"
                    "- Required: true\n"
                    "- Options:\n"
                    "  - ...\n"
                    "  - ...\n"
                    "  - ...\n"
                    "  - ...\n\n"
                    "Continue until all requested questions are produced."
                )
            ),
        ]
    )

    generated_text = content_to_text(response.content).strip()
    generated_questions = _parse_questions_text_rich(generated_text)
    main_questions: list[dict[str, Any]] = []
    seen_titles: set[str] = set()
    respondent_title_keys = {
        str(question.get("title", "") or "").strip().casefold()
        for question in respondent_questions
    }
    for question in generated_questions:
        question_title = str(question.get("title", "") or "").strip()
        if not question_title:
            continue
        lowered_title = question_title.casefold()
        if lowered_title in respondent_title_keys or lowered_title in seen_titles:
            continue
        options = [
            str(option).strip()
            for option in question.get("options", [])
            if str(option).strip()
        ]
        if len(options) < 4:
            continue
        normalized_question = dict(question)
        normalized_question["options"] = options[:4]
        main_questions.append(normalized_question)
        seen_titles.add(lowered_title)
        if len(main_questions) >= expected_question_count:
            break

    return main_questions


def _cleanup_created_workspace_files(file_ids: list[str]) -> None:
    """Best-effort cleanup for newly created Drive files when creation must abort."""
    valid_ids = [file_id.strip() for file_id in file_ids if isinstance(file_id, str) and file_id.strip()]
    if not valid_ids:
        return

    drive_service = _build_drive_service()
    for file_id in valid_ids:
        try:
            drive_service.files().delete(fileId=file_id).execute()
        except Exception:
            continue


def _raise_google_api_connectivity_error(exc: ServerNotFoundError) -> None:
    """Raise a friendlier error when the container cannot resolve Google API hosts."""
    message = str(exc)
    if "googleapis.com" in message:
        raise RuntimeError(
            "The backend container could not resolve a Google API hostname "
            f"({message}). This is usually a Docker DNS/network issue, not a form bug. "
            "Rebuild and restart with the updated docker-compose DNS settings, then retry."
        ) from exc
    raise RuntimeError(message) from exc


def _describe_apps_script_http_error(exc: HttpError) -> str:
    """Convert common Apps Script API failures into a short actionable message."""
    status = getattr(exc.resp, "status", None)
    details: Any = {}
    message = str(exc)
    activation_url = ""

    try:
        details = json.loads(exc.content.decode("utf-8"))
    except Exception:
        details = {}

    if isinstance(details, dict):
        error_payload = details.get("error", {})
        if isinstance(error_payload, dict):
            api_message = error_payload.get("message")
            if isinstance(api_message, str) and api_message.strip():
                message = api_message.strip()
            for item in error_payload.get("details", []) or []:
                if not isinstance(item, dict):
                    continue
                for link in item.get("links", []) or []:
                    if isinstance(link, dict) and isinstance(link.get("url"), str):
                        activation_url = link["url"].strip()
                        break
                if activation_url:
                    break

    if status == 403 and (
        "Apps Script API has not been used" in message
        or "SERVICE_DISABLED" in str(details)
        or "script.googleapis.com" in message
    ):
        guidance = (
            "Enable the Apps Script API for the Google Cloud project used by your OAuth client, "
            "wait a few minutes, then retry."
        )
        if activation_url:
            guidance += f" Open this URL: {activation_url}"
        return f"{message} {guidance}"

    if "Requested entity was not found" in message:
        return (
            "Requested entity was not found. This usually means the configured shared Apps "
            "Script API executable deployment is not accessible to the current user. Recreate "
            "the deployment as an API executable with Execution API access set to ANYONE, and "
            "attach the script project to the same standard Google Cloud project as the OAuth client."
        )

    if "insufficient authentication scopes" in message.lower():
        return (
            "Request had insufficient authentication scopes. Confirm the connected Google user "
            "granted the Apps Script scopes, enable the Apps Script API in the shared standard "
            "Google Cloud project, and recreate the shared API executable deployment if needed."
        )

    return message


def _is_retryable_google_http_error(exc: HttpError) -> bool:
    """Return whether a Google API HttpError is likely transient."""
    status = getattr(exc.resp, "status", None)
    return status in {429, 500, 502, 503, 504}


def _create_google_form_with_retry(
    forms_service: Any,
    form_body: dict[str, Any],
    *,
    attempts: int = 4,
    delay_seconds: float = 2.0,
) -> dict[str, Any]:
    """Create a Google Form with bounded retries for transient upstream failures."""
    last_exc: HttpError | None = None
    for attempt in range(attempts):
        try:
            return forms_service.forms().create(body=form_body).execute()
        except HttpError as exc:
            last_exc = exc
            if not _is_retryable_google_http_error(exc) or attempt == attempts - 1:
                raise
            time.sleep(delay_seconds * (attempt + 1))
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("Google Forms API form creation did not return a response.")


@tool
def create_form_with_response_sheet(
    title: str,
    description: str = "",
    questions_json: str = "",
    questions_text: str = "",
    respondent_questions_json: str = "",
    section_structure_json: str = "",
    expected_question_count: int = 0,
    source_prompt: str = "",
    strict_source_questions: bool = False,
    is_quiz: bool = False,
    google_oauth_session_key: str = "",
) -> str:
    """Create a Google Form, link a response spreadsheet, and optionally add description/questions."""
    token_session = GOOGLE_OAUTH_SESSION_KEY.set(
        _sanitize_google_oauth_session_key(google_oauth_session_key)
    )
    try:
        if not title.strip():
            raise RuntimeError("title is required")

        forms_service = _build_forms_service()
        normalized_source_prompt = source_prompt.strip()
        inferred_question_count = extract_question_count(normalized_source_prompt) if normalized_source_prompt else None
        effective_expected_question_count = expected_question_count or inferred_question_count or 0

        questions = _parse_questions_input(questions_json, questions_text)
        respondent_questions = _parse_respondent_questions_input(respondent_questions_json)
        if not respondent_questions and normalized_source_prompt:
            respondent_questions = extract_requested_respondent_questions(normalized_source_prompt)
        section_structure = _parse_section_structure_input(section_structure_json)
        if not section_structure and normalized_source_prompt:
            section_structure = extract_requested_section_structure(normalized_source_prompt)

        if strict_source_questions and questions:
            effective_expected_question_count = max(
                0,
                _count_non_section_questions(questions) - len(respondent_questions),
            )

        if (
            not strict_source_questions
            and
            effective_expected_question_count > 0
            and _count_non_section_questions(questions) - len(respondent_questions) < effective_expected_question_count
            and normalized_source_prompt
        ):
            regenerated_main_questions = _generate_full_question_set_from_brief(
                title=title,
                description=description,
                source_prompt=normalized_source_prompt,
                respondent_questions=respondent_questions,
                expected_question_count=effective_expected_question_count,
            )
            if regenerated_main_questions:
                questions = regenerated_main_questions

        questions = _ensure_default_respondent_questions(
            title,
            description,
            questions,
            respondent_questions,
            section_structure,
        )
        if not strict_source_questions and effective_expected_question_count > 0:
            actual_main_question_count = (
                _count_non_section_questions(questions) - len(respondent_questions)
            )
            if actual_main_question_count < effective_expected_question_count:
                missing_questions = _generate_missing_questions(
                    title=title,
                    description=description,
                    existing_questions=questions,
                    respondent_questions=respondent_questions,
                    missing_count=effective_expected_question_count - actual_main_question_count,
                    source_prompt=normalized_source_prompt,
                )
                questions.extend(missing_questions)
                actual_main_question_count = (
                    _count_non_section_questions(questions) - len(respondent_questions)
                )
                if actual_main_question_count < effective_expected_question_count:
                    raise RuntimeError(
                        "Generated form content is incomplete: "
                        f"expected {effective_expected_question_count} main questions, "
                        f"but only received {actual_main_question_count}. "
                        "Generate every requested question explicitly and do not use placeholders."
                    )
        form_body = {
            "info": {
                "title": title.strip(),
                "documentTitle": title.strip(),
            }
        }
        form_response = _create_google_form_with_retry(
            forms_service,
            form_body,
        )
        form_id = form_response.get("formId", "")
        if not form_id:
            raise RuntimeError("Google Forms API did not return a formId.")

        questions = _materialize_question_images(questions)
        image_placements = _build_apps_script_image_placements(questions)
        rest_questions = questions
        if image_placements:
            rest_questions = _strip_images_from_questions_for_rest(questions)

        _apply_form_batch_updates(
            forms_service=forms_service,
            form_id=form_id,
            description=description,
            questions=rest_questions,
            is_quiz=is_quiz,
        )

        image_insert_result: dict[str, Any] = {}
        if image_placements:
            image_insert_result = _insert_form_images_via_apps_script(
                form_id=form_id,
                placements=image_placements,
            )
            inserted_count = int(image_insert_result.get("createdCount", 0) or 0)
            image_insert_ok = bool(image_insert_result.get("ok"))
            if image_insert_ok and inserted_count < len(image_placements):
                image_insert_ok = False
                image_insert_result = {
                    **image_insert_result,
                    "ok": False,
                    "status": "images-partial",
                    "error": (
                        "Not all images were inserted. "
                        f"Expected {len(image_placements)} image placements but Apps Script reported {inserted_count} inserted."
                    ),
                    "guidance": "The form was created, but some inline images were skipped.",
                }
        image_insert_status = str(
            image_insert_result.get("status", "") or ("images-inserted" if not image_placements else "images-skipped")
        )
        image_insert_error = str(image_insert_result.get("error", "") or "").strip()
        image_insert_guidance = str(image_insert_result.get("guidance", "") or "").strip()

        spreadsheet_details = _create_response_spreadsheet(title.strip())
        spreadsheet_id = spreadsheet_details["spreadsheetId"]
        link_result = _link_form_to_sheet_natively(form_id=form_id, spreadsheet_id=spreadsheet_id)
        link_ok = bool(link_result.get("ok"))
        link_status = str(link_result.get("status", "") or ("linked" if link_ok else "link-failed"))
        link_error = str(link_result.get("error", "") or "").strip()
        link_guidance = str(link_result.get("guidance", "") or "").strip()
        destination_id = str(link_result.get("destinationId", "") or "").strip()
        linked_at = datetime.now(timezone.utc).isoformat() if link_ok else ""
        if link_ok and destination_id and destination_id != spreadsheet_id:
            link_ok = False
            link_status = "mismatch"
            link_error = (
                "The linked response sheet did not match the created spreadsheet. "
                f"Expected {spreadsheet_id} but Google reported {destination_id}."
            )
            link_guidance = (
                "Use the spreadsheet link below as the response destination reference, or "
                "configure the shared Apps Script linker runtime correctly and retry linking."
            )

        _upsert_form_sheet_link(
            form_id,
            {
                "spreadsheetId": spreadsheet_id,
                "spreadsheetTitle": spreadsheet_details["spreadsheetTitle"],
                "spreadsheetUrl": spreadsheet_details["spreadsheetUrl"],
                "formUrl": f"https://docs.google.com/forms/d/{form_id}/edit",
                "googleOauthSessionKey": GOOGLE_OAUTH_SESSION_KEY.get() or "",
                "linkStatus": link_status,
                "linkMode": str(link_result.get("mode", "") or "api-executable"),
                "linkedAt": linked_at,
                "linkError": link_error,
                "linkGuidance": link_guidance,
                "scriptId": str(link_result.get("scriptId", "") or ""),
                "deploymentId": str(link_result.get("deploymentId", "") or ""),
                "scriptUrl": str(link_result.get("scriptUrl", "") or ""),
            },
        )
        _upsert_agent_form_record(
            form_id,
            spreadsheet_id=spreadsheet_id,
            form_title=title.strip(),
            form_url=f"https://docs.google.com/forms/d/{form_id}/edit",
            responder_url=str(form_response.get("responderUri", "") or "").strip(),
            spreadsheet_url=spreadsheet_details["spreadsheetUrl"],
            google_oauth_session_key=GOOGLE_OAUTH_SESSION_KEY.get() or "",
        )
        response_store_result = _best_effort_sync_agent_form_responses(
            form_id,
            spreadsheet_id=spreadsheet_id,
            form_title=title.strip(),
            form_url=f"https://docs.google.com/forms/d/{form_id}/edit",
            responder_url=str(form_response.get("responderUri", "") or "").strip(),
            spreadsheet_url=spreadsheet_details["spreadsheetUrl"],
            google_oauth_session_key=GOOGLE_OAUTH_SESSION_KEY.get() or "",
        )

        if link_ok:
            postprocess_result = _postprocess_newly_linked_response_sheet(spreadsheet_id)
            if postprocess_result.get("ok"):
                _upsert_form_sheet_link(
                    form_id,
                    {
                        "postprocessStatus": "formatted",
                        "processedSheetName": str(postprocess_result.get("processedSheetName", "") or ""),
                        "analysisSheetName": str(postprocess_result.get("analysisSheetName", "") or ""),
                        "summarySheetName": str(postprocess_result.get("summarySheetName", "") or ""),
                        "postprocessedAt": datetime.now(timezone.utc).isoformat(),
                    },
                )
            else:
                _upsert_form_sheet_link(
                    form_id,
                    {
                        "postprocessStatus": str(postprocess_result.get("status", "") or "format-failed"),
                        "postprocessError": str(postprocess_result.get("error", "") or ""),
                    },
                )
        else:
            postprocess_result = {
                "ok": False,
                "status": "skipped-link-failed",
                "error": "Skipped because the response sheet could not be linked automatically.",
            }
            _upsert_form_sheet_link(
                form_id,
                {
                    "postprocessStatus": "skipped-link-failed",
                    "postprocessError": str(postprocess_result.get("error", "") or ""),
                },
            )

        responder_uri = form_response.get("responderUri", "")
        edit_uri = f"https://docs.google.com/forms/d/{form_id}/edit"

        return json.dumps(
            {
                "formId": form_id,
                "formUrl": edit_uri,
                "editUrl": edit_uri,
                "title": title.strip(),
                "description": description.strip(),
                "responderUri": responder_uri,
                "responseUrl": responder_uri,
                "questionCount": len(questions),
                "insertedImageCount": int(image_insert_result.get("createdCount", 0) or 0),
                "imageInsertStatus": image_insert_status,
                "imageInsertError": image_insert_error,
                "imageInsertGuidance": image_insert_guidance,
                "isQuiz": bool(is_quiz),
                "spreadsheetId": spreadsheet_id,
                "spreadsheetTitle": spreadsheet_details["spreadsheetTitle"],
                "spreadsheetUrl": spreadsheet_details["spreadsheetUrl"],
                "linkStatus": link_status,
                "linkError": link_error,
                "linkGuidance": link_guidance,
                "linkedAt": linked_at,
                "postprocessStatus": str(postprocess_result.get("status", "") or ""),
                "processedSheetName": str(postprocess_result.get("processedSheetName", "") or ""),
                "analysisSheetName": str(postprocess_result.get("analysisSheetName", "") or ""),
                "summarySheetName": str(postprocess_result.get("summarySheetName", "") or ""),
                "postprocessError": str(postprocess_result.get("error", "") or ""),
                "responseStoreStatus": str(response_store_result.get("status", "") or ""),
                "responseStoreError": str(response_store_result.get("error", "") or ""),
                "responseStoreBackend": str(response_store_result.get("store", "") or ""),
                "responseStoreResponseCount": int(response_store_result.get("responseCount", 0) or 0),
                "nextStep": (
                    "The form is already linked to a Google Spreadsheet. "
                    "If responses already exist, the analysis sheets were created automatically. "
                    "Send that spreadsheet link back any time you want the agent to inspect or reformat the data."
                    if link_ok
                    else "The Google Form and a response spreadsheet were created, but automatic native linking did not complete. "
                    "Use the returned links, then finish linking manually or reconfigure the shared Apps Script runtime before retrying automatic linking."
                ),
            },
            ensure_ascii=False,
            indent=2,
        )
    finally:
        GOOGLE_OAUTH_SESSION_KEY.reset(token_session)


@tool
def sync_form_responses_to_sheet(
    form_id: str = "",
    spreadsheet_id: str = "",
) -> str:
    """Legacy compatibility tool; syncing linked sheets has been removed."""
    raise RuntimeError(
        "Form-to-spreadsheet linking and syncing have been removed from this app."
    )


@tool
def format_response_sheet_for_analysis(
    spreadsheet_target: str,
    source_sheet_name: str = "",
    output_sheet_name: str = "",
) -> str:
    """Format a raw Google Form response sheet into normalized analysis tables."""
    target = spreadsheet_target.strip()
    if not target:
        raise RuntimeError("spreadsheet_target is required.")

    spreadsheet_id_value = extract_spreadsheet_id(target)
    response_store_result = _best_effort_sync_agent_form_responses_by_spreadsheet(
        spreadsheet_id_value
    )
    credentials = _load_google_workspace_credentials()
    service = build_google_api(
        "sheets",
        "v4",
        credentials=credentials,
        cache_discovery=False,
    )

    metadata = (
        service.spreadsheets()
        .get(
            spreadsheetId=spreadsheet_id_value,
            fields="properties.title,sheets.properties(sheetId,title,index,gridProperties)",
        )
        .execute()
    )
    spreadsheet_title = str(metadata.get("properties", {}).get("title", "") or "")
    sheets = metadata.get("sheets", []) or []

    selected_sheet: dict[str, Any] | None = None
    if source_sheet_name.strip():
        for sheet in sheets:
            if str(sheet.get("properties", {}).get("title", "") or "") == source_sheet_name.strip():
                selected_sheet = sheet
                break
        if selected_sheet is None:
            raise RuntimeError(f"Could not find source sheet named '{source_sheet_name.strip()}'.")
    else:
        best_rows: list[list[Any]] = []
        for sheet in sheets:
            title = str(sheet.get("properties", {}).get("title", "") or "")
            if not title or _looks_like_generated_analysis_sheet(title):
                continue
            values = (
                service.spreadsheets()
                .values()
                .get(spreadsheetId=spreadsheet_id_value, range=_quote_sheet_title(title))
                .execute()
                .get("values", [])
            )
            if not values:
                continue
            headers, cleaned_rows, _ = _build_analysis_ready_table(values)
            score = len(headers) * 10 + len(cleaned_rows)
            best_score = len(best_rows[0]) * 10 + max(len(best_rows) - 1, 0) if best_rows else -1
            if score > best_score:
                selected_sheet = sheet
                best_rows = values

        if selected_sheet is None:
            selected_sheet = _pick_postprocess_source_sheet(sheets)
            if selected_sheet is None:
                raise RuntimeError("No populated sheet was found to format.")

    source_title = str(selected_sheet.get("properties", {}).get("title", "") or "")
    raw_values = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=spreadsheet_id_value, range=_quote_sheet_title(source_title))
        .execute()
        .get("values", [])
    )
    if not raw_values:
        empty_payload = _initialize_empty_postprocess_tabs(
            service,
            spreadsheet_id_value,
            spreadsheet_title,
            source_title,
            sheets,
            output_sheet_name,
        )
        empty_payload.update(
            {
                "responseStoreStatus": str(response_store_result.get("status", "") or ""),
                "responseStoreError": str(response_store_result.get("error", "") or ""),
                "responseStoreBackend": str(response_store_result.get("store", "") or ""),
                "responseStoreResponseCount": int(response_store_result.get("responseCount", 0) or 0),
            }
        )
        return json.dumps(
            empty_payload,
            ensure_ascii=False,
            indent=2,
        )
    headers, cleaned_rows, header_row_index = _build_analysis_ready_table(raw_values)
    if not headers:
        empty_payload = _initialize_empty_postprocess_tabs(
            service,
            spreadsheet_id_value,
            spreadsheet_title,
            source_title,
            sheets,
            output_sheet_name,
        )
        empty_payload.update(
            {
                "responseStoreStatus": str(response_store_result.get("status", "") or ""),
                "responseStoreError": str(response_store_result.get("error", "") or ""),
                "responseStoreBackend": str(response_store_result.get("store", "") or ""),
                "responseStoreResponseCount": int(response_store_result.get("responseCount", 0) or 0),
            }
        )
        return json.dumps(
            empty_payload,
            ensure_ascii=False,
            indent=2,
        )
    analysis_headers, analysis_rows, summary_rows = _build_normalized_analysis_rows(
        headers,
        cleaned_rows,
    )
    processed_headers, processed_rows = _build_processed_response_table(
        headers,
        cleaned_rows,
    )

    processed_sheet_name, detail_sheet_name, summary_sheet_name = _derive_postprocess_sheet_names(
        source_title,
        headers,
        output_sheet_name,
    )
    _ensure_sheet_exists(service, spreadsheet_id_value, processed_sheet_name, sheets)
    _delete_sheet_if_exists(service, spreadsheet_id_value, detail_sheet_name, sheets)
    _ensure_sheet_exists(service, spreadsheet_id_value, summary_sheet_name, sheets)
    processed_range = f"{_quote_sheet_title(processed_sheet_name)}!A1"
    summary_range = f"{_quote_sheet_title(summary_sheet_name)}!A1"
    service.spreadsheets().values().clear(
        spreadsheetId=spreadsheet_id_value,
        range=_quote_sheet_title(processed_sheet_name),
        body={},
    ).execute()
    service.spreadsheets().values().clear(
        spreadsheetId=spreadsheet_id_value,
        range=_quote_sheet_title(summary_sheet_name),
        body={},
    ).execute()
    service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id_value,
        range=processed_range,
        valueInputOption="RAW",
        body={"values": [processed_headers, *processed_rows]},
    ).execute()
    service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id_value,
        range=summary_range,
        valueInputOption="RAW",
        body={
            "values": [
                ["Question", "Answer", "Count", "Percent"],
                *summary_rows,
            ]
        },
    ).execute()

    return json.dumps(
        {
            "spreadsheetId": spreadsheet_id_value,
            "spreadsheetTitle": spreadsheet_title,
            "sourceSheet": source_title,
            "outputSheet": processed_sheet_name,
            "detailSheet": "",
            "summarySheet": summary_sheet_name,
            "headerRowIndex": header_row_index + 1,
            "columnCount": len(processed_headers),
            "rowCountWritten": len(processed_rows),
            "detailColumnCount": 0,
            "detailRowCountWritten": 0,
            "questionSummaryRowCount": len(summary_rows),
            "rawHeaders": headers,
            "processedHeaders": processed_headers,
            "analysisHeaders": analysis_headers,
            "responseStoreStatus": str(response_store_result.get("status", "") or ""),
            "responseStoreError": str(response_store_result.get("error", "") or ""),
            "responseStoreBackend": str(response_store_result.get("store", "") or ""),
            "responseStoreResponseCount": int(response_store_result.get("responseCount", 0) or 0),
            "note": (
                "The raw response data was transformed into a wide processed-response sheet with helper columns, "
                "plus a separate question summary sheet."
            ),
        },
        ensure_ascii=False,
        indent=2,
    )


def _postprocess_newly_linked_response_sheet(
    spreadsheet_id: str,
    retries: int = 8,
    delay_seconds: float = 3.0,
) -> dict[str, Any]:
    """Best-effort formatting of a newly linked response sheet into analysis tabs."""
    last_error = ""
    for attempt in range(max(1, retries)):
        try:
            result = format_response_sheet_for_analysis.invoke(
                {"spreadsheet_target": spreadsheet_id}
            )
            payload = json.loads(result) if isinstance(result, str) else result
            if isinstance(payload, dict):
                raw_headers = payload.get("rawHeaders", [])
                column_count = int(payload.get("columnCount", 0) or 0)
                if (not raw_headers or column_count == 0) and attempt < max(1, retries) - 1:
                    time.sleep(delay_seconds)
                    continue
                payload["processedSheetName"] = str(payload.get("outputSheet", "") or "")
                payload["analysisSheetName"] = str(payload.get("detailSheet", "") or "")
                payload["ok"] = True
                payload["status"] = "formatted"
                payload["attempts"] = attempt + 1
                return payload
            return {
                "ok": True,
                "status": "formatted",
                "attempts": attempt + 1,
            }
        except Exception as exc:
            last_error = str(exc).strip()
            lowered = last_error.casefold()
            if (
                "no populated sheet was found" in lowered
                or "could not detect a usable header row" in lowered
            ):
                if attempt < max(1, retries) - 1:
                    time.sleep(delay_seconds)
                    continue
                return {
                    "ok": False,
                    "status": "waiting-for-responses",
                    "error": last_error,
                    "attempts": attempt + 1,
                }
            return {
                "ok": False,
                "status": "format-failed",
                "error": last_error,
                "attempts": attempt + 1,
            }

    return {
        "ok": False,
        "status": "format-failed",
        "error": last_error or "Unknown response-sheet post-processing failure.",
        "attempts": max(1, retries),
    }


def _coerce_summary_count(value: Any) -> int:
    text = str(value or "").strip().replace(",", "")
    if not text:
        return 0
    try:
        return int(float(text))
    except ValueError:
        return 0


def _coerce_summary_percent(value: Any) -> float:
    text = str(value or "").strip().replace("%", "").replace(",", "")
    if not text:
        return 0.0
    try:
        return float(text)
    except ValueError:
        return 0.0


def _load_summary_rows_from_sheet(
    service: Any,
    spreadsheet_id: str,
    summary_sheet_name: str,
) -> list[list[str]]:
    values = (
        service.spreadsheets()
        .values()
        .get(
            spreadsheetId=spreadsheet_id,
            range=_quote_sheet_title(summary_sheet_name),
        )
        .execute()
        .get("values", [])
    )
    if not values or len(values) <= 1:
        return []

    rows: list[list[str]] = []
    for row in values[1:]:
        padded = [str(cell or "").strip() for cell in row[:4]]
        while len(padded) < 4:
            padded.append("")
        if not padded[0] or not padded[1]:
            continue
        rows.append(padded[:4])
    return rows


def _load_processed_sheet_table(
    service: Any,
    spreadsheet_id: str,
    processed_sheet_name: str,
) -> tuple[list[str], list[list[str]]]:
    values = (
        service.spreadsheets()
        .values()
        .get(
            spreadsheetId=spreadsheet_id,
            range=_quote_sheet_title(processed_sheet_name),
        )
        .execute()
        .get("values", [])
    )
    if not values:
        return [], []
    headers = [str(cell or "").strip() for cell in values[0]]
    rows: list[list[str]] = []
    width = len(headers)
    for row in values[1:]:
        normalized = [str(cell or "").strip() for cell in row[:width]]
        if len(normalized) < width:
            normalized.extend([""] * (width - len(normalized)))
        if _is_effectively_blank_row(normalized):
            continue
        rows.append(normalized)
    return headers, rows


def _resolve_existing_postprocess_payload(
    service: Any,
    spreadsheet_id: str,
) -> dict[str, Any] | None:
    """Reuse existing post-process tabs when they already exist for this workbook."""
    metadata = (
        service.spreadsheets()
        .get(
            spreadsheetId=spreadsheet_id,
            fields="properties.title,sheets.properties(sheetId,title,index,gridProperties)",
        )
        .execute()
    )
    spreadsheet_title = str(metadata.get("properties", {}).get("title", "") or "")
    sheets = metadata.get("sheets", []) or []
    source_sheet = _pick_postprocess_source_sheet(sheets)
    if source_sheet is None:
        return None

    source_title = str(source_sheet.get("properties", {}).get("title", "") or "").strip()
    if not source_title:
        return None

    processed_sheet_name, _detail_sheet_name, summary_sheet_name = _derive_postprocess_sheet_names(
        source_title,
        [],
        "",
    )
    existing_titles = {
        str(sheet.get("properties", {}).get("title", "") or "").strip()
        for sheet in sheets
        if isinstance(sheet, dict)
    }
    if processed_sheet_name not in existing_titles or summary_sheet_name not in existing_titles:
        return None

    processed_headers, processed_rows = _load_processed_sheet_table(
        service,
        spreadsheet_id,
        processed_sheet_name,
    )
    summary_rows = _load_summary_rows_from_sheet(
        service,
        spreadsheet_id,
        summary_sheet_name,
    )
    return {
        "spreadsheetId": spreadsheet_id,
        "spreadsheetTitle": spreadsheet_title,
        "sourceSheet": source_title,
        "outputSheet": processed_sheet_name,
        "detailSheet": "",
        "summarySheet": summary_sheet_name,
        "headerRowIndex": 1 if processed_headers else 0,
        "columnCount": len(processed_headers),
        "rowCountWritten": len(processed_rows),
        "detailColumnCount": 0,
        "detailRowCountWritten": 0,
        "questionSummaryRowCount": len(summary_rows),
        "rawHeaders": [],
        "processedHeaders": processed_headers,
        "analysisHeaders": [],
        "note": "Reused existing processed-analysis tabs.",
        "reusedExistingPostprocess": True,
    }


def _fallback_chart_type_for_question(points: list[dict[str, Any]]) -> str:
    unique_answers = len(points)
    if unique_answers <= 1:
        return "bar"
    total = sum(int(point.get("value", 0) or 0) for point in points)
    top_share = (
        max((int(point.get("value", 0) or 0) for point in points), default=0) / total
        if total > 0
        else 0.0
    )
    average_label_length = (
        sum(len(str(point.get("label", "") or "")) for point in points) / unique_answers
        if unique_answers
        else 0.0
    )
    if unique_answers <= 4 and average_label_length <= 28 and top_share <= 0.9:
        return "pie"
    return "bar"


def _fallback_chart_reason(chart_type: str, points: list[dict[str, Any]]) -> str:
    unique_answers = len(points)
    if chart_type == "pie":
        return (
            f"Used a pie chart because this question has {unique_answers} compact categories with a part-to-whole breakdown."
        )
    return (
        f"Used a bar chart because this question has {unique_answers} answer categories or labels that are easier to compare on an axis."
    )


def _question_looks_like_low_value_dashboard_topic(question: str) -> bool:
    lowered = question.strip().casefold()
    return any(
        marker in lowered
        for marker in (
            "ชื่อ",
            "อีเมล",
            "เบอร์โทร",
            "หน่วยงาน",
            "timestamp",
            "ประทับเวลา",
            "ข้อใดต่อไปนี้ถูกต้อง",
            "ข้อใดไม่ใช่",
            "ตัวอย่าง",
            "คืออะไร",
            "what is",
            "which of the following",
        )
    )


def _metadata_header_is_segmentable(header: str) -> bool:
    lowered = header.strip().casefold()
    if not lowered:
        return False
    blocked_markers = (
        "ชื่อ",
        "name",
        "อีเมล",
        "email",
        "เบอร์",
        "phone",
        "โทร",
        "timestamp",
        "ประทับเวลา",
    )
    if any(marker in lowered for marker in blocked_markers):
        return False
    allowed_markers = (
        "หน่วยงาน",
        "department",
        "organization",
        "agency",
        "position",
        "ตำแหน่ง",
        "province",
        "จังหวัด",
        "school",
        "unit",
        "role",
        "group",
        "สถานศึกษา",
    )
    return any(marker in lowered for marker in allowed_markers)


def _request_prefers_raw_question_charts(analysis_request: str) -> bool:
    lowered = str(analysis_request or "").strip().casefold()
    return any(
        marker in lowered
        for marker in (
            "per question",
            "each question",
            "question by question",
            "รายข้อ",
            "ทีละข้อ",
            "แต่ละข้อ",
            "plot each question",
        )
    )


def _compute_answer_distribution_entropy(points: list[dict[str, Any]]) -> float:
    total = sum(max(int(point.get("value", 0) or 0), 0) for point in points)
    if total <= 0:
        return 0.0
    entropy = 0.0
    for point in points:
        value = max(int(point.get("value", 0) or 0), 0)
        if value <= 0:
            continue
        probability = value / total
        entropy -= probability * math.log2(probability)
    return entropy


def _choose_chart_specs_with_ai(
    grouped_questions: list[dict[str, Any]],
    analysis_request: str,
) -> list[dict[str, Any]]:
    if not grouped_questions:
        return []

    analysis_brief = analysis_request.strip().casefold()
    request_mentions_compare = any(
        keyword in analysis_brief
        for keyword in ("compare", "comparison", "เปรียบเทียบ", "เทียบ", "difference")
    )
    request_mentions_share = any(
        keyword in analysis_brief
        for keyword in ("share", "distribution", "percent", "สัดส่วน", "กระจาย", "เปอร์เซ็นต์")
    )

    scored_specs: list[dict[str, Any]] = []
    for item in grouped_questions:
        question = str(item.get("question", "") or "").strip()
        points = list(item.get("points", []))
        if not question or not points:
            continue

        unique_answers = len(points)
        total_answers = max(int(item.get("totalAnswers", 0) or 0), 0)
        dominant_share_percent = float(item.get("dominantSharePercent", 0.0) or 0.0)
        entropy = _compute_answer_distribution_entropy(points)
        long_label_count = sum(
            1 for point in points if len(str(point.get("label", "") or "")) > 40
        )
        average_label_length = (
            sum(len(str(point.get("label", "") or "")) for point in points) / unique_answers
            if unique_answers
            else 0.0
        )
        non_trivial_answers = sum(
            1 for point in points if float(point.get("percent", 0.0) or 0.0) >= 5.0
        )

        score = 0.0
        score += min(total_answers / 25.0, 6.0)
        score += min(entropy * 2.2, 6.0)
        score += min(non_trivial_answers * 0.8, 4.0)
        score += 2.0 if 2 <= unique_answers <= 7 else 0.0
        score -= 4.0 if dominant_share_percent >= 95.0 else 0.0
        score -= 2.0 if dominant_share_percent >= 88.0 else 0.0
        score -= 2.0 if unique_answers > 12 else 0.0
        score -= min(long_label_count * 0.4, 2.0)
        if request_mentions_compare:
            score += 1.0
        if request_mentions_share and unique_answers <= 6:
            score += 0.7

        hard_skip_reason = ""
        if _question_looks_like_low_value_dashboard_topic(question):
            hard_skip_reason = (
                "Skipped this topic because it looks like an administrative or fact-recall question that is not useful for the first-pass dashboard."
            )
        elif non_trivial_answers <= 1:
            hard_skip_reason = (
                "Skipped this topic because nearly all responses collapse into one answer, so the chart would not add useful contrast."
            )
        elif unique_answers == 2 and dominant_share_percent >= 75.0:
            hard_skip_reason = (
                "Skipped this topic because the binary split is too one-sided to be informative in the first-pass dashboard."
            )
        elif unique_answers <= 4 and dominant_share_percent >= 85.0 and entropy < 0.85:
            hard_skip_reason = (
                "Skipped this topic because one answer dominates the distribution too strongly for a meaningful chart."
            )
        elif unique_answers > 10:
            hard_skip_reason = (
                "Skipped this topic because it has too many answer categories for a compact first-pass chart."
            )
        elif average_label_length > 48 and unique_answers >= 5:
            hard_skip_reason = (
                "Skipped this topic because the answer labels are too long for a compact comparison chart."
            )

        chart_type = _fallback_chart_type_for_question(points)
        if chart_type == "pie" and request_mentions_compare:
            chart_type = "bar"
        if unique_answers > 6 or average_label_length > 28 or long_label_count > 0:
            chart_type = "bar"
        elif (
            request_mentions_share
            and 2 <= unique_answers <= 5
            and dominant_share_percent < 90.0
            and average_label_length <= 24
        ):
            chart_type = "pie"

        if hard_skip_reason:
            chart_type = "none"
        elif score < 3.8:
            chart_type = "none"

        if chart_type == "none":
            reason = hard_skip_reason or (
                "Skipped this topic because the answers are too one-sided or not distinct enough to add value in the first-pass dashboard."
            )
        elif chart_type == "pie":
            reason = (
                f"Used a pie chart because this topic has {unique_answers} compact categories and is best read as a part-to-whole split."
            )
        else:
            reason = (
                f"Used a bar chart because this topic has {unique_answers} answer categories and the comparison is clearer on a shared axis."
            )

        scored_specs.append(
            {
                "question": question,
                "chartType": chart_type,
                "reason": reason,
                "showPriority": 0,
                "score": score,
            }
        )

    if not scored_specs:
        return []

    visible_specs = sorted(
        (spec for spec in scored_specs if spec["chartType"] != "none"),
        key=lambda spec: (-float(spec["score"]), str(spec["question"]).casefold()),
    )[:4]
    if not visible_specs:
        visible_specs = sorted(
            scored_specs,
            key=lambda spec: (-float(spec["score"]), str(spec["question"]).casefold()),
        )[:2]

    for index, spec in enumerate(visible_specs, start=1):
        spec["showPriority"] = index

    return [
        {
            "question": str(spec["question"]),
            "chartType": str(spec["chartType"]),
            "reason": str(spec["reason"]),
            "showPriority": int(spec["showPriority"]),
        }
        for spec in visible_specs
    ]


def _truncate_dashboard_label(label: str, max_length: int = 44) -> str:
    normalized = " ".join(str(label or "").split())
    if len(normalized) <= max_length:
        return normalized
    return f"{normalized[: max_length - 1].rstrip()}…"


_DOMAIN_SEED_GROUPS = (
    {
        "slug": "ai",
        "display": "AI",
        "keywords": (" ai ", "ปัญญาประดิษฐ์", "เอไอ", "machine learning", "artificial intelligence"),
    },
    {
        "slug": "lora",
        "display": "LoRa",
        "keywords": ("lora",),
    },
    {
        "slug": "iot",
        "display": "IoT",
        "keywords": ("iot", "internet of things"),
    },
    {
        "slug": "kidbright",
        "display": "KidBright",
        "keywords": ("kidbright",),
    },
    {
        "slug": "platform",
        "display": "Platform",
        "keywords": ("platform", "แพลตฟอร์ม"),
    },
    {
        "slug": "network",
        "display": "Network",
        "keywords": ("network", "เครือข่าย", "wifi", "wi-fi", "router", "networking"),
    },
    {
        "slug": "programming",
        "display": "Programming",
        "keywords": ("program", "coding", "code", "เขียนโปรแกรม", "โปรแกรมมิง"),
    },
    {
        "slug": "data",
        "display": "Data",
        "keywords": ("data", "ข้อมูล", "dataset"),
    },
    {
        "slug": "sensor",
        "display": "Sensors",
        "keywords": ("sensor", "เซนเซอร์"),
    },
)

_DOMAIN_TOKEN_STOPWORDS = {
    "what",
    "which",
    "following",
    "question",
    "questions",
    "answer",
    "answers",
    "choose",
    "example",
    "examples",
    "system",
    "platform",
    "process",
    "incorrect",
    "correct",
    "benefit",
    "benefits",
    "topic",
    "topics",
    "about",
    "using",
    "use",
    "with",
    "from",
    "that",
    "this",
    "คือ",
    "ข้อใด",
    "ตัวอย่าง",
    "ระบบ",
    "ข้อมูล",
}


def _extract_question_domain_labels(
    question: str,
    repeated_token_labels: dict[str, str],
) -> list[str]:
    normalized = f" {str(question or '').strip().casefold()} "
    labels: list[str] = []
    for seed in _DOMAIN_SEED_GROUPS:
        if any(keyword in normalized for keyword in seed["keywords"]):
            labels.append(str(seed["display"]))
    for token, display in repeated_token_labels.items():
        if f" {token} " in normalized or token in normalized:
            labels.append(display)
    deduped: list[str] = []
    for label in labels:
        if label not in deduped:
            deduped.append(label)
    return deduped


def _infer_repeated_domain_tokens(grouped_questions: list[dict[str, Any]]) -> dict[str, str]:
    token_examples: dict[str, str] = {}
    token_counts: dict[str, int] = {}
    for item in grouped_questions:
        question = str(item.get("question", "") or "")
        seen_tokens: set[str] = set()
        for raw_token in re.findall(r"[A-Za-z][A-Za-z0-9\-]{1,24}", question):
            token = raw_token.casefold()
            if token in _DOMAIN_TOKEN_STOPWORDS or len(token) < 2:
                continue
            seen_tokens.add(token)
            token_examples.setdefault(token, raw_token)
        for token in seen_tokens:
            token_counts[token] = token_counts.get(token, 0) + 1

    repeated: dict[str, str] = {}
    seeded_labels = {
        str(seed["display"]).casefold()
        for seed in _DOMAIN_SEED_GROUPS
    }
    for token, count in token_counts.items():
        if count < 2:
            continue
        display = str(token_examples.get(token, token))
        if display.casefold() in seeded_labels:
            continue
        repeated[token] = display
    return repeated


def _build_domain_specific_topics(
    grouped_questions: list[dict[str, Any]],
    existing_titles: set[str],
    *,
    prefer_thai: bool,
) -> list[dict[str, Any]]:
    if not grouped_questions:
        return []

    repeated_token_labels = _infer_repeated_domain_tokens(grouped_questions)
    domain_members: dict[str, list[dict[str, Any]]] = {}
    for item in grouped_questions:
        question = str(item.get("question", "") or "").strip()
        if not question:
            continue
        for label in _extract_question_domain_labels(question, repeated_token_labels):
            domain_members.setdefault(label, []).append(item)

    candidates: list[dict[str, Any]] = []
    for label, members in domain_members.items():
        if len(members) < 2:
            continue
        disagreement_values = [
            max(0.0, 100.0 - float(member.get("dominantSharePercent", 0.0) or 0.0))
            for member in members
        ]
        if not disagreement_values:
            continue
        average_disagreement = sum(disagreement_values) / len(disagreement_values)
        average_consensus = sum(
            float(member.get("dominantSharePercent", 0.0) or 0.0)
            for member in members
        ) / len(members)
        if average_disagreement >= 18.0:
            metric = "disagreement"
            ordered_members = sorted(
                members,
                key=lambda member: (
                    -(100.0 - float(member.get("dominantSharePercent", 0.0) or 0.0)),
                    -float(member.get("totalAnswers", 0) or 0),
                    str(member.get("question", "")).casefold(),
                ),
            )[:5]
            title = (
                f"ประเด็นด้าน {label} ที่ยังสับสน"
                if prefer_thai
                else f"{label} topics with the most confusion"
            )
            summary = (
                f"คำถามในกลุ่ม {label} ยังมีคำตอบกระจายหลายจุด โดยประเด็นที่สับสนที่สุดอยู่ทางขวาของกราฟ เหมาะสำหรับใช้หาจุดที่ควรอธิบายเพิ่ม"
                if prefer_thai
                else f"This view isolates the {label}-related questions where answers are still most split, highlighting where explanation or reteaching is likely needed."
            )
        else:
            metric = "consensus"
            ordered_members = sorted(
                members,
                key=lambda member: (
                    -float(member.get("dominantSharePercent", 0.0) or 0.0),
                    float(_compute_answer_distribution_entropy(member.get("points", []))),
                    -float(member.get("totalAnswers", 0) or 0),
                    str(member.get("question", "")).casefold(),
                ),
            )[:5]
            title = (
                f"ประเด็นด้าน {label} ที่เข้าใจตรงกันมากที่สุด"
                if prefer_thai
                else f"{label} topics with the strongest agreement"
            )
            summary = (
                f"คำถามในกลุ่ม {label} มีแนวโน้มตอบไปในทิศทางเดียวกันมากกว่าเรื่องอื่น จึงใช้ดูว่าเนื้อหาส่วนใดผู้ตอบเข้าใจร่วมกันแล้ว"
                if prefer_thai
                else f"This view focuses on the {label}-related questions with the strongest agreement, showing which parts of that domain already look well understood."
            )

        if title in existing_titles:
            continue

        series = []
        for member in ordered_members:
            dominant_share = float(member.get("dominantSharePercent", 0.0) or 0.0)
            value = max(0.0, 100.0 - dominant_share) if metric == "disagreement" else dominant_share
            series.append(
                {
                    "label": _truncate_dashboard_label(str(member.get("question", "") or ""), 56),
                    "value": round(value, 2),
                    "percent": round(value, 2),
                }
            )
        if len(series) < 2:
            continue

        candidates.append(
            {
                "id": f"derived-domain-{re.sub(r'[^a-z0-9]+', '-', label.casefold()).strip('-') or 'topic'}",
                "title": title,
                "chartType": "bar",
                "total": len(members),
                "series": series,
                "summary": summary,
                "_priority": (
                    len(members),
                    average_disagreement if metric == "disagreement" else average_consensus,
                ),
            }
        )

    ranked = sorted(
        candidates,
        key=lambda item: (
            -int(item["_priority"][0]),
            -float(item["_priority"][1]),
            str(item.get("title", "")).casefold(),
        ),
    )[:2]
    for item in ranked:
        item.pop("_priority", None)
        existing_titles.add(str(item.get("title", "") or "").strip())
    return ranked


def _build_synthetic_dashboard_topics(
    grouped_questions: list[dict[str, Any]],
    existing_titles: set[str],
    *,
    prefer_thai: bool,
) -> list[dict[str, Any]]:
    if not grouped_questions:
        return []

    disagreement_points: list[dict[str, Any]] = []
    consensus_points: list[dict[str, Any]] = []
    for item in grouped_questions:
        question = str(item.get("question", "") or "").strip()
        if not question:
            continue
        total_answers = max(int(item.get("totalAnswers", 0) or 0), 0)
        dominant_share = float(item.get("dominantSharePercent", 0.0) or 0.0)
        entropy = _compute_answer_distribution_entropy(item.get("points", []))
        disagreement = round(max(0.0, 100.0 - dominant_share), 2)
        if disagreement >= 8.0:
            disagreement_points.append(
                {
                    "label": _truncate_dashboard_label(question),
                    "value": disagreement,
                    "percent": disagreement,
                    "_sort_entropy": entropy,
                    "_sort_total": total_answers,
                }
            )
        consensus_points.append(
            {
                "label": _truncate_dashboard_label(question),
                "value": round(dominant_share, 2),
                "percent": round(dominant_share, 2),
                "_sort_entropy": entropy,
                "_sort_total": total_answers,
            }
        )

    synthetic_charts: list[dict[str, Any]] = []
    disagreement_title = (
        "คำถามที่คำตอบแตกมากที่สุด"
        if prefer_thai
        else "Most split questions"
    )
    if (
        disagreement_title not in existing_titles
        and len(disagreement_points) >= 2
    ):
        top_disagreement = sorted(
            disagreement_points,
            key=lambda point: (
                -float(point["value"]),
                -float(point["_sort_entropy"]),
                -int(point["_sort_total"]),
                str(point["label"]).casefold(),
            ),
        )[:5]
        synthetic_charts.append(
            {
                "id": "derived-most-split",
                "title": disagreement_title,
                "chartType": "bar",
                "total": len(top_disagreement),
                "series": [
                    {
                        "label": point["label"],
                        "value": point["value"],
                        "percent": point["percent"],
                    }
                    for point in top_disagreement
                ],
                "reason": (
                    "หัวข้อนี้ถูกสร้างขึ้นเพื่อเน้นคำถามที่มีความเห็นกระจายมากที่สุด เมื่อคำถามรายข้อเดี่ยว ๆ ยังไม่เด่นพอสำหรับแดชบอร์ดรอบแรก"
                    if prefer_thai
                    else "This derived topic highlights the questions with the widest spread of answers when individual question charts are not strong enough for the first-pass dashboard."
                ),
            }
        )
        existing_titles.add(disagreement_title)

    consensus_title = (
        "คำถามที่คำตอบไปในทิศทางเดียวกันมากที่สุด"
        if prefer_thai
        else "Strongest consensus questions"
    )
    if consensus_title not in existing_titles and len(consensus_points) >= 2:
        top_consensus = sorted(
            consensus_points,
            key=lambda point: (
                -float(point["value"]),
                float(point["_sort_entropy"]),
                -int(point["_sort_total"]),
                str(point["label"]).casefold(),
            ),
        )[:5]
        synthetic_charts.append(
            {
                "id": "derived-strongest-consensus",
                "title": consensus_title,
                "chartType": "bar",
                "total": len(top_consensus),
                "series": [
                    {
                        "label": point["label"],
                        "value": point["value"],
                        "percent": point["percent"],
                    }
                    for point in top_consensus
                ],
                "reason": (
                    "หัวข้อนี้ถูกสร้างขึ้นเพื่อสรุปว่าคำถามใดมีฉันทามติสูงที่สุด ซึ่งช่วยให้เห็นภาพรวมของประเด็นที่ผู้ตอบเห็นตรงกัน"
                    if prefer_thai
                    else "This derived topic summarizes which questions reached the strongest consensus, giving a compact overview of where respondents aligned most clearly."
                ),
            }
        )
        existing_titles.add(consensus_title)

    return synthetic_charts


def _build_segment_composition_topic(
    processed_headers: list[str],
    processed_rows: list[list[str]],
    existing_titles: set[str],
    *,
    prefer_thai: bool,
) -> dict[str, Any] | None:
    if not processed_headers or not processed_rows:
        return None

    best_candidate: dict[str, Any] | None = None
    for index, header in enumerate(processed_headers):
        if not _metadata_header_is_segmentable(header):
            continue
        counts: dict[str, int] = {}
        for row in processed_rows:
            if index >= len(row):
                continue
            value = str(row[index] or "").strip()
            if not value:
                continue
            counts[value] = counts.get(value, 0) + 1
        unique_values = len(counts)
        if unique_values < 2 or unique_values > 6:
            continue
        total = sum(counts.values())
        if total <= 0:
            continue
        entropy = _compute_answer_distribution_entropy(
            [{"label": key, "value": value} for key, value in counts.items()]
        )
        score = entropy + min(total / 25.0, 3.0)
        candidate = {
            "header": header,
            "counts": counts,
            "score": score,
            "total": total,
        }
        if best_candidate is None or float(candidate["score"]) > float(best_candidate["score"]):
            best_candidate = candidate

    if best_candidate is None:
        return None

    title = (
        f"สัดส่วนผู้ตอบตาม{best_candidate['header']}"
        if prefer_thai
        else f"Respondent composition by {best_candidate['header']}"
    )
    if title in existing_titles:
        return None
    sorted_points = sorted(
        best_candidate["counts"].items(),
        key=lambda item: (-item[1], str(item[0]).casefold()),
    )[:6]
    total = int(best_candidate["total"])
    return {
        "id": "derived-respondent-composition",
        "title": title,
        "chartType": "bar",
        "total": total,
        "series": [
            {
                "label": _truncate_dashboard_label(label),
                "value": count,
                "percent": round((count / total) * 100, 2) if total else 0.0,
            }
            for label, count in sorted_points
        ],
        "reason": (
            f"หัวข้อนี้ถูกสร้างขึ้นเพื่อให้เห็นโครงสร้างผู้ตอบตาม{best_candidate['header']} ซึ่งช่วยตีความผลคำตอบในภาพรวมได้ลึกขึ้น"
            if prefer_thai
            else f"This derived topic shows respondent composition by {best_candidate['header']}, adding segment context for the rest of the analysis."
        ),
    }


def _build_overall_question_agreement_topic(
    grouped_questions: list[dict[str, Any]],
    existing_titles: set[str],
    *,
    prefer_thai: bool,
) -> dict[str, Any] | None:
    if not grouped_questions:
        return None

    title = (
        "ภาพรวมระดับความเห็นตรงกันของคำถาม"
        if prefer_thai
        else "Overall question agreement profile"
    )
    if title in existing_titles:
        return None

    buckets = [
        {
            "label": "เห็นตรงกันสูง" if prefer_thai else "High consensus",
            "min": 80.0,
            "max": 101.0,
        },
        {
            "label": "เห็นตรงกันปานกลาง" if prefer_thai else "Moderate consensus",
            "min": 60.0,
            "max": 80.0,
        },
        {
            "label": "คำตอบค่อนข้างแตก" if prefer_thai else "Mixed responses",
            "min": 0.0,
            "max": 60.0,
        },
    ]
    counts = {bucket["label"]: 0 for bucket in buckets}
    total_questions = 0
    for item in grouped_questions:
        total_answers = max(int(item.get("totalAnswers", 0) or 0), 0)
        if total_answers <= 0:
            continue
        total_questions += 1
        dominant_share = float(item.get("dominantSharePercent", 0.0) or 0.0)
        for bucket in buckets:
            if bucket["min"] <= dominant_share < bucket["max"]:
                counts[bucket["label"]] += 1
                break

    if total_questions < 2:
        return None

    series = [
        {
            "label": bucket["label"],
            "value": counts[bucket["label"]],
            "percent": round((counts[bucket["label"]] / total_questions) * 100, 2),
        }
        for bucket in buckets
        if counts[bucket["label"]] > 0
    ]
    if len(series) < 2:
        return None

    return {
        "id": "derived-overall-agreement-profile",
        "title": title,
        "chartType": "bar",
        "total": total_questions,
        "series": series,
        "reason": (
            "กราฟนี้สรุปภาพรวมว่าคำถามส่วนใหญ่มีระดับความเห็นตรงกันมากน้อยเพียงใด เพื่อให้เห็นโครงสร้างของผลลัพธ์ทั้งชุดในมุมเดียว"
            if prefer_thai
            else "This chart summarizes how the questions distribute across high-consensus, moderate-consensus, and mixed-response patterns, so the full result set can be read at a glance."
        ),
    }


def _build_score_distribution_topic(
    processed_headers: list[str],
    processed_rows: list[list[str]],
    existing_titles: set[str],
    *,
    prefer_thai: bool,
) -> dict[str, Any] | None:
    if not processed_headers or not processed_rows:
        return None

    score_index: int | None = None
    for index, header in enumerate(processed_headers):
        lowered = header.strip().casefold()
        if "score" in lowered or "คะแนน" in lowered:
            score_index = index
            break
    if score_index is None:
        return None

    counts: dict[str, int] = {}
    numeric_values: list[float] = []
    for row in processed_rows:
        if score_index >= len(row):
            continue
        raw_value = str(row[score_index] or "").strip()
        if not raw_value:
            continue
        try:
            numeric_value = float(raw_value)
        except ValueError:
            continue
        numeric_values.append(numeric_value)
        label = str(int(numeric_value)) if numeric_value.is_integer() else f"{numeric_value:.1f}"
        counts[label] = counts.get(label, 0) + 1

    if len(numeric_values) < 3 or len(counts) < 2:
        return None

    title = "การกระจายคะแนนรวม" if prefer_thai else "Overall score distribution"
    if title in existing_titles:
        return None
    total = len(numeric_values)
    sorted_points = sorted(
        counts.items(),
        key=lambda item: float(item[0]),
    )[:12]
    return {
        "id": "derived-score-distribution",
        "title": title,
        "chartType": "bar",
        "total": total,
        "series": [
            {
                "label": label,
                "value": count,
                "percent": round((count / total) * 100, 2) if total else 0.0,
            }
            for label, count in sorted_points
        ],
        "reason": (
            "กราฟนี้แสดงการกระจายคะแนนรวมของผู้ตอบทั้งหมด เพื่อให้เห็นระดับผลลัพธ์โดยรวมของแบบทดสอบ"
            if prefer_thai
            else "This chart shows the overall score distribution across respondents, which is more useful than plotting question titles against percentages."
        ),
    }


def _build_deep_analysis_insights(
    grouped_questions: list[dict[str, Any]],
    processed_headers: list[str],
    processed_rows: list[list[str]],
    *,
    prefer_thai: bool,
) -> list[dict[str, str]]:
    insights: list[dict[str, str]] = []
    if not grouped_questions:
        return insights

    sorted_by_disagreement = sorted(
        grouped_questions,
        key=lambda item: (
            -(100.0 - float(item.get("dominantSharePercent", 0.0) or 0.0)),
            -float(item.get("totalAnswers", 0) or 0),
            str(item.get("question", "")).casefold(),
        ),
    )
    sorted_by_consensus = sorted(
        grouped_questions,
        key=lambda item: (
            -float(item.get("dominantSharePercent", 0.0) or 0.0),
            float(_compute_answer_distribution_entropy(item.get("points", []))),
            -float(item.get("totalAnswers", 0) or 0),
            str(item.get("question", "")).casefold(),
        ),
    )

    most_split = sorted_by_disagreement[0] if sorted_by_disagreement else None
    if most_split:
        top_answers = most_split.get("points", [])[:2]
        if top_answers:
            details = ", ".join(
                f"{point['label']} {round(float(point.get('percent', 0.0) or 0.0), 1)}%"
                for point in top_answers
            )
            insights.append(
                {
                    "title": "ประเด็นที่เห็นต่างมากที่สุด" if prefer_thai else "Largest disagreement",
                    "summary": (
                        f"{most_split['question']} มีคำตอบกระจายสูง โดยคำตอบหลักคือ {details}"
                        if prefer_thai
                        else f"{most_split['question']} is the most split topic, led by {details}."
                    ),
                }
            )

    strongest_consensus = sorted_by_consensus[0] if sorted_by_consensus else None
    if strongest_consensus and strongest_consensus.get("points"):
        top_point = strongest_consensus["points"][0]
        insights.append(
            {
                "title": "ประเด็นที่เห็นตรงกันมากที่สุด" if prefer_thai else "Strongest consensus",
                "summary": (
                    f"{strongest_consensus['question']} มีคำตอบไปในทิศทางเดียวกันมากที่สุด โดย {top_point['label']} คิดเป็น {round(float(top_point.get('percent', 0.0) or 0.0), 1)}%"
                    if prefer_thai
                    else f"{strongest_consensus['question']} has the strongest consensus, with {top_point['label']} at {round(float(top_point.get('percent', 0.0) or 0.0), 1)}%."
                ),
            }
        )

    repeated_token_labels = _infer_repeated_domain_tokens(grouped_questions)
    domain_members: dict[str, list[dict[str, Any]]] = {}
    for item in grouped_questions:
        question = str(item.get("question", "") or "").strip()
        if not question:
            continue
        for label in _extract_question_domain_labels(question, repeated_token_labels):
            domain_members.setdefault(label, []).append(item)

    strongest_domain_label = ""
    strongest_domain_gap = -1.0
    strongest_domain_question = ""
    for label, members in domain_members.items():
        if len(members) < 2:
            continue
        worst_member = max(
            members,
            key=lambda member: (
                100.0 - float(member.get("dominantSharePercent", 0.0) or 0.0),
                float(member.get("totalAnswers", 0) or 0.0),
            ),
        )
        disagreement = 100.0 - float(worst_member.get("dominantSharePercent", 0.0) or 0.0)
        if disagreement > strongest_domain_gap:
            strongest_domain_gap = disagreement
            strongest_domain_label = label
            strongest_domain_question = str(worst_member.get("question", "") or "").strip()

    if strongest_domain_label and strongest_domain_question:
        insights.append(
            {
                "title": (
                    f"จุดที่ยังสับสนในหัวข้อ {strongest_domain_label}"
                    if prefer_thai
                    else f"{strongest_domain_label} confusion hotspot"
                ),
                "summary": (
                    f"เมื่อดูคำถามที่เกี่ยวข้องกับ {strongest_domain_label} โดยรวมแล้ว ประเด็นที่ยังเห็นความไม่แน่ใจชัดที่สุดคือ {strongest_domain_question}"
                    if prefer_thai
                    else f"Within the {strongest_domain_label} domain, the clearest remaining confusion appears in {strongest_domain_question}."
                ),
            }
        )

    metadata_indices, _, _ = _classify_analysis_columns(processed_headers) if processed_headers else ([], [], None)
    for index in metadata_indices:
        header = processed_headers[index]
        if not _metadata_header_is_segmentable(header):
            continue
        counts: dict[str, int] = {}
        for row in processed_rows:
            if index >= len(row):
                continue
            value = str(row[index] or "").strip()
            if not value:
                continue
            counts[value] = counts.get(value, 0) + 1
        if len(counts) < 2 or len(counts) > 6:
            continue
        total = sum(counts.values())
        if total <= 0:
            continue
        label, count = sorted(
            counts.items(),
            key=lambda item: (-item[1], str(item[0]).casefold()),
        )[0]
        insights.append(
            {
                "title": f"โครงสร้างผู้ตอบตาม{header}" if prefer_thai else f"Respondent mix by {header}",
                "summary": (
                    f"กลุ่ม {label} มีจำนวนมากที่สุด คิดเป็น {round((count / total) * 100, 1)}% ของผู้ตอบทั้งหมด"
                    if prefer_thai
                    else f"{label} is the largest segment at {round((count / total) * 100, 1)}% of respondents."
                ),
            }
        )
        break

    return insights[:4]


def _build_spreadsheet_visual_payload(
    *,
    spreadsheet_id: str,
    spreadsheet_title: str,
    spreadsheet_url: str,
    processed_sheet_name: str,
    summary_sheet_name: str,
    row_count_written: int,
    summary_rows: list[list[str]],
    analysis_request: str,
    processed_headers: list[str],
    processed_rows: list[list[str]],
    user_language: str,
) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for question, answer, count_raw, percent_raw in summary_rows:
        grouped.setdefault(question, []).append(
            {
                "label": answer,
                "value": _coerce_summary_count(count_raw),
                "percent": _coerce_summary_percent(percent_raw),
            }
        )

    grouped_questions: list[dict[str, Any]] = []
    for question, points in grouped.items():
        ordered_points = sorted(
            points,
            key=lambda point: (-int(point["value"]), str(point["label"]).casefold()),
        )
        total = sum(int(point["value"]) for point in ordered_points)
        if total <= 0:
            continue
        dominant_share_percent = (
            round((max(int(point["value"]) for point in ordered_points) / total) * 100, 2)
            if ordered_points
            else 0.0
        )
        grouped_questions.append(
            {
                "question": question,
                "points": ordered_points,
                "totalAnswers": total,
                "dominantSharePercent": dominant_share_percent,
            }
        )

    charts: list[dict[str, Any]] = []
    if _request_prefers_raw_question_charts(analysis_request):
        chart_specs = _choose_chart_specs_with_ai(grouped_questions, analysis_request)
        grouped_lookup = {item["question"]: item for item in grouped_questions}
        for index, spec in enumerate(
            sorted(chart_specs, key=lambda item: int(item.get("showPriority", 9999))),
            start=1,
        ):
            question = str(spec.get("question", "") or "")
            if not question or question not in grouped_lookup:
                continue
            if str(spec.get("chartType", "") or "").lower() == "none":
                continue
            grouped_item = grouped_lookup[question]
            ordered_points = grouped_item["points"]
            total = int(grouped_item["totalAnswers"])
            chart_type = str(spec.get("chartType", "") or "bar").lower()
            if chart_type not in {"bar", "pie"}:
                chart_type = _fallback_chart_type_for_question(ordered_points)
            charts.append(
                {
                    "id": f"question-{index}",
                    "title": question,
                    "chartType": chart_type,
                    "total": total,
                    "series": ordered_points,
                    "reason": str(spec.get("reason", "") or ""),
                }
            )
            if len(charts) >= 4:
                break

    existing_titles = {
        str(chart.get("title", "") or "").strip()
        for chart in charts
        if str(chart.get("title", "") or "").strip()
    }
    prefer_thai = _contains_thai(spreadsheet_title) or any(
        _contains_thai(item["question"]) for item in grouped_questions
    )

    existing_titles = {
        str(chart.get("title", "") or "").strip()
        for chart in charts
        if str(chart.get("title", "") or "").strip()
    }
    domain_charts = _build_domain_specific_topics(
        grouped_questions,
        existing_titles,
        prefer_thai=prefer_thai,
    )
    for chart in domain_charts:
        if len(charts) >= 3:
            break
        charts.append(chart)
        existing_titles.add(str(chart.get("title", "") or "").strip())

    score_chart = _build_score_distribution_topic(
        processed_headers,
        processed_rows,
        existing_titles,
        prefer_thai=prefer_thai,
    )
    if score_chart and len(charts) < 3:
        charts.append(score_chart)
        existing_titles.add(str(score_chart.get("title", "") or "").strip())

    overall_agreement_chart = _build_overall_question_agreement_topic(
        grouped_questions,
        existing_titles,
        prefer_thai=prefer_thai,
    )
    if overall_agreement_chart and len(charts) < 3:
        charts.append(overall_agreement_chart)
        existing_titles.add(str(overall_agreement_chart.get("title", "") or "").strip())

    segment_chart = _build_segment_composition_topic(
        processed_headers,
        processed_rows,
        existing_titles,
        prefer_thai=prefer_thai,
    )
    if segment_chart and len(charts) < 3:
        charts.append(segment_chart)

    insights = _build_deep_analysis_insights(
        grouped_questions,
        processed_headers,
        processed_rows,
        prefer_thai=prefer_thai,
    )

    return {
        "version": 1,
        "kind": "spreadsheet-analysis-visual",
        "userLanguage": "th" if user_language == "th" else "en",
        "spreadsheetId": spreadsheet_id,
        "spreadsheetTitle": spreadsheet_title,
        "spreadsheetUrl": spreadsheet_url,
        "processedSheetName": processed_sheet_name,
        "summarySheetName": summary_sheet_name,
        "rowCountWritten": row_count_written,
        "questionCount": len(grouped),
        "analysisRequest": analysis_request.strip(),
        "insights": insights,
        "charts": charts,
    }


def _append_spreadsheet_visual_payload(text: str, payload: dict[str, Any]) -> str:
    return (
        f"{text.strip()}\n\n"
        f"{SPREADSHEET_ANALYSIS_VISUAL_START}\n"
        f"{json.dumps(payload, ensure_ascii=False)}\n"
        f"{SPREADSHEET_ANALYSIS_VISUAL_END}"
    ).strip()


def maybe_complete_spreadsheet_analysis_request(messages: list[AnyMessage]) -> AIMessage | None:
    """Directly inspect and summarize a response spreadsheet with chart-ready data."""
    latest_human_index = -1
    for index in range(len(messages) - 1, -1, -1):
        if messages[index].type == "human":
            latest_human_index = index
            break

    if latest_human_index == -1:
        return None

    latest_human_content = content_to_text(messages[latest_human_index].content)
    targets = extract_spreadsheet_targets(latest_human_content)
    if not targets:
        return None

    stripped_request = strip_spreadsheet_targets(latest_human_content, targets).strip()
    if stripped_request.lower().startswith("spreadsheet_task"):
        return None
    normalized_request = stripped_request.casefold()
    explicit_format_markers = (
        "format",
        "reformat",
        "prepare",
        "analysis-ready",
        "จัดรูปแบบ",
        "จัดใหม่",
        "เตรียมวิเคราะห์",
        "เตรียม",
        "ลบชีต",
        "remove tab",
        "remove sheet",
    )
    if any(marker in normalized_request for marker in explicit_format_markers):
        return None
    if not stripped_request and targets:
        pass
    elif not looks_like_spreadsheet_analysis_request(stripped_request or latest_human_content):
        return None
    if any(
        marker in normalized_request
        for marker in (
            "format",
            "จัดรูปแบบ",
            "reformat",
            "เตรียม",
            "เตรียมวิเคราะห์",
        )
    ):
        return None

    user_language = infer_user_language(latest_human_content)
    target = targets[0]

    try:
        credentials = _load_google_workspace_credentials()
        service = build_google_api(
            "sheets",
            "v4",
            credentials=credentials,
            cache_discovery=False,
        )
        spreadsheet_id = extract_spreadsheet_id(target)
        response_store_result = _best_effort_sync_agent_form_responses_by_spreadsheet(
            spreadsheet_id
        )
        payload = _resolve_existing_postprocess_payload(service, spreadsheet_id)
        if payload is None:
            formatted = format_response_sheet_for_analysis.invoke({"spreadsheet_target": target})
            payload = json.loads(formatted) if isinstance(formatted, str) else formatted
            if not isinstance(payload, dict):
                return None

        spreadsheet_id = str(payload.get("spreadsheetId", "") or spreadsheet_id).strip()
        spreadsheet_title = str(payload.get("spreadsheetTitle", "") or "").strip()
        processed_sheet_name = str(payload.get("outputSheet", "") or "").strip()
        summary_sheet_name = str(payload.get("summarySheet", "") or "").strip()
        row_count_written = int(payload.get("rowCountWritten", 0) or 0)
        summary_row_count = int(payload.get("questionSummaryRowCount", 0) or 0)
        reused_existing_postprocess = bool(payload.get("reusedExistingPostprocess"))
        spreadsheet_url = (
            f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit"
            if spreadsheet_id
            else ""
        )
        summary_rows = (
            _load_summary_rows_from_sheet(service, spreadsheet_id, summary_sheet_name)
            if spreadsheet_id and summary_sheet_name
            else []
        )
        processed_headers, processed_rows = (
            _load_processed_sheet_table(service, spreadsheet_id, processed_sheet_name)
        if spreadsheet_id and processed_sheet_name
            else ([], [])
        )
        if isinstance(payload, dict):
            payload.setdefault(
                "responseStoreStatus",
                str(response_store_result.get("status", "") or ""),
            )
            payload.setdefault(
                "responseStoreError",
                str(response_store_result.get("error", "") or ""),
            )
            payload.setdefault(
                "responseStoreBackend",
                str(response_store_result.get("store", "") or ""),
            )
            payload.setdefault(
                "responseStoreResponseCount",
                int(response_store_result.get("responseCount", 0) or 0),
            )
        visual_payload = _build_spreadsheet_visual_payload(
            spreadsheet_id=spreadsheet_id,
            spreadsheet_title=spreadsheet_title,
            spreadsheet_url=spreadsheet_url,
            processed_sheet_name=processed_sheet_name,
            summary_sheet_name=summary_sheet_name,
            row_count_written=row_count_written,
            summary_rows=summary_rows,
            analysis_request=stripped_request or latest_human_content,
            processed_headers=processed_headers,
            processed_rows=processed_rows,
            user_language=user_language,
        )

        unique_questions = int(visual_payload.get("questionCount", 0) or 0)
        chart_count = len(visual_payload.get("charts", []))
        selected_topics = [
            str(chart.get("title", "") or "").strip()
            for chart in visual_payload.get("charts", [])
            if str(chart.get("title", "") or "").strip()
        ]
        insight_titles = [
            str(insight.get("title", "") or "").strip()
            for insight in visual_payload.get("insights", [])
            if isinstance(insight, dict) and str(insight.get("title", "") or "").strip()
        ]
        top_chart = visual_payload["charts"][0] if chart_count else None
        top_insight = ""
        if top_chart and top_chart.get("series"):
            top_point = top_chart["series"][0]
            top_insight = (
                f"{top_chart['title']}: {top_point['label']} ({top_point['value']})"
            )

        if user_language == "th":
            response_lines = [
                "ฉันวิเคราะห์สเปรดชีตและเตรียมกราฟสรุปให้แล้ว",
                "",
                f"- สเปรดชีต: {spreadsheet_title or spreadsheet_id}",
                f"- ชีตคำตอบที่จัดรูปแบบ: {processed_sheet_name}",
                f"- ชีตสรุปคำตอบรายข้อ: {summary_sheet_name}",
                f"- โหมดการเตรียมข้อมูล: {'ใช้ชีตที่จัดรูปแบบเดิม' if reused_existing_postprocess else 'สร้างชีตจัดรูปแบบครั้งแรก'}",
                f"- จำนวนแถวคำตอบที่ใช้วิเคราะห์: {row_count_written}",
                f"- จำนวนคำถามที่มีคำตอบ: {unique_questions}",
                f"- จำนวนรายการสรุปคำตอบ: {summary_row_count}",
                f"- จำนวนกราฟที่แสดงในแชต: {chart_count}",
            ]
            if selected_topics:
                response_lines.append(
                    f"- หัวข้อที่เลือกมาพล็อต: {', '.join(selected_topics[:4])}"
                )
            if insight_titles:
                response_lines.append(
                    f"- มุมวิเคราะห์เชิงลึก: {', '.join(insight_titles[:3])}"
                )
            if top_insight:
                response_lines.append(f"- ประเด็นเด่น: {top_insight}")
            if spreadsheet_url:
                response_lines.extend(["", f"ลิงก์สเปรดชีต: {spreadsheet_url}"])
        else:
            response_lines = [
                "I analyzed the spreadsheet and prepared chart views for the results.",
                "",
                f"- Spreadsheet: {spreadsheet_title or spreadsheet_id}",
                f"- Processed responses sheet: {processed_sheet_name}",
                f"- Question summary sheet: {summary_sheet_name}",
                f"- Post-process mode: {'reused existing tabs' if reused_existing_postprocess else 'created tabs for first-time analysis'}",
                f"- Response rows analyzed: {row_count_written}",
                f"- Questions with responses: {unique_questions}",
                f"- Summary rows: {summary_row_count}",
                f"- Charts shown in chat: {chart_count}",
            ]
            if selected_topics:
                response_lines.append(
                    f"- Topics selected for plotting: {', '.join(selected_topics[:4])}"
                )
            if insight_titles:
                response_lines.append(
                    f"- Deep-analysis lenses: {', '.join(insight_titles[:3])}"
                )
            if top_insight:
                response_lines.append(f"- Top finding: {top_insight}")
            if spreadsheet_url:
                response_lines.extend(["", f"Spreadsheet link: {spreadsheet_url}"])

        response_lines.extend(
            [
                "",
                (
                    "กราฟในแชตอ้างอิงจากชีตสรุปคำตอบรายข้อ และยังใช้ลิงก์สเปรดชีตเดิมสำหรับวิเคราะห์ต่อได้"
                    if user_language == "th"
                    else "The charts in chat are based on the question summary sheet, and you can keep using the same spreadsheet link for deeper analysis."
                ),
            ]
        )
        response_text = "\n".join(response_lines).strip()
        return AIMessage(content=_append_spreadsheet_visual_payload(response_text, visual_payload))
    except Exception as exc:
        raise RuntimeError(
            (
                "ฉันตรวจพบว่านี่เป็นคำขอวิเคราะห์สเปรดชีต แต่การเตรียมกราฟวิเคราะห์ล้มเหลว "
                if user_language == "th"
                else "I recognized this as a spreadsheet-analysis request, but preparing the analysis graphs failed. "
            )
            + f"Details: {exc}"
        ) from exc


def maybe_complete_database_request(messages: list[AnyMessage]) -> AIMessage | None:
    """Directly answer obvious Postgres response-store requests with read-only SQL."""
    latest_human_index = -1
    for index in range(len(messages) - 1, -1, -1):
        if messages[index].type == "human":
            latest_human_index = index
            break

    if latest_human_index == -1:
        return None

    latest_human_content = content_to_text(messages[latest_human_index].content).strip()
    if not latest_human_content:
        return None

    user_language = infer_user_language(latest_human_content)
    lowered = latest_human_content.casefold()
    explicit_form_id = _extract_requested_form_id(latest_human_content)
    recent_thread_form_id = _extract_recent_thread_form_id(messages, latest_human_index)
    target_form_id = explicit_form_id or recent_thread_form_id
    embedded_sql = _extract_embedded_readonly_sql(latest_human_content)
    form_scoped_query_plan = (
        _plan_form_scoped_database_query(latest_human_content)
        if target_form_id
        else None
    )
    thread_form_analysis_request = (
        bool(recent_thread_form_id)
        and looks_like_spreadsheet_analysis_request(latest_human_content)
    )
    if (
        not looks_like_database_request(lowered)
        and not thread_form_analysis_request
        and not embedded_sql
        and not form_scoped_query_plan
        and not target_form_id
    ):
        return None
    if (
        looks_like_form_creation_request(latest_human_content)
        and not explicit_form_id
        and not thread_form_analysis_request
        and not embedded_sql
        and not form_scoped_query_plan
        and not target_form_id
    ):
        return None

    schema_markers = (
        "schema",
        "table",
        "tables",
        "column",
        "columns",
        "structure",
        "available",
        "what tables",
        "describe database",
        "โครงสร้าง",
        "ตาราง",
        "คอลัมน์",
        "มีอะไรบ้าง",
    )
    list_form_markers = (
        "list all forms",
        "list forms",
        "show forms",
        "all forms",
        "stored forms",
        "forms stored",
        "รายการฟอร์ม",
        "ฟอร์มทั้งหมด",
        "แสดงฟอร์ม",
        "ลิสต์ฟอร์ม",
    )
    latest_response_markers = (
        "latest responses",
        "recent responses",
        "last responses",
        "show responses",
        "responses",
        "คำตอบล่าสุด",
        "คำตอบทั้งหมด",
        "แสดงคำตอบ",
    )
    analysis_markers = (
        "analy",
        "analysis",
        "summary",
        "summarize",
        "insight",
        "dashboard",
        "chart",
        "graph",
        "วิเคราะห์",
        "สรุป",
        "อินไซต์",
        "แดชบอร์ด",
        "กราฟ",
        "ชาร์ต",
    )

    try:
        if embedded_sql:
            payload = _execute_readonly_response_store_query(embedded_sql, row_limit=200)
            row_count = int(payload.get("rowCount", 0) or 0)
            table = _format_query_payload_as_markdown_table(payload)
            if user_language == "th":
                lines = [
                    "ผลลัพธ์จากคำสั่ง SQL แบบอ่านอย่างเดียว:",
                    "",
                    f"- จำนวนแถวที่ส่งกลับ: {row_count}",
                ]
                if payload.get("truncated"):
                    lines.append("- ผลลัพธ์ถูกตัดตาม row limit")
                if table:
                    lines.extend(["", table])
                elif row_count == 0:
                    lines.extend(["", "ไม่พบข้อมูล"])
                return AIMessage(content="\n".join(lines).strip())
            lines = [
                "Read-only SQL query result:",
                "",
                f"- Rows returned: {row_count}",
            ]
            if payload.get("truncated"):
                lines.append("- Result truncated by row limit")
            if table:
                lines.extend(["", table])
            elif row_count == 0:
                lines.extend(["", "No rows found."])
            return AIMessage(content="\n".join(lines).strip())

        if any(marker in lowered for marker in schema_markers):
            payload = json.loads(inspect_form_response_database.invoke({}))
            if user_language == "th":
                lines = [
                    "โครงสร้างฐานข้อมูล Postgres สำหรับฟอร์มที่เอเจนต์จัดการ:",
                    "",
                ]
                for table in payload.get("tables", []):
                    if not isinstance(table, dict):
                        continue
                    lines.append(f"- {table.get('name', '')}: {table.get('description', '')}")
                return AIMessage(content="\n".join(lines).strip())
            lines = [
                "Postgres response-store schema:",
                "",
            ]
            for table in payload.get("tables", []):
                if not isinstance(table, dict):
                    continue
                lines.append(f"- {table.get('name', '')}: {table.get('description', '')}")
            return AIMessage(content="\n".join(lines).strip())

        if any(marker in lowered for marker in list_form_markers):
            payload = json.loads(
                query_form_response_database.invoke(
                    {
                        "sql": (
                            "SELECT form_id, form_title, spreadsheet_id, form_url, spreadsheet_url, updated_at "
                            "FROM agent_forms ORDER BY updated_at DESC"
                        ),
                        "row_limit": 200,
                    }
                )
            )
            rows = payload.get("rows", [])
            if user_language == "th":
                lines = ["รายการฟอร์มที่เก็บใน Postgres:", ""]
                if not rows:
                    lines.append("ไม่พบข้อมูลฟอร์ม")
                else:
                    table_rows: list[list[Any]] = []
                    for row in rows:
                        if not isinstance(row, dict):
                            continue
                        table_rows.append(
                            [
                                row.get("form_title") or row.get("form_id"),
                                row.get("form_id"),
                                row.get("spreadsheet_id"),
                                row.get("updated_at"),
                            ]
                        )
                    lines.append(
                        _format_markdown_table(
                            ["ชื่อฟอร์ม", "Form ID", "Spreadsheet ID", "อัปเดตล่าสุด"],
                            table_rows,
                        )
                    )
                return AIMessage(content="\n".join(lines).strip())
            lines = ["Forms stored in Postgres:", ""]
            if not rows:
                lines.append("No forms found.")
            else:
                table_rows = []
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    table_rows.append(
                        [
                            row.get("form_title") or row.get("form_id"),
                            row.get("form_id"),
                            row.get("spreadsheet_id"),
                            row.get("updated_at"),
                        ]
                    )
                lines.append(
                    _format_markdown_table(
                        ["Form title", "Form ID", "Spreadsheet ID", "Updated at"],
                        table_rows,
                    )
                )
            return AIMessage(content="\n".join(lines).strip())

        if any(marker in lowered for marker in analysis_markers):
            catalog_rows = _load_agent_form_catalog()
            form_row = _choose_database_analysis_form(
                latest_human_content,
                catalog_rows,
                preferred_form_id=recent_thread_form_id,
            )
            if not form_row:
                if user_language == "th":
                    return AIMessage(content="ยังไม่พบฟอร์มที่เก็บไว้ใน Postgres สำหรับการวิเคราะห์")
                return AIMessage(content="No stored Postgres form was found for analysis.")

            visual_payload, response_count, answered_question_count = _build_postgres_form_analysis_snapshot(
                form_row,
                analysis_request=latest_human_content,
                user_language=user_language,
            )
            chart_count = len(visual_payload.get("charts", []) or [])
            form_title = str(form_row.get("form_title", "") or form_row.get("form_id", "")).strip()
            form_id = str(form_row.get("form_id", "") or "").strip()
            if user_language == "th":
                response_text = "\n".join(
                    [
                        "ฉันวิเคราะห์ข้อมูลคำตอบจาก Postgres และเตรียมกราฟสรุปไว้แล้ว",
                        "",
                        f"- ฟอร์ม: {form_title}",
                        f"- Form ID: {form_id}",
                        f"- จำนวนแถวคำตอบที่ใช้วิเคราะห์: {response_count}",
                        f"- จำนวนคำถามที่มีคำตอบ: {answered_question_count}",
                        f"- จำนวนกราฟที่แสดงในแชต: {chart_count}",
                    ]
                ).strip()
            else:
                response_text = "\n".join(
                    [
                        "I analyzed the stored Postgres form responses and prepared summary charts.",
                        "",
                        f"- Form: {form_title}",
                        f"- Form ID: {form_id}",
                        f"- Response rows analyzed: {response_count}",
                        f"- Questions with answers: {answered_question_count}",
                        f"- Charts shown in chat: {chart_count}",
                    ]
                ).strip()
            return AIMessage(
                content=_append_spreadsheet_visual_payload(response_text, visual_payload)
            )

        if form_scoped_query_plan and form_scoped_query_plan.get("kind") == "response-rows":
            catalog_rows = _load_agent_form_catalog()
            form_row = _choose_database_analysis_form(
                latest_human_content,
                catalog_rows,
                preferred_form_id=target_form_id,
            )
            if (
                explicit_form_id
                and (
                    not form_row
                    or str(form_row.get("form_id", "") or "").strip() != explicit_form_id
                )
            ):
                _sync_form_id_into_response_store(explicit_form_id)
                catalog_rows = _load_agent_form_catalog()
                form_row = _choose_database_analysis_form(
                    latest_human_content,
                    catalog_rows,
                    preferred_form_id=explicit_form_id,
                )
            if not form_row:
                if user_language == "th":
                    return AIMessage(content="ยังไม่พบข้อมูลฟอร์มนี้ใน Postgres")
                return AIMessage(content="This form is not stored in Postgres yet.")

            form_id = str(form_row.get("form_id", "") or "").strip()
            payload = _execute_readonly_response_store_query(
                (
                    "SELECT r.response_id, r.created_time, r.respondent_email, "
                    "a.question_title, a.answer_text "
                    "FROM form_responses r "
                    "LEFT JOIN form_response_answers a "
                    "ON a.form_id = r.form_id AND a.response_id = r.response_id "
                    f"WHERE r.form_id = {_quote_sql_string_literal(form_id)} "
                    "ORDER BY r.created_time DESC NULLS LAST, r.response_id DESC, a.question_title ASC"
                ),
                row_limit=120,
            )
            row_count = int(payload.get("rowCount", 0) or 0)
            table = _format_query_payload_as_markdown_table(payload)
            form_title = str(form_row.get("form_title", "") or form_id).strip()
            if user_language == "th":
                lines = [
                    "ฉันดึงข้อมูลคำตอบของฟอร์มนี้จาก Postgres แล้ว",
                    "",
                    f"- ฟอร์ม: {form_title}",
                    f"- Form ID: {form_id}",
                    f"- จำนวนแถวที่ส่งกลับ: {row_count}",
                ]
                if payload.get("truncated"):
                    lines.append("- ผลลัพธ์ถูกตัดตาม row limit")
                if table:
                    lines.extend(["", table])
                elif row_count == 0:
                    lines.extend(["", "ยังไม่มีข้อมูลคำตอบ"])
                return AIMessage(content="\n".join(lines).strip())
            lines = [
                "I fetched this form's stored responses from Postgres.",
                "",
                f"- Form: {form_title}",
                f"- Form ID: {form_id}",
                f"- Rows returned: {row_count}",
            ]
            if payload.get("truncated"):
                lines.append("- Result truncated by row limit")
            if table:
                lines.extend(["", table])
            elif row_count == 0:
                lines.extend(["", "No stored responses found."])
            return AIMessage(content="\n".join(lines).strip())

        if form_scoped_query_plan and form_scoped_query_plan.get("kind") == "top-scorer":
            catalog_rows = _load_agent_form_catalog()
            form_row = _choose_database_analysis_form(
                latest_human_content,
                catalog_rows,
                preferred_form_id=target_form_id,
            )
            if (
                explicit_form_id
                and (
                    not form_row
                    or str(form_row.get("form_id", "") or "").strip() != explicit_form_id
                )
            ):
                _sync_form_id_into_response_store(explicit_form_id)
                catalog_rows = _load_agent_form_catalog()
                form_row = _choose_database_analysis_form(
                    latest_human_content,
                    catalog_rows,
                    preferred_form_id=explicit_form_id,
                )
            if not form_row:
                if user_language == "th":
                    return AIMessage(content="ยังไม่พบข้อมูลฟอร์มนี้ใน Postgres")
                return AIMessage(content="This form is not stored in Postgres yet.")

            form_id = str(form_row.get("form_id", "") or "").strip()
            fallback_sql = _build_top_scorer_fallback_sql(form_id, prefer_identity=True)
            payload = _execute_readonly_response_store_query(fallback_sql, row_limit=20)
            payload = _rename_identity_column(payload, "name")
            table = _format_query_payload_as_markdown_table(payload)
            if table:
                return AIMessage(content=table)
            if user_language == "th":
                return AIMessage(content="ไม่พบข้อมูล")
            return AIMessage(content="No rows found.")

        if any(marker in lowered for marker in latest_response_markers):
            payload = json.loads(
                query_form_response_database.invoke(
                    {
                        "sql": (
                            "SELECT form_id, response_id, created_time, respondent_email "
                            "FROM form_responses ORDER BY created_time DESC NULLS LAST"
                        ),
                        "row_limit": 50,
                    }
                )
            )
            rows = payload.get("rows", [])
            if user_language == "th":
                lines = ["คำตอบล่าสุดที่เก็บใน Postgres:", ""]
                if not rows:
                    lines.append("ไม่พบข้อมูลคำตอบ")
                else:
                    table_rows = []
                    for row in rows:
                        if not isinstance(row, dict):
                            continue
                        table_rows.append(
                            [
                                row.get("form_id"),
                                row.get("response_id"),
                                row.get("created_time"),
                                row.get("respondent_email"),
                            ]
                        )
                    lines.append(
                        _format_markdown_table(
                            ["Form ID", "Response ID", "เวลาส่งคำตอบ", "อีเมลผู้ตอบ"],
                            table_rows,
                        )
                    )
                return AIMessage(content="\n".join(lines).strip())
            lines = ["Latest responses stored in Postgres:", ""]
            if not rows:
                lines.append("No responses found.")
            else:
                table_rows = []
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    table_rows.append(
                        [
                            row.get("form_id"),
                            row.get("response_id"),
                            row.get("created_time"),
                            row.get("respondent_email"),
                        ]
                    )
                lines.append(
                    _format_markdown_table(
                        ["Form ID", "Response ID", "Created time", "Respondent email"],
                        table_rows,
                )
            )
            return AIMessage(content="\n".join(lines).strip())

        if looks_like_database_request(lowered) or target_form_id:
            sql, payload = _run_nl_to_sql_response_store_query(
                latest_human_content,
                target_form_id=target_form_id,
                user_language=user_language,
                row_limit=200,
            )
            table = _format_query_payload_as_markdown_table(payload)
            if table:
                return AIMessage(content=table)
            if user_language == "th":
                return AIMessage(content="ไม่พบข้อมูล")
            return AIMessage(content="No rows found.")
    except Exception as exc:
        raise RuntimeError(
            (
                "ฉันตรวจพบว่านี่เป็นคำขออ่านข้อมูลจาก Postgres แต่การดึงข้อมูลล้มเหลว "
                if user_language == "th"
                else "I recognized this as a Postgres data request, but reading the database failed. "
            )
            + f"Details: {exc}"
        ) from exc

    return None


def maybe_complete_form_database_link_request(messages: list[AnyMessage]) -> AIMessage | None:
    """Link a target form into the SQL response store when the user asks explicitly."""
    latest_human_index = -1
    for index in range(len(messages) - 1, -1, -1):
        if messages[index].type == "human":
            latest_human_index = index
            break

    if latest_human_index == -1:
        return None

    latest_human_content = content_to_text(messages[latest_human_index].content).strip()
    if not latest_human_content:
        return None

    if not looks_like_form_database_link_request(latest_human_content):
        return None

    user_language = infer_user_language(latest_human_content)
    explicit_form_id = _extract_requested_form_id(latest_human_content)
    recent_thread_form_id = _extract_recent_thread_form_id(messages, latest_human_index)
    target_form_id = explicit_form_id or recent_thread_form_id
    if not target_form_id:
        if user_language == "th":
            return AIMessage(
                content=(
                    "ฉันยังหา Form ID ที่จะเชื่อมเข้าฐานข้อมูลไม่พบ "
                    "ให้ส่ง Form ID โดยตรง หรือขอเชื่อมฟอร์มที่เพิ่งสร้างในเธรดนี้อีกครั้ง"
                )
            )
        return AIMessage(
            content=(
                "I could not determine which form to link into the database. "
                "Provide the Form ID directly, or ask to link the form that was just created in this thread."
            )
        )

    try:
        result = _sync_form_id_into_response_store(target_form_id)
        if not result.get("ok"):
            raise RuntimeError(str(result.get("error", "Unknown sync error")))

        catalog_rows = _load_agent_form_catalog()
        form_row = _choose_database_analysis_form(
            target_form_id,
            catalog_rows,
            preferred_form_id=target_form_id,
        ) or {}
        form_title = str(form_row.get("form_title", "") or target_form_id).strip()
        spreadsheet_id = str(form_row.get("spreadsheet_id", "") or "").strip()
        existing_details = _get_form_sheet_link_details(target_form_id)
        _upsert_form_sheet_link(
            target_form_id,
            {
                "formId": target_form_id,
                "formTitle": form_title,
                "formUrl": str(form_row.get("form_url", "") or "").strip(),
                "spreadsheetId": spreadsheet_id,
                "spreadsheetUrl": str(form_row.get("spreadsheet_url", "") or "").strip(),
                "googleOauthSessionKey": GOOGLE_OAUTH_SESSION_KEY.get() or "",
                "linkedAt": str(existing_details.get("linkedAt", "") or "")
                or datetime.now(timezone.utc).isoformat(),
            },
        )
        response_count = int(result.get("responseCount", 0) or 0)
        answer_count = int(result.get("answerCount", 0) or 0)
        store_label = str(result.get("store", "") or "SQL store").strip()
        synced_at = str(result.get("syncedAt", "") or "").strip()

        if user_language == "th":
            lines = [
                "ฉันเชื่อมฟอร์มนี้เข้ากับฐานข้อมูลคำตอบเรียบร้อยแล้ว",
                "",
                f"- ฟอร์ม: {form_title}",
                f"- Form ID: {target_form_id}",
                f"- ฐานข้อมูล: {store_label}",
                f"- จำนวนคำตอบที่ซิงก์: {response_count}",
                f"- จำนวนรายการคำตอบรายข้อ: {answer_count}",
            ]
            if spreadsheet_id:
                lines.append(f"- Spreadsheet ID: {spreadsheet_id}")
            if synced_at:
                lines.append(f"- ซิงก์ล่าสุด: {synced_at}")
        else:
            lines = [
                "I linked this form into the response database.",
                "",
                f"- Form: {form_title}",
                f"- Form ID: {target_form_id}",
                f"- Store: {store_label}",
                f"- Responses synced: {response_count}",
                f"- Answer rows synced: {answer_count}",
            ]
            if spreadsheet_id:
                lines.append(f"- Spreadsheet ID: {spreadsheet_id}")
            if synced_at:
                lines.append(f"- Synced at: {synced_at}")
        return AIMessage(content="\n".join(lines).strip())
    except Exception as exc:
        raise RuntimeError(
            (
                "ฉันตรวจพบว่านี่เป็นคำขอเชื่อมฟอร์มเข้าฐานข้อมูล แต่การซิงก์ข้อมูลล้มเหลว "
                if user_language == "th"
                else "I recognized this as a request to link a form into the database, but the sync failed. "
            )
            + f"Details: {exc}"
        ) from exc


@tool
def list_google_forms(query: str = "", limit: int = 20) -> str:
    """List the user's Google Forms."""
    drive_service = _build_drive_service()
    normalized_limit = max(1, min(int(limit), 100))
    mime_type = "application/vnd.google-apps.form"
    drive_query_parts = [f"mimeType='{mime_type}'", "trashed=false"]
    if query.strip():
        escaped_query = query.strip().replace("'", "\\'")
        drive_query_parts.append(f"name contains '{escaped_query}'")

    response = drive_service.files().list(
        q=" and ".join(drive_query_parts),
        pageSize=normalized_limit,
        fields="files(id,name,createdTime,modifiedTime,webViewLink)",
        orderBy="modifiedTime desc",
    ).execute()

    forms: list[dict[str, Any]] = []
    for entry in response.get("files", []) or []:
        if not isinstance(entry, dict):
            continue
        form_id = str(entry.get("id", "") or "")
        forms.append(
            {
                "formId": form_id,
                "title": str(entry.get("name", "") or ""),
                "editUrl": str(entry.get("webViewLink", "") or ""),
                "createdTime": str(entry.get("createdTime", "") or ""),
                "modifiedTime": str(entry.get("modifiedTime", "") or ""),
            }
        )

    return json.dumps(
        {
            "count": len(forms),
            "forms": forms,
        },
        ensure_ascii=False,
        indent=2,
    )


@tool
def inspect_form_response_database() -> str:
    """Describe the Postgres response-store schema used for agent-managed Google Forms."""
    return json.dumps(
        _response_store_schema_payload(),
        ensure_ascii=False,
        indent=2,
    )


@tool
def query_form_response_database(sql: str, row_limit: int = 200) -> str:
    """Run a bounded read-only Postgres query against the form response store."""
    return json.dumps(
        _execute_readonly_response_store_query(sql, row_limit=row_limit),
        ensure_ascii=False,
        indent=2,
    )


@tool
def inspect_spreadsheet_for_analysis(
    spreadsheet_target: str | None = None,
    spreadsheet_id: str | None = None,
    sheet_name: str | None = None,
    a1_range: str | None = None,
    max_sheets: int = 20,
    max_rows_per_sheet: int = 2000,
) -> str:
    """Inspect spreadsheet tabs and read the full used range for analysis without guessing sheet names.

    Accepts either:
    - spreadsheet_target: a spreadsheet URL or bare spreadsheet ID
    - spreadsheet_id: a bare spreadsheet ID

    Optional sheet_name and a1_range can narrow inspection, but full-workbook
    analysis remains the default when they are omitted.
    """
    target = (spreadsheet_target or spreadsheet_id or "").strip()
    if not target:
        raise RuntimeError(
            "inspect_spreadsheet_for_analysis requires either spreadsheet_target "
            "or spreadsheet_id."
        )

    spreadsheet_id_value = extract_spreadsheet_id(target)
    credentials = _load_google_workspace_credentials()
    service = build_google_api(
        "sheets",
        "v4",
        credentials=credentials,
        cache_discovery=False,
    )

    metadata = (
        service.spreadsheets()
        .get(
            spreadsheetId=spreadsheet_id_value,
            fields="properties.title,sheets.properties(sheetId,title,index,gridProperties)",
        )
        .execute()
    )
    spreadsheet_title = metadata.get("properties", {}).get("title", "")
    sheets = metadata.get("sheets", [])

    sheet_payloads: list[dict[str, Any]] = []
    normalized_sheet_name = (sheet_name or "").strip()
    normalized_a1_range = (a1_range or "").strip()
    selected_sheets = sheets[: max(1, max_sheets)]
    if normalized_sheet_name:
        selected_sheets = [
            sheet
            for sheet in selected_sheets
            if sheet.get("properties", {}).get("title", "") == normalized_sheet_name
        ]

    for sheet in selected_sheets:
        properties = sheet.get("properties", {})
        title = properties.get("title", "")
        if not title:
            continue

        if normalized_a1_range:
            full_range = f"{_quote_sheet_title(title)}!{normalized_a1_range}"
        else:
            full_range = _quote_sheet_title(title)
        values_response = (
            service.spreadsheets()
            .values()
            .get(spreadsheetId=spreadsheet_id_value, range=full_range)
            .execute()
        )
        values = values_response.get("values", [])
        row_count = len(values)
        truncated = row_count > max_rows_per_sheet
        returned_rows = values[:max_rows_per_sheet] if truncated else values

        sheet_payloads.append(
            {
                "sheet_title": title,
                "sheet_index": properties.get("index"),
                "sheet_id": properties.get("sheetId"),
                "used_range": values_response.get("range", full_range),
                "grid_row_count": properties.get("gridProperties", {}).get("rowCount"),
                "grid_column_count": properties.get("gridProperties", {}).get("columnCount"),
                "returned_row_count": len(returned_rows),
                "total_used_row_count": row_count,
                "truncated": truncated,
                "rows": returned_rows,
            }
        )

    return json.dumps(
        {
            "spreadsheet_id": spreadsheet_id_value,
            "spreadsheet_title": spreadsheet_title,
            "sheet_count": len(sheets),
            "analysis_scope": "all available sheet tabs up to configured limits",
            "requested_sheet_name": normalized_sheet_name or None,
            "requested_a1_range": normalized_a1_range or None,
            "sheets": sheet_payloads,
        },
        ensure_ascii=False,
        indent=2,
    )


def inject_form_creation_context(messages: list[AnyMessage]) -> list[AnyMessage]:
    """Rewrite form-creation requests into a canonical tool-first task."""
    next_messages = list(messages)
    for index in range(len(next_messages) - 1, -1, -1):
        message = next_messages[index]
        if message.type != "human":
            continue

        content = content_to_text(message.content)
        if not looks_like_form_creation_request(content):
            return next_messages
        if "FORM_CREATION_TASK" in content:
            return next_messages

        rewritten_content = compress_form_creation_request(content)
        next_messages[index] = message.model_copy(update={"content": rewritten_content})
        return next_messages

    return next_messages


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


def _sanitize_google_oauth_session_key(value: Any) -> str | None:
    """Normalize a user-scoped OAuth session key into a safe filename fragment."""
    if not isinstance(value, str):
        return None
    trimmed = value.strip()
    if not trimmed:
        return None
    normalized = re.sub(r"[^a-zA-Z0-9_-]", "", trimmed)
    if not normalized:
        return None
    return normalized[:128]


def _derive_google_oauth_token_path_for_session(session_key: str | None) -> Path:
    """Map a logical OAuth session key to its token file location."""
    configured = os.getenv("GOOGLE_OAUTH_TOKEN_PATH")
    base_path = Path(configured).expanduser() if configured else DEFAULT_GOOGLE_OAUTH_TOKEN_PATH
    if not session_key:
        return base_path
    return base_path.parent / "google-oauth-sessions" / f"{session_key}.json"


def _discover_single_google_oauth_session_token(base_path: Path) -> Path | None:
    """Return the lone session-scoped OAuth token file when exactly one exists."""
    sessions_dir = base_path.parent / "google-oauth-sessions"
    if not sessions_dir.exists() or not sessions_dir.is_dir():
        return None

    candidates = sorted(
        candidate
        for candidate in sessions_dir.glob("*.json")
        if candidate.is_file()
    )
    if len(candidates) == 1:
        return candidates[0]
    return None


def get_google_oauth_token_path_for_mcp() -> Path:
    """Prefer the active or lone session-scoped OAuth token for MCP bootstraps."""
    session_key = GOOGLE_OAUTH_SESSION_KEY.get()
    if session_key:
        return _derive_google_oauth_token_path_for_session(session_key)

    configured = os.getenv("GOOGLE_OAUTH_TOKEN_PATH")
    base_path = Path(configured).expanduser() if configured else DEFAULT_GOOGLE_OAUTH_TOKEN_PATH
    discovered = _discover_single_google_oauth_session_token(base_path)
    if discovered is not None:
        return discovered
    return base_path


def get_google_oauth_token_path(*, discover_single_session: bool = False) -> Path:
    """Return the OAuth token file path used by the web UI and backend."""
    session_key = GOOGLE_OAUTH_SESSION_KEY.get()
    token_path = _derive_google_oauth_token_path_for_session(session_key)
    if session_key:
        return token_path
    if token_path.exists():
        return token_path

    if discover_single_session:
        discovered = _discover_single_google_oauth_session_token(token_path)
        if discovered is not None:
            return discovered
    return token_path


def _load_google_refresh_token_from_file(token_path: Path) -> str | None:
    """Read a refresh token directly from an authorized-user token file."""
    if not token_path.exists():
        return None

    try:
        payload = json.loads(token_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    refresh_token = payload.get("refresh_token")
    if isinstance(refresh_token, str) and refresh_token.strip():
        return refresh_token.strip()
    return None


def load_google_refresh_token(*, discover_single_session: bool = True) -> str | None:
    """Load the Google refresh token from env first, then shared OAuth storage."""
    env_token = os.getenv("GOOGLE_REFRESH_TOKEN")
    if env_token:
        return env_token

    token_path = get_google_oauth_token_path(
        discover_single_session=discover_single_session
    )
    return _load_google_refresh_token_from_file(token_path)


def load_shared_google_oauth_scopes() -> set[str]:
    """Load granted scopes from the shared Google OAuth token file when available."""
    token_path = get_google_oauth_token_path(discover_single_session=True)
    if not token_path.exists():
        return set()

    try:
        payload = json.loads(token_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()

    scopes = payload.get("scopes")
    if isinstance(scopes, list):
        return {str(scope).strip() for scope in scopes if str(scope).strip()}
    scope = payload.get("scope")
    if isinstance(scope, str):
        return {item.strip() for item in scope.split(" ") if item.strip()}
    return set()


def has_shared_google_oauth_token(*, discover_single_session: bool = False) -> bool:
    """Return whether the shared Google OAuth token file exists."""
    return get_google_oauth_token_path(
        discover_single_session=discover_single_session
    ).exists()


def has_google_sheets_auth_config() -> bool:
    """Return whether Sheets MCP has any usable auth source configured."""
    if os.getenv("CREDENTIALS_CONFIG"):
        return True
    if get_google_oauth_token_path_for_mcp().exists():
        return True
    if build_google_sheets_oauth_client_config() is not None:
        return True
    for env_name in ("SERVICE_ACCOUNT_PATH", "CREDENTIALS_PATH", "TOKEN_PATH"):
        env_value = os.getenv(env_name)
        if env_value and Path(env_value).expanduser().exists():
            return True
    return False


def build_google_sheets_oauth_client_config() -> dict[str, Any] | None:
    """Build an OAuth client config payload for mcp-google-sheets from app env."""
    client_id = os.getenv("GOOGLE_CLIENT_ID", "").strip()
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        return None

    return {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }


def ensure_google_sheets_oauth_credentials_file() -> Path | None:
    """Persist an OAuth client credentials file for the Sheets MCP when needed."""
    payload = build_google_sheets_oauth_client_config()
    if payload is None:
        return None

    GOOGLE_SHEETS_MCP_CREDENTIALS_PATH.parent.mkdir(parents=True, exist_ok=True)
    GOOGLE_SHEETS_MCP_CREDENTIALS_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return GOOGLE_SHEETS_MCP_CREDENTIALS_PATH


def normalize_openai_base_url(base_url: str) -> str:
    """Normalize local OpenAI-compatible base URLs such as Ollama endpoints."""
    normalized = base_url.strip().rstrip("/")
    if not normalized.startswith(("http://", "https://")):
        normalized = f"http://{normalized}"
    if not normalized.endswith("/v1"):
        normalized = f"{normalized}/v1"
    return normalized


def is_env_truthy(name: str) -> bool:
    """Interpret common true-like env values."""
    value = os.getenv(name, "").strip().lower()
    return value in {"1", "true", "yes", "on"}


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
        segments = extract_docx_segments(file_bytes)
        text = serialize_docx_segments_to_text(segments) if segments else extract_docx_text(file_bytes)
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
    """Extract structured text from a DOCX using only the Python standard library."""
    try:
        with zipfile.ZipFile(io.BytesIO(file_bytes)) as docx:
            xml_bytes = docx.read("word/document.xml")
            media_names = {
                name.rsplit("/", 1)[-1]
                for name in docx.namelist()
                if name.startswith("word/media/")
            }
    except Exception:
        return ""

    try:
        root = ElementTree.fromstring(xml_bytes)
    except ElementTree.ParseError:
        return ""

    namespace = {
        "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
        "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
        "pic": "http://schemas.openxmlformats.org/drawingml/2006/picture",
        "wp": "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing",
        "v": "urn:schemas-microsoft-com:vml",
        "o": "urn:schemas-microsoft-com:office:office",
        "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    }

    def paragraph_style_prefix(paragraph: ElementTree.Element) -> str:
        style = paragraph.find("./w:pPr/w:pStyle", namespace)
        style_val = style.get(f"{{{namespace['w']}}}val", "") if style is not None else ""
        if not style_val:
            return ""
        lowered = style_val.casefold()
        if lowered.startswith("heading"):
            level_match = re.search(r"(\d+)", style_val)
            level = max(1, min(6, int(level_match.group(1)))) if level_match else 1
            return "#" * level + " "
        if lowered in {"title", "subtitle"}:
            return "# "
        return ""

    def extract_image_markers(element: ElementTree.Element) -> list[str]:
        markers: list[str] = []

        for drawing in element.findall(".//w:drawing", namespace):
            doc_prop = drawing.find(".//wp:docPr", namespace)
            pic_prop = drawing.find(".//pic:cNvPr", namespace)
            label = ""
            for node in (doc_prop, pic_prop):
                if node is None:
                    continue
                label = (
                    node.get("descr", "").strip()
                    or node.get("title", "").strip()
                    or node.get("name", "").strip()
                )
                if label:
                    break
            markers.append(f"[Embedded image: {label}]" if label else "[Embedded image]")

        for shape in element.findall(".//v:shape", namespace):
            label = (
                shape.get("alt", "").strip()
                or shape.get("title", "").strip()
                or shape.get("id", "").strip()
            )
            imagedata_nodes = shape.findall(".//v:imagedata", namespace)
            if imagedata_nodes:
                for _ in imagedata_nodes:
                    markers.append(f"[Embedded image: {label}]" if label else "[Embedded image]")
            else:
                markers.append(f"[Embedded image: {label}]" if label else "[Embedded image]")

        if not markers:
            blips = element.findall(".//a:blip", namespace)
            imagedata = element.findall(".//v:imagedata", namespace)
            image_refs = len(blips) + len(imagedata)
            if image_refs:
                markers.extend("[Embedded image]" for _ in range(image_refs))
        return markers

    def render_paragraph(paragraph: ElementTree.Element) -> list[str]:
        runs = [
            text_node.text or ""
            for text_node in paragraph.findall(".//w:t", namespace)
        ]
        text = "".join(runs).strip()
        markers = extract_image_markers(paragraph)
        lines: list[str] = []
        if text:
            lines.append(f"{paragraph_style_prefix(paragraph)}{text}".strip())
        lines.extend(markers)
        return lines

    def render_table(table: ElementTree.Element) -> list[str]:
        lines: list[str] = ["[Table]"]
        for row in table.findall("./w:tr", namespace):
            cells: list[str] = []
            for cell in row.findall("./w:tc", namespace):
                cell_parts: list[str] = []
                for child in list(cell):
                    tag = child.tag.rsplit("}", 1)[-1]
                    if tag == "p":
                        cell_parts.extend(render_paragraph(child))
                    elif tag == "tbl":
                        cell_parts.extend(render_table(child))
                cell_text = " ".join(part.strip() for part in cell_parts if part.strip()).strip()
                cells.append(cell_text)
            if any(cells):
                lines.append(" | ".join(cell or " " for cell in cells))
        return lines

    body = root.find("./w:body", namespace)
    if body is None:
        return ""

    parts: list[str] = []
    for child in list(body):
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            parts.extend(render_paragraph(child))
        elif tag == "tbl":
            parts.extend(render_table(child))

    if media_names and not any("[Embedded image" in part for part in parts):
        parts.append(f"[Embedded images: {len(media_names)}]")

    cleaned_parts = [part.strip() for part in parts if part and part.strip()]
    return "\n".join(cleaned_parts).strip()


def _serialize_embedded_image_block(image: dict[str, Any]) -> str:
    """Serialize one embedded DOCX image into a stable hidden-text block."""
    name = str(image.get("name", "") or "").replace('"', "'").strip()
    alt_text = str(image.get("alt_text", "") or "").replace('"', "'").strip()
    mime_type = str(image.get("mime_type", "") or "application/octet-stream").strip()
    data_base64 = str(image.get("data_base64", "") or "").strip()
    meta_parts = []
    if name:
        meta_parts.append(f'name="{name}"')
    if mime_type:
        meta_parts.append(f'mime="{mime_type}"')
    if alt_text:
        meta_parts.append(f'alt="{alt_text}"')
    meta = " " + " ".join(meta_parts) if meta_parts else ""
    return (
        f"<<<EMBEDDED_IMAGE{meta}>>>\n"
        f"{data_base64}\n"
        "<<<END_EMBEDDED_IMAGE>>>"
    )


def serialize_docx_segments_to_text(segments: list[dict[str, Any]]) -> str:
    """Render extracted DOCX segments to hidden text while preserving image position."""
    rendered_parts: list[str] = []
    for segment in segments:
        text = str(segment.get("text", "") or "").strip()
        images = segment.get("images", [])
        if text:
            rendered_parts.append(text)
        if isinstance(images, list):
            for image in images:
                if isinstance(image, dict) and str(image.get("data_base64", "") or "").strip():
                    rendered_parts.append(_serialize_embedded_image_block(image))
    return "\n".join(part for part in rendered_parts if part).strip()


def extract_docx_segments(file_bytes: bytes) -> list[dict[str, Any]]:
    """Extract ordered DOCX text/image segments for higher-fidelity form reconstruction."""
    try:
        with zipfile.ZipFile(io.BytesIO(file_bytes)) as docx:
            xml_bytes = docx.read("word/document.xml")
            try:
                rels_bytes = docx.read("word/_rels/document.xml.rels")
            except KeyError:
                rels_bytes = b""

            media_bytes_by_name = {
                name.rsplit("/", 1)[-1]: docx.read(name)
                for name in docx.namelist()
                if name.startswith("word/media/")
            }
    except Exception:
        return []

    try:
        root = ElementTree.fromstring(xml_bytes)
    except ElementTree.ParseError:
        return []

    rel_namespace = {
        "rel": "http://schemas.openxmlformats.org/package/2006/relationships"
    }
    relationship_targets: dict[str, str] = {}
    if rels_bytes:
        try:
            rel_root = ElementTree.fromstring(rels_bytes)
            for rel in rel_root.findall(".//rel:Relationship", rel_namespace):
                rel_id = rel.get("Id", "").strip()
                target = rel.get("Target", "").strip()
                if rel_id and target:
                    relationship_targets[rel_id] = target.rsplit("/", 1)[-1]
        except ElementTree.ParseError:
            relationship_targets = {}

    namespace = {
        "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
        "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
        "pic": "http://schemas.openxmlformats.org/drawingml/2006/picture",
        "wp": "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing",
        "v": "urn:schemas-microsoft-com:vml",
        "o": "urn:schemas-microsoft-com:office:office",
        "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    }

    def paragraph_style_prefix(paragraph: ElementTree.Element) -> str:
        style = paragraph.find("./w:pPr/w:pStyle", namespace)
        style_val = style.get(f"{{{namespace['w']}}}val", "") if style is not None else ""
        if not style_val:
            return ""
        lowered = style_val.casefold()
        if lowered.startswith("heading"):
            level_match = re.search(r"(\d+)", style_val)
            level = max(1, min(6, int(level_match.group(1)))) if level_match else 1
            return "#" * level + " "
        if lowered in {"title", "subtitle"}:
            return "# "
        return ""

    def build_image_asset(
        rel_id: str | None,
        label: str | None,
        fallback_index: int,
    ) -> dict[str, Any] | None:
        rel_id = (rel_id or "").strip()
        media_name = relationship_targets.get(rel_id, "")
        if not media_name:
            return None
        image_bytes = media_bytes_by_name.get(media_name)
        if not image_bytes:
            return None

        extension = Path(media_name).suffix.casefold()
        mime_type = IMAGE_MIME_BY_EXTENSION.get(extension, "application/octet-stream")
        cleaned_label = (label or "").strip()
        alt_text = cleaned_label or f"Embedded image {fallback_index}"
        return {
            "name": media_name,
            "alt_text": alt_text,
            "mime_type": mime_type,
            "data_base64": base64.b64encode(image_bytes).decode("ascii"),
        }

    image_counter = 0

    def extract_paragraph_images(element: ElementTree.Element) -> list[dict[str, Any]]:
        nonlocal image_counter
        images: list[dict[str, Any]] = []

        for drawing in element.findall(".//w:drawing", namespace):
            doc_prop = drawing.find(".//wp:docPr", namespace)
            blip = drawing.find(".//a:blip", namespace)
            rel_id = blip.get(f"{{{namespace['r']}}}embed", "").strip() if blip is not None else ""
            label = ""
            if doc_prop is not None:
                label = (
                    doc_prop.get("descr", "").strip()
                    or doc_prop.get("title", "").strip()
                    or doc_prop.get("name", "").strip()
                )
            asset = build_image_asset(rel_id, label, image_counter + 1)
            if asset:
                image_counter += 1
                images.append(asset)

        for shape in element.findall(".//v:shape", namespace):
            label = (
                shape.get("alt", "").strip()
                or shape.get("title", "").strip()
                or shape.get("id", "").strip()
            )
            for node in shape.findall(".//v:imagedata", namespace):
                rel_id = (
                    node.get(f"{{{namespace['r']}}}id", "").strip()
                    or node.get(f"{{{namespace['o']}}}relid", "").strip()
                )
                asset = build_image_asset(rel_id, label, image_counter + 1)
                if asset:
                    image_counter += 1
                    images.append(asset)

        deduped: list[dict[str, Any]] = []
        seen_keys: set[tuple[str, str]] = set()
        for image in images:
            key = (
                str(image.get("name", "")),
                str(image.get("data_base64", ""))[:32],
            )
            if key in seen_keys:
                continue
            seen_keys.add(key)
            deduped.append(image)
        return deduped

    def render_paragraph(paragraph: ElementTree.Element) -> list[dict[str, Any]]:
        runs = [
            text_node.text or ""
            for text_node in paragraph.findall(".//w:t", namespace)
        ]
        text = "".join(runs).strip()
        images = extract_paragraph_images(paragraph)
        if not text and not images:
            return []
        return [
            {
                "text": f"{paragraph_style_prefix(paragraph)}{text}".strip(),
                "images": images,
            }
        ]

    def render_table(table: ElementTree.Element) -> list[dict[str, Any]]:
        segments: list[dict[str, Any]] = [{"text": "[Table]", "images": []}]
        for row in table.findall("./w:tr", namespace):
            cell_texts: list[str] = []
            row_images: list[dict[str, Any]] = []
            for cell in row.findall("./w:tc", namespace):
                cell_parts: list[str] = []
                for child in list(cell):
                    tag = child.tag.rsplit("}", 1)[-1]
                    if tag == "p":
                        for segment in render_paragraph(child):
                            if segment["text"]:
                                cell_parts.append(segment["text"])
                            row_images.extend(segment.get("images", []))
                    elif tag == "tbl":
                        for segment in render_table(child):
                            if segment["text"]:
                                cell_parts.append(segment["text"])
                            row_images.extend(segment.get("images", []))
                cell_texts.append(" ".join(part.strip() for part in cell_parts if part.strip()).strip())
            if any(cell_texts) or row_images:
                segments.append(
                    {
                        "text": " | ".join(cell or " " for cell in cell_texts).strip(),
                        "images": row_images,
                    }
                )
        return segments

    body = root.find("./w:body", namespace)
    if body is None:
        return []

    segments: list[dict[str, Any]] = []
    for child in list(body):
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            segments.extend(render_paragraph(child))
        elif tag == "tbl":
            segments.extend(render_table(child))
    return [segment for segment in segments if segment.get("text") or segment.get("images")]


def _normalize_match_text(text: str) -> str:
    """Normalize text for loose matching between extracted source and parsed questions."""
    lowered = str(text or "").casefold()
    lowered = re.sub(r"\s+", " ", lowered)
    lowered = lowered.replace("â€œ", '"').replace("â€", '"')
    lowered = lowered.replace("â€˜", "'").replace("â€™", "'")
    lowered = lowered.replace("\u00a0", " ")
    return lowered.strip(" .:-")


def _extract_option_marker(text: str) -> tuple[str, str] | None:
    """Extract a leading Thai/English option marker from a line."""
    match = re.match(r"^\s*([A-Da-d]|[\u0E01-\u0E2E])[.)]\s*(.*)$", str(text or "").strip())
    if not match:
        return None
    return match.group(1).strip(), match.group(2).strip()


def _option_label_for_index(index: int) -> str:
    """Return a human-readable option label for the given zero-based index."""
    thai_labels = ["ก", "ข", "ค", "ง", "จ", "ฉ", "ช", "ซ"]
    if 0 <= index < len(thai_labels):
        return thai_labels[index]
    return chr(ord("A") + index)


def _build_unique_choice_options(
    raw_options: list[Any],
) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
    """Build Forms choice options while guaranteeing unique visible values."""
    options_payload: list[dict[str, Any]] = []
    answer_value_map: dict[str, list[str]] = {}
    used_values: set[str] = set()

    for option_index, option in enumerate(raw_options):
        original_value = ""
        option_label = _option_label_for_index(option_index)
        option_payload: dict[str, Any] = {}

        if isinstance(option, dict):
            original_value = _sanitize_display_text(option.get("value", ""))
            option_label = str(option.get("label", "") or option_label).strip() or option_label
            if not original_value:
                continue
            option_images = option.get("images", [])
            if isinstance(option_images, list) and option_images:
                first_image = option_images[0]
                if isinstance(first_image, dict):
                    source_uri = str(first_image.get("source_uri", "") or "").strip()
                    if source_uri:
                        option_payload["image"] = {"sourceUri": source_uri}
                        alt_text = str(first_image.get("alt_text", "") or "").strip()
                        if alt_text:
                            option_payload["image"]["altText"] = alt_text
        else:
            original_value = _sanitize_display_text(option)
            if not original_value:
                continue

        unique_value = original_value
        if unique_value in used_values:
            if not unique_value.startswith(f"{option_label}. "):
                unique_value = f"{option_label}. {original_value}"
            suffix = 2
            while unique_value in used_values:
                unique_value = f"{original_value} ({suffix})"
                suffix += 1

        option_payload["value"] = unique_value
        options_payload.append(option_payload)
        used_values.add(unique_value)

        normalized_original_value = _normalize_match_text(original_value)
        if normalized_original_value:
            answer_value_map.setdefault(normalized_original_value, []).append(unique_value)

    return options_payload, answer_value_map


def _remap_correct_answers_for_choice_values(
    correct_answers: list[str],
    answer_value_map: dict[str, list[str]],
) -> list[str]:
    """Map correct answers onto the final visible option values used in Forms."""
    remapped_answers: list[str] = []
    seen_answers: set[str] = set()
    for answer in correct_answers:
        normalized_answer = _normalize_match_text(answer)
        if not normalized_answer:
            continue
        candidates = answer_value_map.get(normalized_answer, [])
        if not candidates:
            continue
        candidate = candidates[0]
        if candidate in seen_answers:
            continue
        remapped_answers.append(candidate)
        seen_answers.add(candidate)
    return remapped_answers


def _is_visual_separator_line(text: str) -> bool:
    """Return whether a line is only a decorative separator from the source document."""
    stripped = str(text or "").strip()
    if not stripped:
        return False
    normalized = _normalize_match_text(stripped)
    if not normalized:
        return False
    if normalized.startswith("%="):
        return True
    return bool(re.fullmatch(r"[%=~_\-]{8,}", stripped))


def _sanitize_display_text(text: Any) -> str:
    """Flatten text for Google Forms displayed fields that cannot contain newlines."""
    value = str(text or "").strip()
    if not value:
        return ""
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def _sanitize_multiline_display_text(text: Any) -> str:
    """Normalize multiline text while preserving line breaks."""
    value = str(text or "").strip()
    if not value:
        return ""
    lines = [
        re.sub(r"\s+", " ", line).strip()
        for line in value.replace("\r\n", "\n").split("\n")
    ]
    return "\n".join(line for line in lines if line)


def _sanitize_forms_payload(value: Any, key: str | None = None) -> Any:
    """Recursively sanitize displayed text fields before sending requests to Forms."""
    if isinstance(value, dict):
        return {
            sub_key: _sanitize_forms_payload(sub_value, sub_key)
            for sub_key, sub_value in value.items()
        }
    if isinstance(value, list):
        return [_sanitize_forms_payload(item, key) for item in value]
    if isinstance(value, str):
        if key == "description":
            return _sanitize_multiline_display_text(value)
        if key in {"title", "value", "altText"}:
            return _sanitize_display_text(value)
        return value
    return value


def _match_next_question_index(
    normalized_segment_text: str,
    current_question_index: int,
    normalized_titles: list[str],
) -> int | None:
    """Return the next question index when a segment text matches the upcoming question title."""
    next_index = current_question_index + 1
    if next_index >= len(normalized_titles):
        return None
    target = normalized_titles[next_index]
    candidate_texts = [normalized_segment_text]
    stripped_numbering = re.sub(r"^\d+[.)]\s*", "", normalized_segment_text).strip()
    if stripped_numbering and stripped_numbering not in candidate_texts:
        candidate_texts.append(stripped_numbering)

    if target and any(
        candidate == target
        or candidate.startswith(target)
        or target.startswith(candidate)
        for candidate in candidate_texts
        if candidate
    ):
        return next_index
    return None


def _question_title_refers_to_image(title: str) -> bool:
    """Detect question titles that explicitly refer to an accompanying image."""
    normalized = _normalize_match_text(title)
    image_cues = (
        "จากภาพ",
        "ภาพด้านบน",
        "ภาพต่อไปนี้",
        "จากรูป",
        "รูปด้านบน",
        "รูปต่อไปนี้",
        "ภาพข้างล่าง",
        "ภาพข้างล่างนี้",
        "รูปข้างล่าง",
        "รูปข้างล่างนี้",
        "ตามที่ปรากฏในภาพ",
        "ตามที่ปรากฏในรูป",
        "ดูภาพ",
        "ดูรูป",
    )
    return any(cue in normalized for cue in image_cues)


def _is_docx_run_answer_signaled(run: ElementTree.Element, namespace: dict[str, str]) -> bool:
    """Return whether a DOCX run uses a salient answer-key style."""
    run_properties = run.find("./w:rPr", namespace)
    if run_properties is None:
        return False

    highlight = run_properties.find("./w:highlight", namespace)
    if highlight is not None:
        highlight_value = str(highlight.get(f"{{{namespace['w']}}}val", "") or "").strip().casefold()
        if highlight_value and highlight_value not in {"none", "default"}:
            return True

    shading = run_properties.find("./w:shd", namespace)
    if shading is not None:
        fill_value = str(shading.get(f"{{{namespace['w']}}}fill", "") or "").strip().casefold()
        if fill_value and fill_value not in {"auto", "ffffff", "000000"}:
            return True

    color = run_properties.find("./w:color", namespace)
    if color is not None:
        color_value = str(color.get(f"{{{namespace['w']}}}val", "") or "").strip().casefold()
        if color_value and color_value not in {"auto", "000000", "ffffff"}:
            return True

    return False


def _extract_docx_answer_signal_paragraphs(file_bytes: bytes) -> list[dict[str, str]]:
    """Extract paragraph text with salient styled fragments used to mark correct answers."""
    try:
        with zipfile.ZipFile(io.BytesIO(file_bytes)) as docx:
            xml_bytes = docx.read("word/document.xml")
    except Exception:
        return []

    try:
        root = ElementTree.fromstring(xml_bytes)
    except ElementTree.ParseError:
        return []

    namespace = {
        "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    }

    paragraphs: list[dict[str, str]] = []
    for paragraph in root.findall(".//w:p", namespace):
        all_text_parts: list[str] = []
        signaled_parts: list[str] = []
        for run in paragraph.findall("./w:r", namespace):
            run_text = "".join(
                text_node.text or ""
                for text_node in run.findall(".//w:t", namespace)
            )
            if not run_text:
                continue
            all_text_parts.append(run_text)
            if _is_docx_run_answer_signaled(run, namespace):
                signaled_parts.append(run_text)

        paragraph_text = "".join(all_text_parts).strip()
        signaled_text = "".join(signaled_parts).strip()
        if paragraph_text or signaled_text:
            paragraphs.append(
                {
                    "text": paragraph_text,
                    "signaled_text": signaled_text,
                }
            )

    return paragraphs


def _split_labeled_option_pairs(text: str) -> list[tuple[str, str]]:
    """Split a line that may contain inline Thai/English labeled options."""
    pattern = re.compile(r"([A-Da-d]|[\u0E01-\u0E2E])[.)]\s*")
    matches = list(pattern.finditer(str(text or "").strip()))
    if not matches:
        return []

    pairs: list[tuple[str, str]] = []
    for index, match in enumerate(matches):
        label = match.group(1).strip()
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = str(text[start:end]).strip()
        if body:
            pairs.append((label, body))
    return pairs


def _highlight_matches_option(highlighted_text: str, option_label: str, option_value: str) -> bool:
    """Return whether highlighted text corresponds to the given option."""
    normalized_highlight = _normalize_match_text(highlighted_text)
    if not normalized_highlight:
        return False

    normalized_label = _normalize_match_text(option_label)
    normalized_value = _normalize_match_text(option_value)

    if normalized_label and normalized_label in normalized_highlight:
        return True
    if normalized_value and (
        normalized_highlight in normalized_value
        or normalized_value in normalized_highlight
    ):
        return True
    return False


def _apply_docx_correct_answers(
    questions: list[dict[str, Any]],
    file_bytes: bytes,
) -> list[dict[str, Any]]:
    """Attach correct answers from highlighted DOCX option text to parsed questions."""
    paragraphs = _extract_docx_answer_signal_paragraphs(file_bytes)
    if not paragraphs or not questions:
        return questions

    normalized_titles = [_normalize_match_text(question.get("title", "")) for question in questions]
    question_index = -1

    for paragraph in paragraphs:
        paragraph_text = str(paragraph.get("text", "") or "").strip()
        highlighted_text = str(paragraph.get("signaled_text", "") or "").strip()
        normalized_paragraph_text = _normalize_match_text(paragraph_text)

        if normalized_paragraph_text:
            next_index = _match_next_question_index(
                normalized_paragraph_text,
                question_index,
                normalized_titles,
            )
            if next_index is not None:
                question_index = next_index
                continue

        if question_index < 0 or question_index >= len(questions) or not highlighted_text:
            continue

        question = questions[question_index]
        options = question.get("options", [])
        if not isinstance(options, list) or not options:
            continue

        matched_labels: list[str] = []
        inline_pairs = _split_labeled_option_pairs(paragraph_text)
        if inline_pairs:
            for label, body in inline_pairs:
                if _highlight_matches_option(highlighted_text, label, body):
                    matched_labels.append(label.casefold())
        else:
            option_match = _extract_option_marker(paragraph_text)
            if option_match:
                option_label, option_body = option_match
                if _highlight_matches_option(highlighted_text, option_label, option_body or paragraph_text):
                    matched_labels.append(option_label.casefold())

        if not matched_labels:
            for option in options:
                if not isinstance(option, dict):
                    continue
                option_label = str(option.get("label", "") or "").strip()
                option_value = str(option.get("value", "") or "").strip()
                if _highlight_matches_option(highlighted_text, option_label, option_value):
                    matched_labels.append(option_label.casefold())

        if not matched_labels:
            continue

        correct_answers = list(question.get("correct_answers", [])) if isinstance(question.get("correct_answers", []), list) else []
        seen_answers = {
            _normalize_match_text(answer)
            for answer in correct_answers
            if str(answer or "").strip()
        }
        for option in options:
            if not isinstance(option, dict):
                continue
            option_label = str(option.get("label", "") or "").strip().casefold()
            option_value = str(option.get("value", "") or "").strip()
            if option_label not in matched_labels or not option_value:
                continue
            normalized_value = _normalize_match_text(option_value)
            if normalized_value in seen_answers:
                continue
            correct_answers.append(option_value)
            seen_answers.add(normalized_value)

        if correct_answers:
            question["correct_answers"] = correct_answers
            question.setdefault("point_value", 1)

    return questions


def extract_docx_questions_with_images(file_bytes: bytes) -> list[dict[str, Any]]:
    """Extract questions from a DOCX and attach embedded image assets to the nearest question."""
    segments = extract_docx_segments(file_bytes)
    if not segments:
        return []

    text = "\n".join(
        segment.get("text", "").strip()
        for segment in segments
        if str(segment.get("text", "")).strip()
    ).strip()
    if not text:
        return []

    questions = extract_questions_from_reference_text(text)
    if not questions:
        return []

    normalized_titles = [_normalize_match_text(question.get("title", "")) for question in questions]
    for question in questions:
        raw_options = question.get("options", [])
        structured_options: list[dict[str, Any]] = []
        if isinstance(raw_options, list):
            for option_index, option in enumerate(raw_options):
                if isinstance(option, dict):
                    structured_option = dict(option)
                    structured_option.setdefault("images", [])
                    structured_option.setdefault("extra_images", [])
                    structured_options.append(structured_option)
                    continue
                structured_options.append(
                    {
                        "value": str(option or "").strip(),
                        "label": _option_label_for_index(option_index),
                        "images": [],
                        "extra_images": [],
                    }
                )
        question["options"] = structured_options

    question_index = -1
    current_option_index: int | None = None

    for segment_index, segment in enumerate(segments):
        segment_text = str(segment.get("text", "") or "").strip()
        if _is_visual_separator_line(segment_text):
            continue

        normalized_segment_text = _normalize_match_text(segment_text)
        if normalized_segment_text:
            next_index = _match_next_question_index(
                normalized_segment_text,
                question_index,
                normalized_titles,
            )
            if next_index is not None:
                question_index = next_index
                current_option_index = None
                continue

            if question_index >= 0:
                option_match = _extract_option_marker(segment_text)
                if option_match:
                    option_label, option_body = option_match
                    options = questions[question_index].get("options", [])
                    resolved_option_index: int | None = None
                    if isinstance(options, list):
                        for candidate_index, option in enumerate(options):
                            if not isinstance(option, dict):
                                continue
                            label = str(option.get("label", "") or "").strip()
                            value = _normalize_match_text(option.get("value", ""))
                            if label.casefold() == option_label.casefold():
                                resolved_option_index = candidate_index
                                if option_body:
                                    normalized_body = _normalize_match_text(option_body)
                                    if normalized_body and value and normalized_body not in value and value not in normalized_body:
                                        continue
                                break
                    current_option_index = resolved_option_index
                else:
                    current_option_index = None

        images = segment.get("images", [])
        if not images or question_index < 0 or question_index >= len(questions):
            continue

        if not normalized_segment_text and current_option_index is not None:
            lookahead_index = segment_index + 1
            while lookahead_index < len(segments):
                lookahead_text = str(segments[lookahead_index].get("text", "") or "").strip()
                normalized_lookahead_text = _normalize_match_text(lookahead_text)
                if not normalized_lookahead_text or _is_visual_separator_line(lookahead_text):
                    lookahead_index += 1
                    continue
                next_question_index = _match_next_question_index(
                    normalized_lookahead_text,
                    question_index,
                    normalized_titles,
                )
                if (
                    next_question_index is not None
                    and _question_title_refers_to_image(questions[next_question_index].get("title", ""))
                ):
                    question_index = next_question_index
                    current_option_index = None
                break

        if current_option_index is not None:
            options = questions[question_index].get("options", [])
            if isinstance(options, list) and 0 <= current_option_index < len(options):
                option = options[current_option_index]
                if isinstance(option, dict):
                    option_images = option.setdefault("images", [])
                    extra_images = option.setdefault("extra_images", [])
                    for image in images:
                        if not isinstance(image, dict):
                            continue
                        image_name = str(image.get("name", "") or "")
                        existing = [
                            *[
                                str(existing_image.get("name", "") or "")
                                for existing_image in option_images
                                if isinstance(existing_image, dict)
                            ],
                            *[
                                str(existing_image.get("name", "") or "")
                                for existing_image in extra_images
                                if isinstance(existing_image, dict)
                            ],
                        ]
                        if image_name in existing:
                            continue
                        if not option_images:
                            option_images.append(image)
                        else:
                            extra_images.append(image)
                    continue

        destination = questions[question_index].setdefault("images", [])
        if not isinstance(destination, list):
            destination = []
            questions[question_index]["images"] = destination

        for image in images:
            if not isinstance(image, dict):
                continue
            image_name = str(image.get("name", "") or "")
            if any(str(existing.get("name", "") or "") == image_name for existing in destination):
                continue
            destination.append(image)

    return _apply_docx_correct_answers(questions, file_bytes)


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


def get_google_oauth_session_key_from_request(request: ModelRequest) -> str | None:
    """Read the user-scoped Google OAuth session key from runtime or state context."""
    def _find_session_key(value: Any, depth: int = 0) -> str | None:
        if depth > 6:
            return None
        if isinstance(value, dict):
            direct = _sanitize_google_oauth_session_key(
                value.get("google_oauth_session_key")
            )
            if direct:
                return direct
            for nested in value.values():
                found = _find_session_key(nested, depth + 1)
                if found:
                    return found
            return None
        if isinstance(value, (list, tuple)):
            for item in value:
                found = _find_session_key(item, depth + 1)
                if found:
                    return found
            return None
        if hasattr(value, "get"):
            try:
                direct = _sanitize_google_oauth_session_key(
                    value.get("google_oauth_session_key")
                )
                if direct:
                    return direct
            except Exception:
                pass
        object_dict = getattr(value, "__dict__", None)
        if isinstance(object_dict, dict):
            found = _find_session_key(object_dict, depth + 1)
            if found:
                return found
        return None

    runtime = getattr(request, "runtime", None)
    runtime_context = getattr(runtime, "context", None)
    found = _find_session_key(runtime_context)
    if found:
        return found

    found = _find_session_key(getattr(request, "state", None))
    if found:
        return found

    found = _find_session_key(getattr(request, "config", None))
    if found:
        return found

    found = _find_session_key(getattr(runtime, "config", None))
    if found:
        return found

    found = _find_session_key(getattr(request, "configurable", None))
    if found:
        return found

    found = _find_session_key(getattr(request, "metadata", None))
    if found:
        return found

    return None


def run_with_google_oauth_session(
    google_oauth_session_key: str | None,
    func: Callable[..., Any],
    *args: Any,
    **kwargs: Any,
) -> Any:
    """Execute sync work with an explicit Google OAuth session binding."""
    token_session = GOOGLE_OAUTH_SESSION_KEY.set(
        _sanitize_google_oauth_session_key(google_oauth_session_key)
    )
    try:
        return func(*args, **kwargs)
    finally:
        GOOGLE_OAUTH_SESSION_KEY.reset(token_session)


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


def inject_spreadsheet_target_context(messages: list[AnyMessage]) -> list[AnyMessage]:
    """Rewrite spreadsheet-analysis requests into a canonical Sheets-first task."""
    next_messages = list(messages)
    for index in range(len(next_messages) - 1, -1, -1):
        message = next_messages[index]
        if message.type != "human":
            continue

        content = content_to_text(message.content)
        targets = extract_spreadsheet_targets(content)
        if not targets:
            return next_messages
        if "SPREADSHEET_TASK" in content:
            return next_messages

        cleaned_request = strip_spreadsheet_targets(content, targets)
        request_summary = cleaned_request or "Analyze the spreadsheet data."
        if not looks_like_spreadsheet_analysis_request(request_summary):
            request_summary = f"Analyze the spreadsheet data. User request: {request_summary}".strip()

        aliases = build_spreadsheet_alias_map(targets)
        alias_lines = "\n".join(
            f"- {alias} => opaque spreadsheet target `{target}`"
            for alias, target in aliases
        )
        rewritten_content = (
            "SPREADSHEET_TASK\n"
            "Use the skill: google-sheets-form-response-analysis\n"
            "This is a spreadsheet-analysis request.\n"
            "This is NOT a Google Forms creation request.\n"
            "Treat spreadsheet identifiers as opaque handles. Never interpret their "
            "characters as natural-language content, Thai text, model names, "
            "versions, answer choices, or commands.\n"
            "Spreadsheet targets:\n"
            f"{alias_lines}\n"
            f"Requested task: {request_summary}\n"
            "Required behavior:\n"
            "1. Inspect the spreadsheet structure first using inspect_spreadsheet_for_analysis.\n"
            "2. Only after inspection, use Google Sheets tools such as "
            "google_sheets_list_sheets, google_sheets_get_sheet_data, or "
            "google_sheets_get_multiple_sheet_data if needed.\n"
            "3. Unless the user narrows the scope, analyze all sheet tabs with data "
            "and use the full used range returned by inspection, not just a preview sample.\n"
            "4. If analysis type is not specific, decide a sensible default analysis "
            "yourself from the available columns and provide a simple useful summary.\n"
            "5. Do not ask the user to choose tabs, ranges, columns, chart types, "
            "or other analysis parameters until after tool-based inspection proves "
            "that the spreadsheet is genuinely ambiguous.\n"
            "Original user request (for intent only, not for parsing spreadsheet IDs):\n"
            f"{content}"
        )
        next_messages[index] = message.model_copy(
            update={"content": rewritten_content.strip()}
        )
        return next_messages

    return next_messages


def maybe_complete_manual_sheet_format_handoff(messages: list[AnyMessage]) -> AIMessage | None:
    """Directly format a linked response sheet when the user sends its URL back."""
    latest_human_index = -1
    for index in range(len(messages) - 1, -1, -1):
        if messages[index].type == "human":
            latest_human_index = index
            break

    if latest_human_index == -1:
        return None

    latest_human_content = content_to_text(messages[latest_human_index].content)
    user_language = infer_user_language(latest_human_content)
    targets = extract_spreadsheet_targets(latest_human_content)
    if not targets:
        return None

    stripped_request = strip_spreadsheet_targets(latest_human_content, targets)
    normalized_request = stripped_request.strip().lower()
    if normalized_request.startswith("spreadsheet_task"):
        return None

    explicit_format_request = any(
        marker in normalized_request
        for marker in (
            "format",
            "reformat",
            "prepare",
            "analysis-ready",
            "จัดรูปแบบ",
            "จัดใหม่",
            "เตรียมวิเคราะห์",
            "เตรียม",
            "ทำให้อ่านง่าย",
            "ลบชีต",
            "remove tab",
            "remove sheet",
        )
    )

    prior_ai_texts: list[str] = []
    for index in range(latest_human_index - 1, -1, -1):
        if messages[index].type == "ai":
            content = content_to_text(messages[index].content).lower().strip()
            if content:
                prior_ai_texts.append(content)
    previous_ai_text = "\n".join(prior_ai_texts)

    handoff_markers = (
        "send the spreadsheet link back",
        "analysis-ready table",
        "manually link this form",
        "format the raw response data",
        "please provide the spreadsheet link",
        "link to spreadsheet",
    )
    looks_like_handoff_reply = any(marker in previous_ai_text for marker in handoff_markers)
    is_link_only_reply = not normalized_request or normalized_request in {
        "here",
        "here you go",
        "this one",
        "this sheet",
        "spreadsheet",
        "sheet",
    }
    is_short_follow_up = len(normalized_request) <= 40
    prior_form_context = any(
        hint in previous_ai_text
        for hint in (
            "google form",
            "form link",
            "responses",
            "setup responses",
        )
    )

    if not explicit_format_request and not looks_like_handoff_reply and not is_link_only_reply and not (
        prior_form_context and is_short_follow_up
    ):
        return None

    try:
        result = format_response_sheet_for_analysis.invoke(
            {"spreadsheet_target": targets[0]}
        )
        if not isinstance(result, str):
            result = json.dumps(result, ensure_ascii=False, indent=2)
        payload = json.loads(result)
        spreadsheet_id = str(payload.get("spreadsheetId", "") or "")
        spreadsheet_title = str(payload.get("spreadsheetTitle", "") or "")
        source_sheet = str(payload.get("sourceSheet", "") or "")
        output_sheet = str(payload.get("outputSheet", "") or "")
        detail_sheet = str(payload.get("detailSheet", "") or "")
        summary_sheet = str(payload.get("summarySheet", "") or "")
        row_count = int(payload.get("rowCountWritten", 0) or 0)
        column_count = int(payload.get("columnCount", 0) or 0)
        detail_row_count = int(payload.get("detailRowCountWritten", 0) or 0)
        summary_row_count = int(payload.get("questionSummaryRowCount", 0) or 0)
        spreadsheet_url = (
            f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit"
            if spreadsheet_id
            else ""
        )
        if user_language == "th":
            response_lines = [
                "ฉันจัดรูปแบบชีตคำตอบที่ลิงก์ไว้สำหรับการวิเคราะห์เรียบร้อยแล้ว",
                "",
                f"- สเปรดชีต: {spreadsheet_title or spreadsheet_id}",
                f"- ชีตต้นฉบับ: {source_sheet}",
                f"- ชีตคำตอบที่จัดรูปแบบ: {output_sheet}",
                f"- ชีตสรุปคำตอบรายข้อ: {summary_sheet}",
                f"- จำนวนแถวคำตอบที่เขียน: {row_count}",
                f"- จำนวนคอลัมน์ในชีตคำตอบที่จัดรูปแบบ: {column_count}",
                f"- จำนวนแถวในชีตสรุป: {summary_row_count}",
            ]
        else:
            response_lines = [
                "I formatted the linked response sheet for analysis.",
                "",
                f"- Spreadsheet: {spreadsheet_title or spreadsheet_id}",
                f"- Source sheet: {source_sheet}",
                f"- Processed responses sheet: {output_sheet}",
                f"- Question summary sheet: {summary_sheet}",
                f"- Response rows written: {row_count}",
                f"- Columns in processed sheet: {column_count}",
                f"- Summary rows written: {summary_row_count}",
            ]
        if spreadsheet_url:
            response_lines.extend(
                ["", f"{'ลิงก์สเปรดชีต' if user_language == 'th' else 'Spreadsheet link'}: {spreadsheet_url}"]
            )
        response_lines.extend(
            [
                "",
                (
                    "ใช้ชีตคำตอบที่จัดรูปแบบสำหรับการไล่ดูคำตอบแบบกว้าง และใช้ชีตสรุปคำตอบรายข้อสำหรับการนับ เปอร์เซ็นต์ กราฟ และการวิเคราะห์ต่อ"
                    if user_language == "th"
                    else "Use the processed responses sheet for wide row inspection and the question summary sheet for counts, percentages, charts, and further analysis."
                ),
            ]
        )
        return AIMessage(content="\n".join(response_lines).strip())
    except Exception as exc:
        raise RuntimeError(
            (
                "ฉันตรวจพบว่าเป็นการส่งลิงก์สเปรดชีตกลับมา แต่การจัดรูปแบบชีตคำตอบล้มเหลว "
                if user_language == "th"
                else "I recognized the spreadsheet link handoff, but formatting the linked response sheet failed. "
            )
            + f"Details: {exc}"
        ) from exc


def maybe_complete_form_creation_request(messages: list[AnyMessage]) -> AIMessage | None:
    """Directly create a Google Form when the latest user turn is clearly a form request."""
    latest_human_index = -1
    for index in range(len(messages) - 1, -1, -1):
        if messages[index].type == "human":
            latest_human_index = index
            break

    if latest_human_index == -1:
        return None

    latest_human_content = content_to_text(messages[latest_human_index].content).strip()
    latest_docx_bytes = extract_latest_docx_bytes(messages)
    embedded_file_context = extract_embedded_file_context(latest_human_content)
    latest_user_instruction = strip_embedded_file_context(latest_human_content)
    effective_reference_text = embedded_file_context.strip()
    effective_creation_brief = latest_user_instruction.strip()
    exact_source_mode = bool(effective_reference_text) and prefers_exact_source_following(
        latest_user_instruction or latest_human_content
    )
    if effective_reference_text:
        effective_creation_brief = (
            f"{effective_creation_brief}\n\n"
            "Reference material from uploaded file:\n"
            f"{effective_reference_text}"
        ).strip()

    user_language = infer_user_language(latest_human_content)
    if not latest_human_content or latest_human_content.startswith("FORM_CREATION_TASK"):
        return None
    if extract_spreadsheet_targets(latest_user_instruction or latest_human_content):
        return None
    if not looks_like_form_creation_request(latest_user_instruction or latest_human_content):
        return None

    parsing_source = latest_user_instruction or latest_human_content
    file_questions: list[dict[str, Any]] = []
    if latest_docx_bytes:
        file_questions = extract_docx_questions_with_images(latest_docx_bytes)
    if not file_questions and effective_reference_text:
        file_questions = extract_questions_from_reference_text(effective_reference_text)

    is_quiz = infer_form_is_quiz(
        "\n\n".join(
            part
            for part in (
                latest_user_instruction,
                effective_reference_text,
                effective_creation_brief,
            )
            if str(part or "").strip()
        ),
        file_questions,
    )

    respondent_prompt_source = latest_user_instruction or latest_human_content
    respondent_questions = extract_requested_respondent_questions(respondent_prompt_source)
    if not respondent_questions:
        respondent_questions = extract_inline_respondent_questions(respondent_prompt_source)
    try:
        title = _choose_form_title_with_agent(
            parsing_source,
            user_language=user_language,
            respondent_questions=respondent_questions,
            source_questions=file_questions,
        ).strip() or "Generated Google Form"
    except Exception:
        title = extract_form_title(parsing_source).strip() or "Generated Google Form"
    description = extract_form_description(parsing_source).strip()
    expected_question_count = 0 if exact_source_mode else infer_default_question_count(parsing_source)
    section_structure = extract_requested_section_structure(parsing_source)
    if not section_structure:
        section_structure = infer_default_section_structure(
            parsing_source,
            respondent_questions,
            expected_question_count,
        )
    if not section_structure and effective_reference_text:
        section_structure = extract_requested_section_structure(effective_reference_text)

    if effective_reference_text and file_questions:
        file_main_question_count = _count_non_section_questions(file_questions) - len(
            respondent_questions
        )
        if file_main_question_count > 0:
            expected_question_count = file_main_question_count
        if (
            not exact_source_mode
            and infer_default_question_count(latest_user_instruction or latest_human_content) > 0
            and file_main_question_count > 0
            and expected_question_count != file_main_question_count
        ):
            expected_question_count = file_main_question_count

    if exact_source_mode and effective_reference_text and not file_questions:
        raise RuntimeError(
            "\u0e09\u0e31\u0e19\u0e16\u0e39\u0e01\u0e02\u0e2d\u0e43\u0e2b\u0e49\u0e22\u0e36\u0e14\u0e15\u0e32\u0e21\u0e44\u0e1f\u0e25\u0e4c\u0e41\u0e19\u0e1a\u0e41\u0e1a\u0e1a\u0e15\u0e23\u0e07\u0e15\u0e49\u0e19\u0e09\u0e1a\u0e31\u0e1a "
            "\u0e41\u0e15\u0e48\u0e22\u0e31\u0e07\u0e14\u0e36\u0e07\u0e04\u0e33\u0e16\u0e32\u0e21\u0e41\u0e25\u0e30\u0e15\u0e31\u0e27\u0e40\u0e25\u0e37\u0e2d\u0e01\u0e08\u0e32\u0e01\u0e44\u0e1f\u0e25\u0e4c\u0e41\u0e19\u0e1a\u0e44\u0e14\u0e49\u0e44\u0e21\u0e48\u0e04\u0e23\u0e1a\u0e16\u0e49\u0e27\u0e19 "
            "\u0e01\u0e23\u0e38\u0e13\u0e32\u0e41\u0e19\u0e1a\u0e44\u0e1f\u0e25\u0e4c\u0e17\u0e35\u0e48\u0e0a\u0e31\u0e14\u0e40\u0e08\u0e19\u0e02\u0e36\u0e49\u0e19 \u0e2b\u0e23\u0e37\u0e2d\u0e43\u0e0a\u0e49 DOCX/PDF \u0e17\u0e35\u0e48\u0e21\u0e35\u0e02\u0e49\u0e2d\u0e04\u0e27\u0e32\u0e21\u0e40\u0e25\u0e37\u0e2d\u0e01\u0e04\u0e31\u0e14\u0e25\u0e2d\u0e01\u0e44\u0e14\u0e49"
            if user_language == "th"
            else "I was asked to follow the uploaded source exactly, but I could not reliably extract "
            "questions and answer choices from the attached file. Please attach a clearer file or a "
            "DOCX/PDF with selectable text."
        )

    try:
        result = create_form_with_response_sheet.invoke(
            {
                "title": title,
                "description": description,
                "questions_json": (
                    json.dumps(file_questions, ensure_ascii=False) if file_questions else ""
                ),
                "respondent_questions_json": (
                    json.dumps(respondent_questions, ensure_ascii=False)
                    if respondent_questions
                    else ""
                ),
                "section_structure_json": (
                    json.dumps(section_structure, ensure_ascii=False)
                    if section_structure
                    else ""
                ),
                "expected_question_count": expected_question_count,
                "source_prompt": effective_creation_brief or latest_human_content,
                "strict_source_questions": bool(file_questions) or exact_source_mode,
                "is_quiz": is_quiz,
                "google_oauth_session_key": GOOGLE_OAUTH_SESSION_KEY.get() or "",
            }
        )
        if not isinstance(result, str):
            result = json.dumps(result, ensure_ascii=False, indent=2)
        payload = json.loads(result)

        if user_language == "th":
            response_lines = [
                "\u0e2a\u0e23\u0e49\u0e32\u0e07 Google Form \u0e40\u0e23\u0e35\u0e22\u0e1a\u0e23\u0e49\u0e2d\u0e22\u0e41\u0e25\u0e49\u0e27",
                "",
                f"- \u0e0a\u0e37\u0e48\u0e2d\u0e1f\u0e2d\u0e23\u0e4c\u0e21: {str(payload.get('title', '') or title)}",
            ]
        else:
            response_lines = [
                "The Google Form has been successfully created.",
                "",
                f"- Title: {str(payload.get('title', '') or title)}",
            ]

        if effective_reference_text and file_questions:
            response_lines.append(
                f"- {'\u0e43\u0e0a\u0e49\u0e04\u0e33\u0e16\u0e32\u0e21\u0e08\u0e32\u0e01\u0e44\u0e1f\u0e25\u0e4c\u0e41\u0e19\u0e1a\u0e40\u0e1b\u0e47\u0e19\u0e2b\u0e25\u0e31\u0e01' if user_language == 'th' else 'Primary question source'}: "
                f"{'\u0e44\u0e1f\u0e25\u0e4c\u0e41\u0e19\u0e1a\u0e17\u0e35\u0e48\u0e2d\u0e31\u0e1b\u0e42\u0e2b\u0e25\u0e14' if user_language == 'th' else 'uploaded file'}"
            )

        form_url = str(payload.get("formUrl", "") or payload.get("editUrl", "") or "")
        responder_url = str(payload.get("responderUri", "") or payload.get("responseUrl", "") or "")
        form_id = str(payload.get("formId", "") or "")
        question_count = int(payload.get("questionCount", 0) or 0)
        inserted_image_count = int(payload.get("insertedImageCount", 0) or 0)
        payload_is_quiz = bool(payload.get("isQuiz", False))
        spreadsheet_id = str(payload.get("spreadsheetId", "") or "")
        spreadsheet_title = str(payload.get("spreadsheetTitle", "") or "")
        spreadsheet_url = str(payload.get("spreadsheetUrl", "") or "")
        link_status = str(payload.get("linkStatus", "") or "").strip()
        link_ok = link_status == "linked"
        postprocess_status = str(payload.get("postprocessStatus", "") or "")
        processed_sheet_name = str(payload.get("processedSheetName", "") or "")
        summary_sheet_name = str(payload.get("summarySheetName", "") or "")
        postprocess_error = str(payload.get("postprocessError", "") or "")

        if form_id:
            response_lines.append(f"- {'\u0e23\u0e2b\u0e31\u0e2a\u0e1f\u0e2d\u0e23\u0e4c\u0e21' if user_language == 'th' else 'Form ID'}: {form_id}")
        if question_count:
            response_lines.append(
                f"- {'\u0e08\u0e33\u0e19\u0e27\u0e19\u0e04\u0e33\u0e16\u0e32\u0e21\u0e17\u0e35\u0e48\u0e40\u0e1e\u0e34\u0e48\u0e21' if user_language == 'th' else 'Questions added'}: {question_count}"
            )
        response_lines.append(
            f"- {'\u0e42\u0e2b\u0e21\u0e14\u0e41\u0e1a\u0e1a\u0e17\u0e14\u0e2a\u0e2d\u0e1a' if user_language == 'th' else 'Quiz mode'}: "
            + (
                "\u0e40\u0e1b\u0e34\u0e14"
                if user_language == "th" and payload_is_quiz
                else "\u0e1b\u0e34\u0e14"
                if user_language == "th" and not payload_is_quiz
                else "On"
                if payload_is_quiz
                else "Off"
            )
        )
        if inserted_image_count:
            response_lines.append(
                f"- {'\u0e08\u0e33\u0e19\u0e27\u0e19\u0e23\u0e39\u0e1b\u0e17\u0e35\u0e48\u0e41\u0e17\u0e23\u0e01' if user_language == 'th' else 'Images inserted'}: {inserted_image_count}"
            )
        if spreadsheet_id:
            response_lines.append(
                f"- {'\u0e23\u0e2b\u0e31\u0e2a\u0e2a\u0e40\u0e1b\u0e23\u0e14\u0e0a\u0e35\u0e15' if user_language == 'th' else 'Spreadsheet ID'}: {spreadsheet_id}"
            )
        if spreadsheet_title:
            response_lines.append(
                f"- {'\u0e0a\u0e37\u0e48\u0e2d\u0e2a\u0e40\u0e1b\u0e23\u0e14\u0e0a\u0e35\u0e15' if user_language == 'th' else 'Spreadsheet title'}: {spreadsheet_title}"
            )
        link_status = str(payload.get("linkStatus", "") or "").strip()
        if link_status and link_status != "linked":
            response_lines.append(
                "- การเชื่อมสเปรดชีตอัตโนมัติยังไม่สำเร็จ"
                if user_language == "th"
                else "- Automatic spreadsheet linking did not complete"
            )
        image_insert_status = str(payload.get("imageInsertStatus", "") or "").strip()
        if image_insert_status and image_insert_status != "images-inserted":
            response_lines.append(
                "- การแทรกรูปภาพบางส่วนยังไม่สำเร็จ"
                if user_language == "th"
                else "- Some form images were not inserted automatically"
            )
        link_lines: list[str] = []
        if form_url:
            link_lines.extend(
                [
                    f"{'\u0e25\u0e34\u0e07\u0e01\u0e4c\u0e1f\u0e2d\u0e23\u0e4c\u0e21' if user_language == 'th' else 'Form link'}:",
                    "",
                    form_url,
                ]
            )
        if responder_url:
            if link_lines:
                link_lines.append("")
            link_lines.extend(
                [
                    f"{'\u0e25\u0e34\u0e07\u0e01\u0e4c\u0e2a\u0e33\u0e2b\u0e23\u0e31\u0e1a\u0e1c\u0e39\u0e49\u0e15\u0e2d\u0e1a' if user_language == 'th' else 'Responder link'}:",
                    "",
                    responder_url,
                ]
            )
        if spreadsheet_url:
            if link_lines:
                link_lines.append("")
            link_lines.extend(
                [
                    f"{'\u0e25\u0e34\u0e07\u0e01\u0e4c\u0e2a\u0e40\u0e1b\u0e23\u0e14\u0e0a\u0e35\u0e15' if user_language == 'th' else 'Spreadsheet link'}:",
                    "",
                    spreadsheet_url,
                ]
            )
        if link_lines:
            response_lines.extend(["", *link_lines])
        if postprocess_status == "formatted":
            response_lines.append(
                f"- {'สถานะการจัดรูปแบบชีต' if user_language == 'th' else 'Sheet post-process'}: "
                f"{'เสร็จแล้ว' if user_language == 'th' else 'Completed'}"
            )
            if processed_sheet_name:
                response_lines.append(
                    f"- {'ชีตคำตอบที่จัดรูปแบบ' if user_language == 'th' else 'Processed responses sheet'}: {processed_sheet_name}"
                )
            if summary_sheet_name:
                response_lines.append(
                    f"- {'ชีตสรุปคำตอบรายข้อ' if user_language == 'th' else 'Question summary sheet'}: {summary_sheet_name}"
                )
        elif postprocess_status == "waiting-for-responses":
            response_lines.append(
                f"- {'สถานะการจัดรูปแบบชีต' if user_language == 'th' else 'Sheet post-process'}: "
                f"{'รอข้อมูลคำตอบชุดแรก' if user_language == 'th' else 'Waiting for first responses'}"
            )
        elif postprocess_status and postprocess_error:
            response_lines.append(
                f"- {'สถานะการจัดรูปแบบชีต' if user_language == 'th' else 'Sheet post-process'}: "
                f"{'ยังไม่สำเร็จ' if user_language == 'th' else 'Not completed yet'}"
            )

        next_step = str(payload.get("nextStep", "") or "").strip()
        if next_step:
            localized_next_step = next_step
            if user_language == "th":
                if not link_ok:
                    localized_next_step = (
                        "ฟอร์มและสเปรดชีตถูกสร้างแล้ว แต่การเชื่อมสเปรดชีตอัตโนมัติยังไม่สำเร็จ "
                        "ให้ใช้ลิงก์ที่ส่งกลับไว้ก่อน แล้วค่อยลองเชื่อมใหม่อีกครั้ง"
                    )
                else:
                    localized_next_step = ""
            elif not link_ok:
                localized_next_step = (
                    "The form and spreadsheet were created, but automatic linking did not complete. "
                    "Use the returned links for now, then retry linking after the Apps Script runtime is corrected."
                )
            else:
                localized_next_step = ""
            if localized_next_step:
                response_lines.extend(["", localized_next_step])

        return AIMessage(content="\n".join(response_lines).strip())
    except Exception as exc:
        raise RuntimeError(
            (
                "\u0e09\u0e31\u0e19\u0e15\u0e23\u0e27\u0e08\u0e1e\u0e1a\u0e27\u0e48\u0e32\u0e19\u0e35\u0e48\u0e40\u0e1b\u0e47\u0e19\u0e04\u0e33\u0e02\u0e2d\u0e2a\u0e23\u0e49\u0e32\u0e07 Google Form \u0e41\u0e15\u0e48\u0e01\u0e32\u0e23\u0e2a\u0e23\u0e49\u0e32\u0e07\u0e1f\u0e2d\u0e23\u0e4c\u0e21\u0e41\u0e1a\u0e1a\u0e15\u0e23\u0e07\u0e25\u0e49\u0e21\u0e40\u0e2b\u0e25\u0e27 "
                if user_language == "th"
                else "I recognized this as a Google Form creation request, but the direct form creation flow failed. "
            )
            + f"Details: {exc}"
        ) from exc


class LocalLLMMessageFormatMiddleware(AgentMiddleware):
    """Make DeepAgents messages compatible with local OpenAI-compatible servers."""

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Any],
    ) -> ModelResponse | AIMessage:
        google_oauth_session_key = get_google_oauth_session_key_from_request(request)
        token_session = GOOGLE_OAUTH_SESSION_KEY.set(google_oauth_session_key)
        try:
            original_messages = list(request.messages)
            system_message = (
                sanitize_message_content(request.system_message)
                if request.system_message is not None
                else None
            )
            messages = [sanitize_message_content(message) for message in original_messages]
            messages = inject_attached_file_context(
                messages,
                get_attached_file_context(request),
            )
            direct_sheet_response = await asyncio.to_thread(
                run_with_google_oauth_session,
                google_oauth_session_key,
                maybe_complete_manual_sheet_format_handoff,
                messages,
            )
            if direct_sheet_response is not None:
                return direct_sheet_response
            direct_form_database_link_response = await asyncio.to_thread(
                run_with_google_oauth_session,
                google_oauth_session_key,
                maybe_complete_form_database_link_request,
                messages,
            )
            if direct_form_database_link_response is not None:
                return direct_form_database_link_response
            direct_database_response = await asyncio.to_thread(
                run_with_google_oauth_session,
                google_oauth_session_key,
                maybe_complete_database_request,
                messages,
            )
            if direct_database_response is not None:
                return direct_database_response
            direct_analysis_response = await asyncio.to_thread(
                run_with_google_oauth_session,
                google_oauth_session_key,
                maybe_complete_spreadsheet_analysis_request,
                messages,
            )
            if direct_analysis_response is not None:
                return direct_analysis_response
            direct_form_messages = inject_attached_file_context(
                original_messages,
                get_attached_file_context(request),
            )
            direct_form_response = await asyncio.to_thread(
                run_with_google_oauth_session,
                google_oauth_session_key,
                maybe_complete_form_creation_request,
                direct_form_messages,
            )
            if direct_form_response is not None:
                return direct_form_response
            messages = inject_form_creation_context(messages)
            messages = inject_spreadsheet_target_context(messages)
            response = await handler(
                request.override(system_message=system_message, messages=messages)
            )
            return clean_model_response(response)
        finally:
            GOOGLE_OAUTH_SESSION_KEY.reset(token_session)


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


def build_mcp_client(*, include_google_sheets: bool = True) -> MultiServerMCPClient:
    """Build the MCP client for the configured stdio MCP servers."""
    forms_server_path = Path(get_required_env("GOOGLE_FORMS_MCP_PATH")).expanduser()
    if not forms_server_path.exists():
        raise RuntimeError(
            "GOOGLE_FORMS_MCP_PATH does not exist. Build google-forms-mcp and "
            f"set GOOGLE_FORMS_MCP_PATH to its build/index.js file: {forms_server_path}"
        )

    forms_server_env = {
        "GOOGLE_CLIENT_ID": get_required_env("GOOGLE_CLIENT_ID"),
        "GOOGLE_CLIENT_SECRET": get_required_env("GOOGLE_CLIENT_SECRET"),
    }
    refresh_token = (
        _load_google_refresh_token_from_file(get_google_oauth_token_path_for_mcp())
        or os.getenv("GOOGLE_REFRESH_TOKEN")
    )
    if refresh_token:
        forms_server_env["GOOGLE_REFRESH_TOKEN"] = refresh_token

    servers: dict[str, dict[str, Any]] = {
        "google_forms": {
            "transport": "stdio",
            "command": "node",
            "args": [str(forms_server_path)],
            "env": forms_server_env,
        }
    }

    if include_google_sheets and is_env_truthy("ENABLE_GOOGLE_SHEETS_MCP"):
        if not has_google_sheets_auth_config():
            return MultiServerMCPClient(
                servers,
                tool_name_prefix=True,
            )

        sheets_server_env = {
            key: value
            for key in (
                "SERVICE_ACCOUNT_PATH",
                "DRIVE_FOLDER_ID",
                "CREDENTIALS_PATH",
                "TOKEN_PATH",
                "CREDENTIALS_CONFIG",
                "ENABLED_TOOLS",
            )
            if (value := os.getenv(key))
        }
        if "TOKEN_PATH" not in sheets_server_env:
            preferred_token_path = get_google_oauth_token_path_for_mcp()
            if preferred_token_path.exists():
                sheets_server_env["TOKEN_PATH"] = str(preferred_token_path)
        if (
            "SERVICE_ACCOUNT_PATH" not in sheets_server_env
            and "CREDENTIALS_PATH" not in sheets_server_env
        ):
            generated_credentials_path = ensure_google_sheets_oauth_credentials_file()
            if generated_credentials_path is not None:
                sheets_server_env["CREDENTIALS_PATH"] = str(generated_credentials_path)
        sheets_enabled_tools = os.getenv(
            "GOOGLE_SHEETS_ENABLED_TOOLS",
            "search_spreadsheets,list_spreadsheets,list_sheets,get_sheet_data,"
            "get_multiple_sheet_data,get_sheet_formulas,find_in_spreadsheet,"
            "create_sheet,update_cells,batch_update_cells,add_chart,batch_update",
        ).strip()
        sheets_server_args = ["--include-tools", sheets_enabled_tools] if sheets_enabled_tools else []
        servers["google_sheets"] = {
            "transport": "stdio",
            "command": "mcp-google-sheets",
            "args": sheets_server_args,
            "env": sheets_server_env,
        }

    return MultiServerMCPClient(
        servers,
        tool_name_prefix=True,
    )


async def build_agent() -> Any:
    """Create the Deep Agent with Google Forms MCP tools."""
    global BUILT_AGENT, BUILT_AGENT_LOCK, GOOGLE_SHEETS_MCP_DISABLED_UNTIL_RESTART
    if BUILT_AGENT is not None:
        return BUILT_AGENT
    if BUILT_AGENT_LOCK is None:
        BUILT_AGENT_LOCK = asyncio.Lock()

    async with BUILT_AGENT_LOCK:
        if BUILT_AGENT is not None:
            return BUILT_AGENT

        _ensure_form_response_sync_worker_started()
        model = build_chat_model()
        include_google_sheets = (
            is_env_truthy("ENABLE_GOOGLE_SHEETS_MCP")
            and not GOOGLE_SHEETS_MCP_DISABLED_UNTIL_RESTART
        )
        client = await asyncio.to_thread(
            build_mcp_client,
            include_google_sheets=include_google_sheets,
        )
        try:
            mcp_tools = await client.get_tools()
        except Exception:
            if not include_google_sheets:
                raise
            GOOGLE_SHEETS_MCP_DISABLED_UNTIL_RESTART = True
            client = await asyncio.to_thread(
                build_mcp_client,
                include_google_sheets=False,
            )
            mcp_tools = await client.get_tools()
        filtered_mcp_tools = [
            tool
            for tool in mcp_tools
            if "create_form" not in str(getattr(tool, "name", "") or "").lower()
        ]
        tools = [
            create_form_with_response_sheet,
            list_google_forms,
            inspect_form_response_database,
            query_form_response_database,
            format_response_sheet_for_analysis,
            inspect_spreadsheet_for_analysis,
            *filtered_mcp_tools,
        ]

        BUILT_AGENT = create_deep_agent(
            model=model,
            tools=tools,
            middleware=[LocalLLMMessageFormatMiddleware()],
            backend=FilesystemBackend(root_dir=str(PROJECT_ROOT), virtual_mode=False),
            skills=[SKILLS_DIR.as_posix()],
            system_prompt=SYSTEM_PROMPT,
        )
        return BUILT_AGENT

