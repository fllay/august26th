import { NextResponse } from "next/server";
import {
  fetchHistoryEntries,
  groupIntoConversations,
} from "@/lib/server/history-report";
import type { ConversationRow } from "@/lib/server/history-report";
import * as XLSX from "xlsx";

type FormatterOptions = {
  locale: string | undefined;
  timeZone: string | undefined;
};

function normalizeTimestamp(value: string): string {
  return value.includes("T") ? value : value.replace(" ", "T").replace(/Z?$/, "Z");
}

function formatTimestamp(value: string, options: FormatterOptions): string {
  const normalized = normalizeTimestamp(value);
  const date = new Date(normalized);
  if (Number.isNaN(date.getTime())) return value;
  const baseOptions = {
    dateStyle: "medium" as const,
    timeStyle: "medium" as const,
  };
  if (!options.timeZone) {
    return new Intl.DateTimeFormat(options.locale, baseOptions).format(date);
  }
  try {
    return new Intl.DateTimeFormat(options.locale, {
      ...baseOptions,
      timeZone: options.timeZone,
    }).format(date);
  } catch {
    return new Intl.DateTimeFormat(options.locale, baseOptions).format(date);
  }
}

function formatBlocks(blocks: string[]): string {
  return blocks.join("\n").trim();
}

function formatThinking(conversation: ConversationRow, options: FormatterOptions): string {
  return conversation.thinking
    .filter((entry) => (entry.thinking ?? entry.content ?? "").trim().length > 0)
    .map((entry, index) => {
      const text = (entry.thinking ?? entry.content ?? "").trim();
      const label = `${index + 1}. [${formatTimestamp(entry.createdAt, options)}]`;
      return `${label} ${text}`.trim();
    })
    .join(" | ");
}

function formatAnswer(conversation: ConversationRow, options: FormatterOptions): string {
  const answer = conversation.answer;
  if (!answer || (answer.content?.trim().length ?? 0) === 0) {
    return "";
  }
  return `[${formatTimestamp(answer.createdAt, options)}] ${answer.content.trim()}`;
}

function formatFeedback(conversation: ConversationRow): string {
  return conversation.answer?.feedback ?? "";
}

function formatFeedbackText(conversation: ConversationRow): string {
  return conversation.answer?.feedbackText ?? "";
}

function createCsv(conversations: ConversationRow[], options: FormatterOptions) {
  const headers = [
    "Created At",
    "Session ID",
    "User ID",
    "Question",
    "Thinking",
    "Answers",
    "Feedback",
    "Feedback Text",
  ];
  const rows = conversations.map((conversation) => {
    const thinkingBlocks = formatThinking(conversation, options);
    const answerBlocks = formatAnswer(conversation, options);

    return [
      formatTimestamp(conversation.createdAt, options),
      conversation.sessionId,
      conversation.userId ?? "",
      conversation.question.replace(/\r?\n/g, " "),
      thinkingBlocks,
      answerBlocks,
      formatFeedback(conversation),
      formatFeedbackText(conversation),
    ];
  });

  const workbook = XLSX.utils.book_new();
  const sheet = XLSX.utils.aoa_to_sheet([headers, ...rows]);
  XLSX.utils.book_append_sheet(workbook, sheet, "History");
  return XLSX.utils.sheet_to_csv(sheet);
}

export async function GET(request: Request) {
  const url = new URL(request.url);
  const format = url.searchParams.get("format")?.toLowerCase() ?? "xlsx";
  const timeZone = url.searchParams.get("tz") ?? undefined;
  const locale = request.headers.get("accept-language")?.split(",")[0];
  const formatterOptions = { locale, timeZone };

  const entries = await fetchHistoryEntries(1000);
  const conversations = groupIntoConversations(entries);

  if (format === "csv") {
    const csv = createCsv(conversations, formatterOptions);
    const filename = `history-report-${new Date().toISOString().slice(0, 10)}.csv`;
    return new NextResponse(csv, {
      headers: {
        "Content-Type": "text/csv; charset=utf-8",
        "Content-Disposition": `attachment; filename="${filename}"`,
        "Cache-Control": "no-store",
      },
    });
  }

  const headers = [
    "Created At",
    "Session ID",
    "User ID",
    "Question",
    "Thinking",
    "Answers",
    "Feedback",
    "Feedback Text",
  ];
  const data = conversations.map((conversation) => {
    const thinkingBlocks = formatThinking(conversation, formatterOptions)
      .split(" | ")
      .filter((block) => block.trim().length > 0);

    const answerBlock = formatAnswer(conversation, formatterOptions);

    return [
      formatTimestamp(conversation.createdAt, formatterOptions),
      conversation.sessionId,
      conversation.userId ?? "",
      conversation.question,
      formatBlocks(thinkingBlocks),
      answerBlock,
      formatFeedback(conversation),
      formatFeedbackText(conversation),
    ];
  });

  const workbook = XLSX.utils.book_new();
  const sheet = XLSX.utils.aoa_to_sheet([headers, ...data]);
  XLSX.utils.book_append_sheet(workbook, sheet, "History");

  const buffer = XLSX.write(workbook, { type: "buffer", bookType: "xlsx" });
  const filename = `history-report-${new Date().toISOString().slice(0, 10)}.xlsx`;

  return new NextResponse(buffer, {
    headers: {
      "Content-Type":
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
      "Content-Disposition": `attachment; filename="${filename}"`,
      "Cache-Control": "no-store",
    },
  });
}
