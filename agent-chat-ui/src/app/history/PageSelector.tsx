"use client";

type PageSelectorProps = {
  page: number;
  pageSize: number;
  totalPages: number;
};

export function PageSelector({ page, pageSize, totalPages }: PageSelectorProps) {
  const safeTotal = Math.max(1, totalPages);
  const safePage = Math.min(Math.max(page, 1), safeTotal);

  return (
    <form action="/history" method="GET" className="flex items-center gap-2">
      <input type="hidden" name="pageSize" value={pageSize} />
      <label htmlFor="page-select" className="text-sm text-slate-600">
        Page
      </label>
      <select
        id="page-select"
        name="page"
        defaultValue={safePage}
        onChange={(event) => event.currentTarget.form?.submit()}
        className="rounded border border-slate-300 bg-white px-2 py-1 text-sm"
      >
        {Array.from({ length: safeTotal }, (_, idx) => idx + 1).map((p) => (
          <option key={p} value={p}>
            {p}
          </option>
        ))}
      </select>
    </form>
  );
}
