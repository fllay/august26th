import type { PoolClient } from "pg";
import {
  CHAT_HISTORY_TABLE,
  getPgPool,
  isPgConfigured,
} from "@/lib/server/postgres";

type RawHistoryRow = {
  id: number;
  session_id: string;
  user_id: string | null;
  created_at: string;
  message: unknown;
  feedback_text: string | null;
};

export type HistoryEntry = {
  sessionId: string;
  userId: string | null;
  createdAt: string;
  role: string;
  type: string;
  content: string;
  feedback: string;
  feedbackText: string;
  messageId?: string | null;
  thinking?: string;
  thinkingBlocks?: string[];
  rowId?: number;
};

export type ConversationRow = {
  sessionId: string;
  userId: string | null;
  createdAt: string;
  question: string;
  normalizedQuestion: string;
  thinking: HistoryEntry[];
  answer: HistoryEntry | null;
};

function normalizeContent(value: unknown): string {
  if (typeof value === "string") return value;
  if (value == null) return "";
  if (Array.isArray(value)) {
    return value
      .map((part) => {
        if (typeof part === "string") return part;
        if (part && typeof part === "object" && "text" in part) {
          const maybeText = (part as { text?: unknown }).text;
          return typeof maybeText === "string" ? maybeText : JSON.stringify(part);
        }
        return JSON.stringify(part);
      })
      .join("\n");
  }
  if (typeof value === "object") {
    if ("text" in value && typeof (value as { text?: unknown }).text === "string") {
      return (value as { text: string }).text;
    }
    return JSON.stringify(value);
  }
  return String(value);
}

const THINKING_REGEX = /<thinking>([\s\S]*?)<\/thinking>/gi;

function separateThinking(
  text: string,
): { thinking: string; rest: string; hadThinking: boolean; blocks: string[] } {
  if (!text) return { thinking: "", rest: "", hadThinking: false, blocks: [] };
  const matches = [...text.matchAll(THINKING_REGEX)];
  if (!matches.length && /<thinking>/i.test(text)) {
    return {
      thinking: "",
      rest: text.replace(/<\/?thinking>/gi, "").trim(),
      hadThinking: false,
      blocks: [],
    };
  }
  if (!matches.length) return { thinking: "", rest: text, hadThinking: false, blocks: [] };
  const thinkingParts = matches.map((match) => match[1].trim()).filter(Boolean);
  const rest = text.replace(THINKING_REGEX, "").trim();
  return {
    thinking: thinkingParts.join("\n\n"),
    rest,
    hadThinking: true,
    blocks: thinkingParts,
  };
}

function normalizeQuestionText(text: string): string {
  return text.trim().toLowerCase().replace(/\s+/g, " ");
}

function toHistoryEntry(row: RawHistoryRow): HistoryEntry {
  const message =
    typeof row.message === "string" && row.message.length
      ? JSON.parse(row.message)
      : (row.message as Record<string, any>) ?? {};
  const data = (message?.data as Record<string, any>) ?? {};
  const rawContent = normalizeContent(data.content ?? message?.content ?? "");
  const rawThinking = normalizeContent(
    data?.additional_kwargs?.thinking ?? data?.thinking ?? "",
  );
  const feedback = data?.additional_kwargs?.feedback;
  const role = String(message?.role ?? data?.role ?? message?.type ?? "unknown");
  const type = String(message?.type ?? data?.type ?? "unknown");
  let content = rawContent;
  let thinking: string | undefined;
  let thinkingBlocks: string[] | undefined;
  const isHuman = role.toLowerCase() === "human" || type.toLowerCase() === "human";
  const isThinkingOnly = type.toLowerCase() === "ai_thinking" || role.toLowerCase() === "ai_thinking";

  if (!isHuman) {
    if (isThinkingOnly) {
      thinking = rawThinking || rawContent;
      thinkingBlocks = thinking ? [thinking] : [];
      content = "";
    } else {
      const separated = separateThinking(rawContent);
      thinking = separated.thinking || rawThinking;
      thinkingBlocks = separated.blocks && separated.blocks.length ? separated.blocks : undefined;
      const cleanedRest = separated.rest.replace(THINKING_REGEX, "").trim();
      const fallbackRest = separated.rest.trim();
      const strippedRaw = rawContent.replace(THINKING_REGEX, "").trim();
      let resolved = cleanedRest || fallbackRest || strippedRaw;
      if (!resolved || resolved.length === 0) {
        resolved = "";
      }
      content = resolved;
      if (!strippedRaw && !separated.hadThinking) {
        thinking = thinking || undefined;
        thinkingBlocks = thinkingBlocks || (thinking ? [thinking] : undefined);
      }
    }
  }

  return {
    rowId: row.id,
    sessionId: row.session_id,
    userId: typeof row.user_id === "string" ? row.user_id : null,
    createdAt: row.created_at,
    role,
    type,
    content,
    feedback:
      typeof feedback === "number" || typeof feedback === "string"
        ? String(feedback)
        : "—",
    feedbackText: row.feedback_text?.trim() ?? "",
    messageId: typeof message?.id === "string" ? message.id : null,
    thinking,
    thinkingBlocks,
  };
}

export async function fetchHistoryPage(
  limit = 200,
  offset = 0,
): Promise<{ entries: HistoryEntry[]; totalRows: number; databaseAvailable: boolean }> {
  if (!isPgConfigured()) return { entries: [], totalRows: 0, databaseAvailable: false };
  const safeLimit = Math.max(limit, 1);
  let client: PoolClient;
  try {
    client = await getPgPool().connect();
  } catch {
    return { entries: [], totalRows: 0, databaseAvailable: false };
  }
  try {
    let totalRows = 0;
    try {
      const totalResult = await client.query<{ count: string }>(
        "SELECT reltuples::bigint::text AS count FROM pg_class WHERE oid = $1::regclass",
        [CHAT_HISTORY_TABLE],
      );
      totalRows = parseInt(totalResult.rows[0]?.count ?? "0", 10);
    } catch {
      totalRows = 0;
    }

    const query = `
      SELECT
        id,
        session_id::text AS session_id,
        user_id,
        message,
        feedback_text,
        to_char(created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"') AS created_at
      FROM ${CHAT_HISTORY_TABLE}
      ORDER BY id DESC
      LIMIT $1
      OFFSET $2
    `;
    const result = await client.query<RawHistoryRow>(query, [safeLimit, Math.max(offset, 0)]);
    return {
      entries: result.rows.map(toHistoryEntry),
      totalRows: Math.max(totalRows, offset + result.rows.length),
      databaseAvailable: true,
    };
  } catch {
    return { entries: [], totalRows: 0, databaseAvailable: false };
  } finally {
    client.release();
  }
}

export async function fetchHistoryEntries(limit = 200): Promise<HistoryEntry[]> {
  const page = await fetchHistoryPage(limit, 0);
  return page.entries;
}

function compareTimestampsAsc(a: string, b: string): number {
  if (a === b) return 0;
  return a < b ? -1 : 1;
}

function compareTimestampsDesc(a: string, b: string): number {
  if (a === b) return 0;
  return a < b ? 1 : -1;
}

function compareRowIdsDesc(a?: number, b?: number): number {
  if (typeof a !== "number" || typeof b !== "number") return 0;
  if (a === b) return 0;
  return a < b ? 1 : -1;
}

function compareRowIdsAsc(a?: number, b?: number): number {
  if (typeof a !== "number" || typeof b !== "number") return 0;
  if (a === b) return 0;
  return a < b ? -1 : 1;
}

function pushThinkingBlocks(target: HistoryEntry[], source: HistoryEntry) {
  const blocks = (source.thinkingBlocks && source.thinkingBlocks.length
    ? source.thinkingBlocks
    : source.thinking
      ? [source.thinking]
      : []
  ).filter((val) => (val ?? "").toString().trim().length > 0);

  if (!blocks.length) return;

  for (const [idx, block] of blocks.entries()) {
    target.push({
      ...source,
      thinking: block,
      content: "",
      messageId: source.messageId ? `${source.messageId}:think#${idx + 1}` : source.messageId,
    });
  }
}

export function groupIntoConversations(entries: HistoryEntry[]): ConversationRow[] {
  // Bucket by session first to avoid interleaving rows across threads.
  const sessions = new Map<string, HistoryEntry[]>();
  for (const entry of entries) {
    if (!sessions.has(entry.sessionId)) {
      sessions.set(entry.sessionId, []);
    }
    sessions.get(entry.sessionId)!.push(entry);
  }

  const rows: ConversationRow[] = [];

  for (const sessionEntries of sessions.values()) {
    const sessionUserId =
      sessionEntries.find((entry) => entry.userId)?.userId ?? null;
    const chronological = [...sessionEntries].sort((a, b) =>
      compareTimestampsAsc(a.createdAt, b.createdAt) || compareRowIdsAsc(a.rowId, b.rowId),
    );

    let current: ConversationRow | null = null;
    const pendingThinking: HistoryEntry[] = [];
    let pendingFinal: HistoryEntry | null = null;
    let addedRowForSession = false;

    for (const entry of chronological) {
      const role = entry.role.toLowerCase();
      const type = entry.type.toLowerCase();
      const isHuman = role === "human" || type === "human";
      const isThinking = type === "ai_thinking";
      const hasThinkingOnly = !!entry.thinking && (!entry.content || entry.content.trim() === "");
      const isFinal = !isThinking && !isHuman && !hasThinkingOnly;

      if (isHuman) {
        current = {
          sessionId: entry.sessionId,
          userId: entry.userId ?? sessionUserId,
          createdAt: entry.createdAt,
          question: entry.content,
          normalizedQuestion: normalizeQuestionText(entry.content),
          thinking: [],
          answer: null,
        };
        if (pendingThinking.length) {
          current.thinking.push(...pendingThinking);
          pendingThinking.length = 0;
        }
        if (pendingFinal) {
          pushThinkingBlocks(current.thinking, pendingFinal);
          current.answer = pendingFinal;
          pendingFinal = null;
        }
        rows.push(current);
        addedRowForSession = true;
        continue;
      }

      if (!current) {
        // Buffer AI/thinking until we see the first human message for this session.
        if (isThinking || hasThinkingOnly) {
          pushThinkingBlocks(pendingThinking, entry);
        } else if (isFinal) {
          pushThinkingBlocks(pendingThinking, entry);
          pendingFinal = entry;
        }
        continue;
      }

      if (isThinking || hasThinkingOnly) {
        pushThinkingBlocks(current.thinking, entry);
        continue;
      }

      if (isFinal) {
        // Some providers embed thinking inside the final message; surface it alongside dedicated thinking rows.
        pushThinkingBlocks(current.thinking, entry);
        // Always keep a single row per human turn; the last seen final message wins.
        current.answer = entry;
      }
    }

    if (!addedRowForSession && (pendingThinking.length || pendingFinal)) {
      const createdAt =
        pendingFinal?.createdAt ??
        pendingThinking[0]?.createdAt ??
        new Date().toISOString();
      const synthetic: ConversationRow = {
        sessionId: sessionEntries[0]?.sessionId ?? "",
        userId: sessionUserId,
        createdAt,
        question: "(missing question)",
        normalizedQuestion: "",
        thinking: [...pendingThinking],
        answer: pendingFinal,
      };
      rows.push(synthetic);
    }
  }

  // Present newest-first across all sessions; prefer row id ordering when available.
  return rows.sort((a, b) => {
    const aRowId = a.answer?.rowId ?? a.thinking.at(-1)?.rowId;
    const bRowId = b.answer?.rowId ?? b.thinking.at(-1)?.rowId;
    const byRowId = compareRowIdsDesc(aRowId, bRowId);
    if (byRowId !== 0) return byRowId;
    return compareTimestampsDesc(a.createdAt, b.createdAt);
  });
}

export function isHistoryConfigured() {
  return isPgConfigured();
}

export { CHAT_HISTORY_TABLE };
