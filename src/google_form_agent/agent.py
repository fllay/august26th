"""LangChain Deep Agent wired to the Google Forms MCP server."""

import ast
import base64
import csv
from datetime import datetime, timezone
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
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials as UserCredentials
from googleapiclient.discovery import build as build_google_api
from langchain.agents.middleware.types import AgentMiddleware, ModelRequest, ModelResponse
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_core.messages import AIMessage, AnyMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI


OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
LOCAL_LLM_DEFAULT_API_KEY = "not-needed"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
SKILLS_DIR = PROJECT_ROOT / "skills"
DEFAULT_GOOGLE_OAUTH_TOKEN_PATH = PROJECT_ROOT / ".data" / "google-oauth.json"
FORM_SHEET_LINKS_PATH = PROJECT_ROOT / ".data" / "form-sheet-links.json"
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
  - Prefer create_form_with_response_sheet for new forms so each created form
    also gets a companion response spreadsheet tracked by the app.
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
  - If the user asks to refresh a companion response sheet from the live form,
    use sync_form_responses_to_sheet.
  - Do not call sync_form_responses_to_sheet during a form creation request
    unless the user explicitly asks to sync responses.
  - After create_form_with_response_sheet succeeds, reuse the exact returned
    formId and spreadsheetId values. Never substitute placeholder IDs.
  - After creating or editing a form, report the form title, the questions added,
    and any URL, form ID, or spreadsheet ID returned by the tools.
  - Never claim a form was created unless a Google Forms MCP tool succeeded.
- For Google Sheets analysis:
  - Start spreadsheet inspection with the inspect_spreadsheet_for_analysis tool
    when the user provides a spreadsheet target and wants analysis.
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
SPREADSHEET_URL_RE = re.compile(
    r"https?://docs\.google\.com/spreadsheets/d/([a-zA-Z0-9-_]+)",
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
    )
    return any(keyword in lowered for keyword in analysis_keywords)


def looks_like_form_creation_request(text: str) -> bool:
    """Return whether the user is probably asking to create or modify a form."""
    lowered = text.lower()
    if "spreadsheet" in lowered and "analy" in lowered:
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
    return any(keyword in lowered for keyword in creation_keywords)


def extract_form_title(text: str) -> str:
    """Extract a form title from common prompt patterns."""
    lines = [line.rstrip() for line in text.splitlines()]
    for index, raw_line in enumerate(lines):
        line = raw_line.strip()
        if line.lower() == "title:" and index + 1 < len(lines):
            candidate = lines[index + 1].strip()
            if candidate:
                return candidate
        if line.lower().startswith("title:"):
            candidate = line.split(":", 1)[1].strip()
            if candidate:
                return candidate
    return ""


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
    return "\n".join(collected).strip()


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


def compress_form_creation_request(text: str) -> str:
    """Condense a long form-creation prompt into a compact structured task."""
    title = extract_form_title(text)
    description = extract_form_description(text)
    question_count = extract_question_count(text) or 0
    lowered = text.lower()

    requirement_lines: list[str] = []
    if question_count:
        requirement_lines.append(
            f"- Generate {question_count} required multiple-choice questions."
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
    if "companion google sheet" in lowered or "response sheet" in lowered:
        requirement_lines.append("- Create and link a companion Google Sheet for responses.")

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
            "Use tools. Prefer one create_form_with_response_sheet call with title, "
            "description, and questions_json when enough details are available."
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
    sections.append(
        "If you can infer the questions, pass them in questions_json as JSON like "
        "[{\"title\":\"...\",\"type\":\"multiple_choice\",\"required\":true,"
        "\"options\":[\"A\",\"B\",\"C\",\"D\"]}]."
    )
    sections.append("Do not call sync_form_responses_to_sheet unless the user explicitly asks for syncing.")
    sections.append("Never invent or shorten IDs like '1aBcD...'. Use only exact IDs returned by tools.")
    sections.append("Always return the form ID, edit URL, responder URL, spreadsheet ID, and spreadsheet URL after successful creation.")
    sections.append("If any field is missing, ask the minimum necessary clarifying question.")
    return "\n".join(sections)


def extract_spreadsheet_id(target: str) -> str:
    """Return a spreadsheet id from either a full URL or a bare id."""
    if "..." in target:
        raise RuntimeError(
            "Spreadsheet ID appears truncated or placeholder-like. Use the full exact spreadsheet ID."
        )
    match = SPREADSHEET_URL_RE.search(target)
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
        os.getenv("TOKEN_PATH") or str(get_google_oauth_token_path())
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

    refresh_token = load_google_refresh_token()
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

        title = str(raw_question.get("title", "") or "").strip()
        if not title:
            raise RuntimeError(f"Question {index} is missing a title.")

        question_type = str(raw_question.get("type", "multiple_choice") or "multiple_choice").strip().lower()
        required = bool(raw_question.get("required", True))
        options = raw_question.get("options", [])
        help_text = str(raw_question.get("description", "") or raw_question.get("help_text", "") or "").strip()

        normalized = {
            "title": title,
            "type": question_type,
            "required": required,
            "options": options if isinstance(options, list) else [],
            "description": help_text,
        }
        normalized_questions.append(normalized)

    return normalized_questions


def _build_form_item(question: dict[str, Any]) -> dict[str, Any]:
    """Build a Google Forms item payload from a normalized question."""
    question_type = str(question.get("type", "multiple_choice") or "multiple_choice").strip().lower()
    required = bool(question.get("required", True))
    item: dict[str, Any] = {"title": str(question["title"])}
    description = str(question.get("description", "") or "").strip()
    if description:
        item["description"] = description

    if question_type in {"multiple_choice", "multiple-choice", "radio"}:
        options = [str(option).strip() for option in question.get("options", []) if str(option).strip()]
        if len(options) < 2:
            raise RuntimeError(f"Multiple-choice question '{question['title']}' needs at least 2 options.")
        item["questionItem"] = {
            "question": {
                "required": required,
                "choiceQuestion": {
                    "type": "RADIO",
                    "options": [{"value": option} for option in options],
                },
            }
        }
        return item

    if question_type in {"checkbox", "checkboxes"}:
        options = [str(option).strip() for option in question.get("options", []) if str(option).strip()]
        if len(options) < 2:
            raise RuntimeError(f"Checkbox question '{question['title']}' needs at least 2 options.")
        item["questionItem"] = {
            "question": {
                "required": required,
                "choiceQuestion": {
                    "type": "CHECKBOX",
                    "options": [{"value": option} for option in options],
                },
            }
        }
        return item

    if question_type in {"dropdown", "drop_down"}:
        options = [str(option).strip() for option in question.get("options", []) if str(option).strip()]
        if len(options) < 2:
            raise RuntimeError(f"Dropdown question '{question['title']}' needs at least 2 options.")
        item["questionItem"] = {
            "question": {
                "required": required,
                "choiceQuestion": {
                    "type": "DROP_DOWN",
                    "options": [{"value": option} for option in options],
                },
            }
        }
        return item

    item["questionItem"] = {
        "question": {
            "required": required,
            "textQuestion": {},
        }
    }
    return item


def _apply_form_batch_updates(
    forms_service: Any,
    form_id: str,
    description: str,
    questions: list[dict[str, Any]],
) -> None:
    """Apply form description and items in a single batchUpdate call."""
    requests: list[dict[str, Any]] = []

    if description.strip():
        requests.append(
            {
                "updateFormInfo": {
                    "info": {"description": description.strip()},
                    "updateMask": "description",
                }
            }
        )

    for index, question in enumerate(questions):
        requests.append(
            {
                "createItem": {
                    "item": _build_form_item(question),
                    "location": {"index": index},
                }
            }
        )

    if not requests:
        return

    forms_service.forms().batchUpdate(
        formId=form_id,
        body={"requests": requests},
    ).execute()


@tool
def create_form_with_response_sheet(
    title: str,
    description: str = "",
    spreadsheet_title: str = "",
    questions_json: str = "",
) -> str:
    """Create a Google Form, optionally add description/questions, and create a companion Google Sheet."""
    if not title.strip():
        raise RuntimeError("title is required")

    forms_service = _build_forms_service()
    sheets_service = _build_sheets_service()
    questions = _parse_questions_json(questions_json)
    form_body = {
        "info": {
            "title": title.strip(),
            "documentTitle": title.strip(),
        }
    }
    form_response = forms_service.forms().create(body=form_body).execute()
    form_id = form_response.get("formId", "")
    if not form_id:
        raise RuntimeError("Google Forms API did not return a formId.")

    _apply_form_batch_updates(
        forms_service=forms_service,
        form_id=form_id,
        description=description,
        questions=questions,
    )

    responder_uri = form_response.get("responderUri", "")
    edit_uri = f"https://docs.google.com/forms/d/{form_id}/edit"
    sheet_title = spreadsheet_title.strip() or f"{title.strip()} Responses"
    spreadsheet_response = sheets_service.spreadsheets().create(
        body={
            "properties": {"title": sheet_title},
            "sheets": [{"properties": {"title": "Responses"}}],
        }
    ).execute()
    spreadsheet_id = spreadsheet_response.get("spreadsheetId", "")
    spreadsheet_url = spreadsheet_response.get("spreadsheetUrl", "")
    if not spreadsheet_id:
        raise RuntimeError("Google Sheets API did not return a spreadsheetId.")

    _upsert_form_sheet_link(
        form_id,
        {
            "formId": form_id,
            "formUrl": edit_uri,
            "editUrl": edit_uri,
            "title": title.strip(),
            "description": description.strip(),
            "responderUri": responder_uri,
            "responseUrl": responder_uri,
            "spreadsheetId": spreadsheet_id,
            "spreadsheetUrl": spreadsheet_url,
            "linkedAt": datetime.now(timezone.utc).isoformat(),
            "syncMode": "agent-managed",
        },
    )

    return json.dumps(
        {
            "formId": form_id,
            "formUrl": edit_uri,
            "editUrl": edit_uri,
            "title": title.strip(),
            "description": description.strip(),
            "responderUri": responder_uri,
            "responseUrl": responder_uri,
            "spreadsheetId": spreadsheet_id,
            "spreadsheetUrl": spreadsheet_url,
            "questionCount": len(questions),
            "linkMode": "agent-managed",
            "note": (
                "A companion response spreadsheet was created and linked in the app registry. "
                "Use sync_form_responses_to_sheet to refresh sheet data from live form responses."
            ),
        },
        ensure_ascii=False,
        indent=2,
    )


@tool
def sync_form_responses_to_sheet(
    form_id: str = "",
    spreadsheet_id: str = "",
) -> str:
    """Sync a form's live responses into its companion Google Sheet."""
    normalized_form_id = form_id.strip()
    normalized_spreadsheet_id = spreadsheet_id.strip()
    if "..." in normalized_form_id or "..." in normalized_spreadsheet_id:
        raise RuntimeError(
            "Form or spreadsheet ID appears truncated or placeholder-like. Use the full exact ID returned by the create tool."
        )
    if not normalized_form_id and normalized_spreadsheet_id:
        normalized_form_id = _find_form_id_by_spreadsheet_id(normalized_spreadsheet_id)
    if not normalized_form_id and not normalized_spreadsheet_id:
        normalized_form_id, latest_link = _get_latest_form_sheet_link()
        normalized_spreadsheet_id = str(latest_link.get("spreadsheetId", "") or "")
    if not normalized_form_id:
        raise RuntimeError(
            "form_id is required unless spreadsheet_id matches a linked form created by this app."
        )

    links = _load_form_sheet_links()
    link = links.get(normalized_form_id, {})
    target_spreadsheet_id = normalized_spreadsheet_id or str(link.get("spreadsheetId", "") or "")
    if not target_spreadsheet_id:
        raise RuntimeError("No spreadsheet is linked to this form. Provide spreadsheet_id explicitly.")

    forms_service = _build_forms_service()
    sheets_service = _build_sheets_service()

    form_payload = forms_service.forms().get(formId=normalized_form_id).execute()
    responses_payload = forms_service.forms().responses().list(formId=normalized_form_id).execute()
    rows = _build_response_rows(form_payload, responses_payload)

    response_tab = "Responses"
    sheets_service.spreadsheets().values().clear(
        spreadsheetId=target_spreadsheet_id,
        range=_quote_sheet_title(response_tab),
        body={},
    ).execute()
    sheets_service.spreadsheets().values().update(
        spreadsheetId=target_spreadsheet_id,
        range=f"{_quote_sheet_title(response_tab)}!A1",
        valueInputOption="RAW",
        body={"values": rows},
    ).execute()

    _upsert_form_sheet_link(
        normalized_form_id,
        {
            "formId": normalized_form_id,
            "title": str(form_payload.get("info", {}).get("title", "") or ""),
            "description": str(form_payload.get("info", {}).get("description", "") or ""),
            "responderUri": str(form_payload.get("responderUri", "") or ""),
            "spreadsheetId": target_spreadsheet_id,
            "lastSyncedAt": str(form_payload.get("revisionId", "") or ""),
            "syncMode": "agent-managed",
        },
    )

    return json.dumps(
        {
            "formId": normalized_form_id,
            "spreadsheetId": target_spreadsheet_id,
            "responseTab": response_tab,
            "rowCountWritten": len(rows),
            "responseCount": max(len(rows) - 1, 0),
            "questionCount": max(len(rows[0]) - 4, 0) if rows else 0,
        },
        ensure_ascii=False,
        indent=2,
    )


@tool
def list_google_forms(query: str = "", limit: int = 20) -> str:
    """List the user's Google Forms and include any companion response sheet tracked by this app."""
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

    links = _load_form_sheet_links()
    forms: list[dict[str, Any]] = []
    for entry in response.get("files", []) or []:
        if not isinstance(entry, dict):
            continue
        form_id = str(entry.get("id", "") or "")
        linked_sheet = links.get(form_id, {})
        forms.append(
            {
                "formId": form_id,
                "title": str(entry.get("name", "") or ""),
                "editUrl": str(entry.get("webViewLink", "") or ""),
                "createdTime": str(entry.get("createdTime", "") or ""),
                "modifiedTime": str(entry.get("modifiedTime", "") or ""),
                "linkedSpreadsheetId": str(linked_sheet.get("spreadsheetId", "") or ""),
                "linkedSpreadsheetUrl": str(linked_sheet.get("spreadsheetUrl", "") or ""),
                "linkMode": str(linked_sheet.get("syncMode", "") or ""),
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
    credentials = _load_google_sheets_credentials()
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


def get_google_oauth_token_path() -> Path:
    """Return the shared OAuth token file path used by the web UI and backend."""
    configured = os.getenv("GOOGLE_OAUTH_TOKEN_PATH")
    if configured:
        return Path(configured).expanduser()
    return DEFAULT_GOOGLE_OAUTH_TOKEN_PATH


def load_google_refresh_token() -> str | None:
    """Load the Google refresh token from env first, then shared OAuth storage."""
    env_token = os.getenv("GOOGLE_REFRESH_TOKEN")
    if env_token:
        return env_token

    token_path = get_google_oauth_token_path()
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


def has_shared_google_oauth_token() -> bool:
    """Return whether the shared Google OAuth token file exists."""
    return get_google_oauth_token_path().exists()


def has_google_sheets_auth_config() -> bool:
    """Return whether Sheets MCP has any usable auth source configured."""
    if os.getenv("CREDENTIALS_CONFIG"):
        return True
    if has_shared_google_oauth_token():
        return True
    for env_name in ("SERVICE_ACCOUNT_PATH", "CREDENTIALS_PATH", "TOKEN_PATH"):
        env_value = os.getenv(env_name)
        if env_value and Path(env_value).expanduser().exists():
            return True
    return False


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
        messages = inject_form_creation_context(messages)
        messages = inject_spreadsheet_target_context(messages)
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
    refresh_token = load_google_refresh_token()
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

    if is_env_truthy("ENABLE_GOOGLE_SHEETS_MCP"):
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
        if "TOKEN_PATH" not in sheets_server_env and has_shared_google_oauth_token():
            sheets_server_env["TOKEN_PATH"] = str(get_google_oauth_token_path())
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
    model = build_chat_model()
    client = build_mcp_client()
    mcp_tools = await client.get_tools()
    filtered_mcp_tools = [
        tool
        for tool in mcp_tools
        if "create_form" not in str(getattr(tool, "name", "") or "").lower()
    ]
    tools = [
        create_form_with_response_sheet,
        sync_form_responses_to_sheet,
        list_google_forms,
        inspect_spreadsheet_for_analysis,
        *filtered_mcp_tools,
    ]

    return create_deep_agent(
        model=model,
        tools=tools,
        middleware=[LocalLLMMessageFormatMiddleware()],
        backend=FilesystemBackend(root_dir=str(PROJECT_ROOT)),
        skills=[SKILLS_DIR.as_posix()],
        system_prompt=SYSTEM_PROMPT,
    )
