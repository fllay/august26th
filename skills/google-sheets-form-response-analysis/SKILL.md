---
name: google-sheets-form-response-analysis
description: Analyze spreadsheet data in Google Sheets with the mcp-google-sheets MCP tools, especially Google Form response data. Use this skill when a user wants summaries, counts, trends, comparisons, grouped analysis, response-quality checks, spreadsheet search, anomaly review, formula-aware inspection, written analysis tabs, or charts and graphs created from sheet data.
metadata:
  license: MIT
---

# Google Sheets Form Response Analysis

Analyze Google Sheets data with a repeatable workflow that reads the source tabs first, confirms the structure, computes the requested findings, and writes back tables or charts only when the user asks for a saved output. This skill fits Google Form response sheets especially well, but it should also handle broader spreadsheet analysis requests.

## Workflow

1. Locate the spreadsheet.
   - If the user gives a spreadsheet ID, use it directly.
   - If the user gives a spreadsheet title or folder context, use MCP listing or search tools first.
   - If the user refers to a Google Form response sheet, look for tabs with response-style headers such as `Timestamp`, respondent fields, ratings, and free-text comments.

2. Inspect structure before analyzing.
   - List sheets/tabs before reading data if the spreadsheet layout is unknown.
   - Read the full used range of the relevant tabs by default unless the workbook is clearly too large.
   - Confirm which row is the header row when the sheet is messy or has intro text above the table.
   - Do not ask the user to choose a tab, range, chart type, or column before you have inspected the spreadsheet unless no spreadsheet target was provided.

3. Analyze in-memory first.
   - Count valid rows, blank rows, duplicate-looking rows, and obvious missing answers.
   - Compute the exact analysis the user asked for: totals, averages, medians when useful, rating distribution, grouped counts, trends, comparisons, comment themes, completion issues, date patterns, or anomaly checks.
   - Preserve the raw wording of response options instead of normalizing labels unless the user asks for cleanup.
   - If the user does not specify the analysis type, choose a sensible default analysis from the data shape instead of asking immediately.
   - For requests like `analyze this spreadsheet`, `simple summary`, `summarize the data`, or `make a chart`, decide the first-pass analysis plan yourself from the detected columns and tabs.

4. Write back only when requested.
   - If the user asks for a summary sheet, create a new sheet or write to a clearly named analysis tab.
   - If the user asks for a graph, choose the chart type that best matches the question and underlying data.
   - Keep source data unchanged unless the user explicitly asks to clean or edit it.
   - Prefer compact tables with headings such as `Metric`, `Value`, `Segment`, `Count`, or `Insight`.

5. Report clearly.
   - Summarize what spreadsheet and tab were used.
   - State the row scope used for analysis.
   - Separate computed facts from interpretation.
   - If the sheet structure blocks confident analysis, explain exactly what is missing.

## Working Rules

- Read first, then analyze, then optionally write.
- When the user says `analyze this spreadsheet` without narrowing the scope, analyze all tabs that contain data.
- Do not invent columns, respondent categories, or date meanings.
- Treat the first non-empty row as a candidate header row, but verify it from the values.
- If a spreadsheet has multiple response tabs, ask which tab to analyze unless one is clearly the latest or explicitly named.
- If the user gave a spreadsheet target and a general analysis request, do not bounce the task back for more specificity before inspection.
- If free-text answers are part of the request, provide a concise thematic summary and quote only short fragments when needed.
- When calculating percentages, state the denominator.
- When data quality is poor, surface that before drawing conclusions.
- If the user wants a saved chart, first write or confirm the summary table that the chart should use.
- Use charts to support the analysis, not replace the written summary.
- If the user only says things like `analyze this spreadsheet`, decide the analysis plan yourself from the available columns.
- If the user asks for a graph but does not specify a chart type, inspect first and choose a reasonable chart automatically.

## Default Analysis Heuristics

When the user does not specify a particular analysis type, choose the most useful combination below:

- Always start with:
  - row count
  - missing-value check
  - duplicate-looking row check
  - quick header/column summary
  - cross-tab overview if the spreadsheet has more than one populated tab

- If there is a timestamp or date column:
  - add a time trend summary
  - highlight peaks, drops, or recent changes when visible

- If there are categorical columns such as department, role, location, session, or status:
  - add grouped counts for the most informative categories
  - prefer columns with a manageable number of unique values

- If there are numeric columns:
  - add totals, averages, minima, maxima, and spread-oriented observations when useful

- If there are rating-style columns:
  - add score distribution
  - add average rating
  - add top/bottom rating observations

- If there are free-text columns:
  - add a short theme summary
  - mention repeated concerns, praise, or requests

- If the user also wants a chart but does not specify a chart type:
  - use a column/bar chart for grouped counts
  - use a line chart for time trends
  - use a pie chart only for simple small-category share breakdowns

## Recommended MCP Tool Pattern

Use the smallest useful tool subset when available. The most common tools for this skill are:

- `search_spreadsheets` or `list_spreadsheets` to find the file
- `list_sheets` to inspect tabs
- `get_sheet_data` to read a single tab
- `get_multiple_sheet_data` when the analysis spans several tabs
- `get_sheet_formulas` when formulas or computed columns matter
- `find_in_spreadsheet` when locating a specific value, question, or respondent segment
- `create_sheet` and `update_cells` to write an analysis tab
- `batch_update_cells` when writing a larger summary table
- `add_chart` to create graphs directly in the spreadsheet
- `batch_update` for advanced formatting or chart-related sheet operations

Read [references/mcp-google-sheets.md](references/mcp-google-sheets.md) when you need the MCP authentication modes, the broader tool list, or guidance on choosing a smaller enabled-tool set.

## Common Analysis Patterns

- Rating summary:
  - count responses
  - average score
  - per-score distribution
  - top positive and negative comment themes if comments exist

- Attendance or registration sheet:
  - total registrations
  - grouped counts by department, role, session, or location
  - missing contact fields

- Multi-tab workbook:
  - compare response counts by tab
  - identify tabs with mismatched headers
  - merge findings conceptually in the answer before writing back

- General dataset analysis:
  - grouped summaries by any categorical column
  - trend analysis over time
  - top and bottom categories
  - missing-value review
  - duplicate or suspicious-row checks
  - formula-aware inspection when the sheet contains calculated fields

- Chart and graph requests:
  - bar or column chart for grouped counts
  - line chart for time trends
  - pie chart for simple share breakdowns
  - scatter plot only when the sheet contains sensible numeric x/y pairs
  - write the supporting summary table first if the raw data is too detailed for a direct chart

- Write-back request:
  - create a tab named something like `Analysis Summary`
  - write one compact metrics block and one grouped-results block
  - include charts when requested and when the data shape is appropriate
  - include a timestamp or note only if the user asks
