# mcp-google-sheets Notes

This skill is designed around the `xing5/mcp-google-sheets` MCP server:
https://github.com/xing5/mcp-google-sheets

## Best-fit use in this project

Use the server for Google Form response analysis and broader spreadsheet analysis in Google Sheets. The most useful tasks are:

- finding spreadsheets and tabs
- reading tabular response data
- producing grouped summaries
- writing analysis results back into a new sheet
- inspecting formula-based sheets
- finding specific values or segments
- creating charts from summary tables

## Useful tool subset

For this skill, prefer the smallest useful subset of tools:

- `search_spreadsheets`
- `list_spreadsheets`
- `list_sheets`
- `get_sheet_data`
- `get_multiple_sheet_data`
- `get_sheet_formulas`
- `find_in_spreadsheet`
- `create_sheet`
- `update_cells`
- `batch_update_cells`
- `add_chart`
- `batch_update`

The upstream README also lists these additional tools:

- `add_columns`
- `add_rows`
- `copy_sheet`
- `create_spreadsheet`
- `get_multiple_spreadsheet_summary`
- `list_folders`
- `rename_sheet`
- `share_spreadsheet`

## Authentication modes from upstream

The upstream MCP supports three authentication patterns:

1. Service account
   - recommended for servers and automation
   - uses `SERVICE_ACCOUNT_PATH`
   - usually also uses `DRIVE_FOLDER_ID`

2. OAuth 2.0
   - suitable for interactive personal use
   - uses `CREDENTIALS_PATH`
   - stores a writable token file at `TOKEN_PATH`

3. Direct credential injection
   - suitable for containerized or secret-managed environments
   - inject credential JSON content through environment variables

For this project, service account auth is usually the cleanest option for spreadsheet analysis because it avoids interactive login and works well in Docker.

## Upstream operational notes

- The upstream README recommends `uvx mcp-google-sheets@latest`.
- The upstream server supports tool filtering through `--include-tools` or `ENABLED_TOOLS`.
- Tool filtering is useful because the full tool set consumes significantly more context than a narrow read/write subset.

## Analysis workflow reminder

When using this MCP for form responses or general analysis:

1. find the spreadsheet
2. inspect tabs
3. read the response range
4. compute the result in the agent
5. write a summary tab only if requested
6. add charts only when they help answer the user's question

Keep source tabs untouched unless the user explicitly asks to clean or edit them.
