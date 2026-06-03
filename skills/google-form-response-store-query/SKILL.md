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

## SQL Patterns To Reuse

Use these patterns before asking a weak local model to invent SQL. Replace `<form_id>` and question titles with real values from `agent_forms` / `form_response_answers`.

Score distribution:

```sql
WITH scores AS (
  SELECT
    COALESCE(
      NULLIF(fr.response_json->>'totalScore', '')::numeric,
      (
        SELECT SUM((answer.answer_obj->'grade'->>'score')::numeric)
        FROM jsonb_each(fr.response_json->'answers') AS answer(question_id, answer_obj)
        WHERE answer.answer_obj->'grade'->>'score' IS NOT NULL
      )
    ) AS total_score
  FROM form_responses fr
  WHERE fr.form_id = '<form_id>'
)
SELECT total_score, COUNT(*) AS response_count
FROM scores
WHERE total_score IS NOT NULL
GROUP BY total_score
ORDER BY total_score ASC;
```

Count respondents by a stored answer field, such as `รุ่นการอบรม`, `ชื่อโรงเรียน`, or a resolved organization field:

```sql
SELECT
  COALESCE(NULLIF(BTRIM(answer_text), ''), '-') AS group_value,
  COUNT(DISTINCT response_id) AS response_count
FROM form_response_answers
WHERE form_id = '<form_id>'
  AND question_title = '<actual question title>'
GROUP BY COALESCE(NULLIF(BTRIM(answer_text), ''), '-')
ORDER BY response_count DESC, group_value ASC;
```

Count respondents with a score filter grouped by a stored answer field:

```sql
WITH scores AS (
  SELECT
    fr.response_id,
    COALESCE(
      NULLIF(fr.response_json->>'totalScore', '')::numeric,
      (
        SELECT SUM((answer.answer_obj->'grade'->>'score')::numeric)
        FROM jsonb_each(fr.response_json->'answers') AS answer(question_id, answer_obj)
        WHERE answer.answer_obj->'grade'->>'score' IS NOT NULL
      )
    ) AS total_score
  FROM form_responses fr
  WHERE fr.form_id = '<form_id>'
),
groups AS (
  SELECT response_id, answer_text AS group_value
  FROM form_response_answers
  WHERE form_id = '<form_id>'
    AND question_title = '<actual group question title>'
)
SELECT
  COALESCE(NULLIF(BTRIM(groups.group_value), ''), '-') AS group_value,
  COUNT(DISTINCT scores.response_id) AS response_count
FROM scores
JOIN groups ON groups.response_id = scores.response_id
WHERE scores.total_score > 8
GROUP BY COALESCE(NULLIF(BTRIM(groups.group_value), ''), '-')
ORDER BY response_count DESC, group_value ASC;
```

Average score by a stored answer field:

```sql
WITH scores AS (
  SELECT
    fr.response_id,
    COALESCE(
      NULLIF(fr.response_json->>'totalScore', '')::numeric,
      (
        SELECT SUM((answer.answer_obj->'grade'->>'score')::numeric)
        FROM jsonb_each(fr.response_json->'answers') AS answer(question_id, answer_obj)
        WHERE answer.answer_obj->'grade'->>'score' IS NOT NULL
      )
    ) AS total_score
  FROM form_responses fr
  WHERE fr.form_id = '<form_id>'
),
groups AS (
  SELECT response_id, answer_text AS group_value
  FROM form_response_answers
  WHERE form_id = '<form_id>'
    AND question_title = '<actual group question title>'
)
SELECT
  COALESCE(NULLIF(BTRIM(groups.group_value), ''), '-') AS group_value,
  COUNT(DISTINCT scores.response_id) AS response_count,
  ROUND(AVG(scores.total_score), 2) AS avg_score
FROM scores
JOIN groups ON groups.response_id = scores.response_id
WHERE scores.total_score IS NOT NULL
GROUP BY COALESCE(NULLIF(BTRIM(groups.group_value), ''), '-')
ORDER BY avg_score DESC NULLS LAST, response_count DESC;
```

Most-wrong questions when correct answers have a populated grade but wrong answers may have an empty `{}` grade:

```sql
WITH correct_answers AS (
  SELECT answer.question_id, fr.response_id
  FROM form_responses fr
  JOIN LATERAL jsonb_each(fr.response_json->'answers') AS answer(question_id, answer_obj) ON TRUE
  WHERE fr.form_id = '<form_id>'
    AND (
      answer.answer_obj->'grade'->>'correct' = 'true'
      OR (
        answer.answer_obj->'grade'->>'score' IS NOT NULL
        AND (answer.answer_obj->'grade'->>'score')::numeric > 0
      )
    )
),
gradable_answers AS (
  SELECT fa.question_id, fa.question_title, fa.response_id
  FROM form_response_answers fa
  WHERE fa.form_id = '<form_id>'
    AND fa.question_id IN (SELECT DISTINCT question_id FROM correct_answers)
)
SELECT
  ga.question_title,
  COUNT(DISTINCT ga.response_id) - COUNT(DISTINCT ca.response_id) AS wrong_response_count
FROM gradable_answers ga
LEFT JOIN correct_answers ca
  ON ca.question_id = ga.question_id
 AND ca.response_id = ga.response_id
GROUP BY ga.question_id, ga.question_title
HAVING COUNT(DISTINCT ga.response_id) - COUNT(DISTINCT ca.response_id) > 0
ORDER BY wrong_response_count DESC, question_title ASC
LIMIT 20;
```

Top scorer with respondent identity resolved from likely name fields:

```sql
WITH scores AS (
  SELECT
    fr.response_id,
    fr.respondent_email,
    COALESCE(
      NULLIF(fr.response_json->>'totalScore', '')::numeric,
      (
        SELECT SUM((answer.answer_obj->'grade'->>'score')::numeric)
        FROM jsonb_each(fr.response_json->'answers') AS answer(question_id, answer_obj)
        WHERE answer.answer_obj->'grade'->>'score' IS NOT NULL
      )
    ) AS total_score
  FROM form_responses fr
  WHERE fr.form_id = '<form_id>'
),
ranked AS (
  SELECT response_id, respondent_email, total_score
  FROM scores
  WHERE total_score IS NOT NULL
  ORDER BY total_score DESC, response_id ASC
  LIMIT 50
),
identity_answers AS (
  SELECT DISTINCT ON (fra.response_id)
    fra.response_id,
    fra.answer_text AS fallback_name
  FROM form_response_answers fra
  WHERE fra.form_id = '<form_id>'
    AND (
      regexp_replace(lower(fra.question_title), '[[:space:]_\\-/\\\\|():]+', '', 'g') IN ('ชื่อนามสกุล', 'fullname')
      OR fra.question_title ILIKE '%name%'
      OR fra.question_title LIKE '%ชื่อ%'
    )
    AND NULLIF(BTRIM(fra.answer_text), '') IS NOT NULL
  ORDER BY fra.response_id, fra.question_title ASC
)
SELECT
  COALESCE(identity_answers.fallback_name, NULLIF(ranked.respondent_email, ''), ranked.response_id) AS name,
  ranked.total_score
FROM ranked
LEFT JOIN identity_answers ON identity_answers.response_id = ranked.response_id
ORDER BY ranked.total_score DESC, name ASC
LIMIT 1;
```

Useful schema discovery queries:

```sql
SELECT form_id, form_title, updated_at
FROM agent_forms
ORDER BY updated_at DESC NULLS LAST;

SELECT question_id, question_title, COUNT(*) AS answer_count
FROM form_response_answers
WHERE form_id = '<form_id>'
GROUP BY question_id, question_title
ORDER BY question_title ASC;
```

## Safety Rules

- Never generate write SQL.
- Never use `INSERT`, `UPDATE`, `DELETE`, `ALTER`, `DROP`, `CREATE`, transaction control, or other non-read-only commands.
- Do not invent schema fields that are not present in the response store.
