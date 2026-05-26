---
name: google-form-response-store-query
description: Use this skill for natural-language querying of the Postgres response store behind Google Forms. It is for existing forms and stored responses, not for creating new forms.
license: MIT
compatibility: "Requires the local Postgres response-store tools exposed by this repo: inspect_form_response_database and query_form_response_database."
---

# google-form-response-store-query

## Overview

This skill handles questions about form data that is already stored in Postgres.

Use it when the user is asking about:
- an existing `form_id`
- responses already stored in the database
- rankings, counts, filters, summaries, comparisons, or record lookups on stored form data

Do not use it for:
- creating a new Google Form
- editing a Google Form definition
- linking a spreadsheet for analysis unless the user is asking about the SQL store itself

## Workflow

1. Resolve the target form.
   - Prefer an explicit `form_id`.
   - If the user refers to `this form` or a previously created form in the same thread, use that thread-local form context when available.

2. Inspect the SQL store if needed.
   - Use `inspect_form_response_database` for schema/table questions.
   - Use it once up front if you need to confirm table or column names before writing SQL.

3. Query with read-only SQL.
   - Use `query_form_response_database`.
   - Only generate `SELECT` or `WITH` queries.
   - Prefer direct SQL that answers the user's request without extra joins or formatting layers.

4. Keep the query intent separate from form creation.
   - If the user is asking about an existing form id, treat that as a database query context unless they explicitly say to create a new form.
   - Do not interpret generic words like `form`, `ฟอร์ม`, or `แบบฟอร์ม` as creation intent by themselves.

5. Report results clearly.
   - State which form id was used when relevant.
   - Summarize the result briefly before showing rows.
   - Use markdown tables for row-oriented results.

## Query Planning Rules

- For simple row lookups, query `form_responses` and `form_response_answers`.
- For form metadata, query `agent_forms`.
- For free-form analytics questions, prefer one read-only SQL query that answers the question directly.
- If the user asks for ranking, counts, top or bottom results, or grouped summaries, do that in SQL rather than narrating from memory.
- If no rows match, say so directly.

## Safety Rules

- Never generate write SQL.
- Never use `INSERT`, `UPDATE`, `DELETE`, `ALTER`, `DROP`, `CREATE`, transaction control, or other non-read-only commands.
- Do not invent schema fields that are not present in the response store.
