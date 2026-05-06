/* eslint-disable react-refresh/only-export-components */
import { Metadata } from "next";
import { Button } from "@/components/ui/button";
import {
  CHAT_HISTORY_TABLE,
  fetchHistoryPage,
  groupIntoConversations,
  isHistoryConfigured,
} from "@/lib/server/history-report";
import { LocalTime } from "./LocalTime";
import { PageSelector } from "./PageSelector";
import { TimeZoneField } from "./TimeZoneField";

export const metadata: Metadata = {
  title: "Chat History Report",
};

export const revalidate = 0;

type HistoryPageProps = {
  searchParams?: Promise<Record<string, string | string[] | undefined>>;
};

export default async function HistoryReportPage({ searchParams }: HistoryPageProps) {
  const resolvedSearchParams = (await searchParams) ?? {};
  if (!isHistoryConfigured()) {
    return (
      <div className="mx-auto max-w-4xl p-6">
        <h1 className="text-2xl font-semibold">Chat History Report</h1>
        <p className="mt-4 text-muted-foreground">
          PG_CONN_STR is not configured on this deployment. Set the connection string to
          enable the report.
        </p>
      </div>
    );
  }

  const pageParam = Array.isArray(resolvedSearchParams.page)
    ? resolvedSearchParams.page[0]
    : resolvedSearchParams.page;
  const pageSizeParam = Array.isArray(resolvedSearchParams.pageSize)
    ? resolvedSearchParams.pageSize[0]
    : resolvedSearchParams.pageSize;
  const page = Math.max(parseInt(pageParam ?? "1", 10) || 1, 1);
  const pageSize = Math.max(Math.min(parseInt(pageSizeParam ?? "50", 10) || 50, 200), 10);
  const offset = (page - 1) * pageSize;

  const { entries, totalRows, databaseAvailable } = await fetchHistoryPage(pageSize, offset);
  const conversations = groupIntoConversations(entries);
  const totalPages = Math.max(1, Math.ceil(totalRows / pageSize));
  const hasPrev = page > 1;
  const hasNext = page < totalPages;

  return (
    <div className="mx-auto w-full max-w-full p-6">
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div>
          <h1 className="text-3xl font-semibold tracking-tight">Chat History Report</h1>
          <p className="mt-1 text-muted-foreground">
            Page {page} of {totalPages} · {totalRows} rows from {CHAT_HISTORY_TABLE}.
          </p>
        </div>
        <form
          className="flex items-center gap-2"
          action="/history/export"
          method="GET"
        >
          <TimeZoneField />
          <label
            htmlFor="export-format"
            className="text-sm text-slate-600"
          >
            Format
          </label>
          <select
            id="export-format"
            name="format"
            className="rounded border border-slate-300 bg-white px-2 py-1 text-sm"
            defaultValue="xlsx"
          >
            <option value="xlsx">Excel (.xlsx)</option>
            <option value="csv">CSV (.csv)</option>
          </select>
          <Button type="submit">Export</Button>
        </form>
      </div>

      <div className="mt-6 flex flex-wrap items-center justify-center gap-3">
        <form action="/history" method="GET">
          <input type="hidden" name="pageSize" value={pageSize} />
          <input type="hidden" name="page" value={Math.max(page - 1, 1)} />
          <Button
            type="submit"
            variant="outline"
            disabled={!hasPrev}
            className="w-32 justify-center"
          >
            Previous page
          </Button>
        </form>
        <PageSelector page={page} pageSize={pageSize} totalPages={totalPages} />
        <form action="/history" method="GET">
          <input type="hidden" name="pageSize" value={pageSize} />
          <input type="hidden" name="page" value={page + 1} />
          <Button
            type="submit"
            variant="outline"
            disabled={!hasNext}
            className="w-32 justify-center"
          >
            Next page
          </Button>
        </form>
      </div>

      {!databaseAvailable && (
        <div className="mt-4 rounded-md border border-amber-300 bg-amber-50 px-4 py-3 text-sm text-amber-900">
          PostgreSQL is configured but currently unreachable. Showing empty results.
        </div>
      )}

      <div className="mt-6 overflow-x-auto overflow-y-visible rounded-lg border border-slate-200 shadow-sm">
        <table className="min-w-full divide-y divide-slate-200">
          <thead className="bg-slate-50">
            <tr>
              <th className="px-4 py-3 text-left text-xs font-semibold uppercase tracking-wider text-slate-600">
                Created At
              </th>
              <th className="px-4 py-3 text-left text-xs font-semibold uppercase tracking-wider text-slate-600">
                Session ID
              </th>
              <th className="px-4 py-3 text-left text-xs font-semibold uppercase tracking-wider text-slate-600">
                User ID
              </th>
              <th className="px-4 py-3 text-left text-xs font-semibold uppercase tracking-wider text-slate-600">
                Question
              </th>
              <th className="px-4 py-3 text-left text-xs font-semibold uppercase tracking-wider text-slate-600">
                Thinking
              </th>
              <th className="px-4 py-3 text-left text-xs font-semibold uppercase tracking-wider text-slate-600">
                Answers
              </th>
              <th className="px-4 py-3 text-left text-xs font-semibold uppercase tracking-wider text-slate-600">
                Feedback
              </th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100 bg-white">
            {conversations.map((conversation, index) => (
              <tr
                key={`${conversation.sessionId}-${conversation.createdAt}-${index}`}
              >
                <td className="whitespace-nowrap px-4 py-3 text-sm text-slate-700">
                  <LocalTime value={conversation.createdAt} />
                </td>
                <td className="max-w-[240px] truncate px-4 py-3 text-sm font-mono text-slate-700">
                  {conversation.sessionId}
                </td>
                <td className="max-w-[220px] truncate px-4 py-3 text-sm font-mono text-slate-700">
                  {conversation.userId || "—"}
                </td>
                <td className="px-4 py-3 text-sm text-slate-800">
                  <pre className="max-h-48 overflow-y-auto whitespace-pre-wrap rounded bg-slate-50 p-2 text-xs text-slate-800">
                    {conversation.question}
                  </pre>
                </td>
                <td className="px-4 py-3 text-sm text-slate-800">
                  {conversation.thinking && conversation.thinking.length ? (
                    <div className="flex flex-col gap-3">
                      {conversation.thinking.map((response, idx) => (
                        <div
                          key={`thinking-${response.createdAt}-${response.messageId ?? "think"}-${idx}`}
                          className="rounded border border-slate-200 bg-slate-50 p-3"
                        >
                          <div className="text-xs text-slate-600">
                            <LocalTime value={response.createdAt} />
                          </div>
                          <pre className="mt-2 max-h-48 overflow-y-auto whitespace-pre-wrap text-[11px] leading-snug text-slate-900">
                            {response.thinking}
                          </pre>
                        </div>
                      ))}
                    </div>
                  ) : (
                    <span className="text-slate-500">No thinking recorded.</span>
                  )}
                </td>
                <td className="px-4 py-3 text-sm text-slate-800">
                  {conversation.answer && (conversation.answer.content?.trim()?.length ?? 0) > 0 ? (
                    <div className="flex flex-col gap-3">
                      <div
                        key={`${conversation.answer.createdAt}-${conversation.answer.messageId ?? "resp"}`}
                        className="rounded border border-slate-100 bg-slate-50 p-3"
                      >
                        <div className="text-xs text-slate-500">
                          <LocalTime value={conversation.answer.createdAt} />
                        </div>
                        <pre className="mt-2 max-h-48 overflow-y-auto whitespace-pre-wrap text-xs text-slate-900">
                          {conversation.answer.content}
                        </pre>
                      </div>
                    </div>
                  ) : (
                    <span className="text-slate-500">No answers recorded.</span>
                  )}
                </td>
                <td className="relative px-4 py-3 text-center text-sm text-slate-800">
                  {conversation.answer?.feedbackText ? (
                    <details className="relative inline-block text-left">
                      <summary
                        className="cursor-pointer list-none font-mono text-xs text-slate-600 underline decoration-dotted"
                        title={conversation.answer.feedbackText}
                      >
                        {conversation.answer.feedback}
                      </summary>
                      <div className="absolute right-0 z-20 mt-2 min-w-36 max-w-80 rounded border border-slate-200 bg-white p-2 text-left shadow-lg">
                        <div className="mb-1 text-[10px] font-semibold uppercase tracking-wide text-slate-500">
                          Feedback text
                        </div>
                        <p className="max-h-48 overflow-y-auto whitespace-pre-wrap break-words text-xs leading-relaxed text-slate-700">
                          {conversation.answer.feedbackText}
                        </p>
                      </div>
                    </details>
                  ) : (
                    <span className="font-mono text-xs text-slate-600">
                      {conversation.answer?.feedback ?? "No feedback."}
                    </span>
                  )}
                </td>
              </tr>
            ))}
            {conversations.length === 0 && (
              <tr>
                <td
                  colSpan={7}
                  className="px-4 py-6 text-center text-sm text-slate-500"
                >
                  No history rows found.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
      <div className="mt-4 flex flex-wrap items-center justify-center gap-3">
        <form action="/history" method="GET">
          <input type="hidden" name="pageSize" value={pageSize} />
          <input type="hidden" name="page" value={Math.max(page - 1, 1)} />
          <Button
            type="submit"
            variant="outline"
            disabled={!hasPrev}
            className="w-32 justify-center"
          >
            Previous page
          </Button>
        </form>
        <PageSelector page={page} pageSize={pageSize} totalPages={totalPages} />
        <form action="/history" method="GET">
          <input type="hidden" name="pageSize" value={pageSize} />
          <input type="hidden" name="page" value={page + 1} />
          <Button
            type="submit"
            variant="outline"
            disabled={!hasNext}
            className="w-32 justify-center"
          >
            Next page
          </Button>
        </form>
      </div>
    </div>
  );
}
