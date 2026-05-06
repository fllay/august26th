import { NextResponse } from "next/server";
import {
  CHAT_HISTORY_TABLE,
  getPgPool,
  isPgConfigured,
} from "@/lib/server/postgres";

const TRANSIENT_PG_ERROR_CODES = new Set([
  "ECONNREFUSED",
  "ECONNRESET",
  "EHOSTUNREACH",
  "ENOTFOUND",
  "ETIMEDOUT",
  "EAI_AGAIN",
]);
const retryBackoffRaw = Number(process.env.PG_FEEDBACK_RETRY_MS);
const PG_RETRY_BACKOFF_MS =
  Number.isFinite(retryBackoffRaw) && retryBackoffRaw > 0
    ? retryBackoffRaw
    : 30_000;
let pgRetryAfter = 0;
let lastTransientLogAt = 0;

function collectErrorCodes(error: unknown): string[] {
  const codes: string[] = [];
  const queue: unknown[] = [error];
  while (queue.length) {
    const current = queue.shift();
    if (!current || typeof current !== "object") continue;

    const maybeCode = (current as { code?: unknown }).code;
    if (typeof maybeCode === "string") {
      codes.push(maybeCode);
    }

    const nestedErrors = (current as { errors?: unknown }).errors;
    if (Array.isArray(nestedErrors)) {
      queue.push(...nestedErrors);
    }

    const cause = (current as { cause?: unknown }).cause;
    if (cause) {
      queue.push(cause);
    }
  }
  return codes;
}

function isTransientPgError(error: unknown): boolean {
  return collectErrorCodes(error).some((code) =>
    TRANSIENT_PG_ERROR_CODES.has(code),
  );
}

function canAttemptPg(): boolean {
  return Date.now() >= pgRetryAfter;
}

function markPgFailureAndMaybeLog(error: unknown, label: string) {
  if (isTransientPgError(error)) {
    const now = Date.now();
    pgRetryAfter = now + PG_RETRY_BACKOFF_MS;
    if (now - lastTransientLogAt >= PG_RETRY_BACKOFF_MS) {
      lastTransientLogAt = now;
      console.warn(
        `PostgreSQL unavailable while trying to ${label} feedback. Backing off for ${PG_RETRY_BACKOFF_MS}ms.`,
        { codes: collectErrorCodes(error) },
      );
    }
    return;
  }
  console.error(`Failed to ${label} feedback in PostgreSQL:`, error);
}

function normalizeFeedback(value: unknown): number {
  const numeric = Number(value);
  if (Number.isNaN(numeric)) return 0;
  if (numeric > 0) return 1;
  if (numeric < 0) return -1;
  return 0;
}

export const runtime = "nodejs";

export async function GET(request: Request) {
  const { searchParams } = new URL(request.url);
  const sessionId = searchParams.get("sessionId") || "";
  if (!sessionId) {
    return NextResponse.json(
      { error: "sessionId is required." },
      { status: 400 },
    );
  }
  if (!isPgConfigured()) {
    return NextResponse.json({ feedback: {}, persisted: false });
  }
  if (!canAttemptPg()) {
    return NextResponse.json({ feedback: {}, persisted: false });
  }

  const query = `
    SELECT
      message->>'id' AS id,
      COALESCE(message->'data'->'additional_kwargs'->>'feedback', '0') AS feedback
    FROM ${CHAT_HISTORY_TABLE}
    WHERE session_id::text = $1
      AND message ? 'id'
    ORDER BY created_at ASC
  `;

  try {
    const result = await getPgPool().query(query, [sessionId]);
    pgRetryAfter = 0;
    const feedback: Record<string, number> = {};
    for (const row of result.rows) {
      if (!row?.id) continue;
      const parsed = Number(row.feedback);
      feedback[String(row.id)] = Number.isNaN(parsed) ? 0 : parsed;
    }
    return NextResponse.json({ feedback, persisted: true });
  } catch (error) {
    markPgFailureAndMaybeLog(error, "load");
    return NextResponse.json({ feedback: {}, persisted: false });
  }
}

export async function POST(request: Request) {
  let payload: {
    sessionId?: string;
    messageId?: string;
    feedback?: unknown;
  };
  try {
    payload = await request.json();
  } catch {
    return NextResponse.json({ error: "Invalid JSON." }, { status: 400 });
  }

  const sessionId = payload.sessionId?.trim() || "";
  const messageId = payload.messageId?.trim() || "";
  if (!sessionId || !messageId) {
    return NextResponse.json(
      { error: "sessionId and messageId are required." },
      { status: 400 },
    );
  }

  const rating = normalizeFeedback(payload.feedback);
  if (!isPgConfigured()) {
    return NextResponse.json({ updated: 0, feedback: rating, persisted: false });
  }
  if (!canAttemptPg()) {
    return NextResponse.json({ updated: 0, feedback: rating, persisted: false });
  }

  const query = `
    UPDATE ${CHAT_HISTORY_TABLE}
    SET message = jsonb_set(
      message,
      '{data,additional_kwargs,feedback}',
      to_jsonb($1::int),
      true
    )
    WHERE session_id::text = $2
      AND message->>'id' = $3
  `;

  try {
    const result = await getPgPool().query(query, [rating, sessionId, messageId]);
    pgRetryAfter = 0;
    return NextResponse.json({
      updated: result.rowCount,
      feedback: rating,
      persisted: true,
    });
  } catch (error) {
    markPgFailureAndMaybeLog(error, "update");
    return NextResponse.json({ updated: 0, feedback: rating, persisted: false });
  }
}
