---
name: google-form-author
description: Use this skill when creating, editing, or planning Google Forms. It gives the agent a reliable workflow for converting a user request into a Google Form through the google-forms-mcp tools.
license: MIT
compatibility: Requires the Google Forms MCP tools and Google OAuth credentials configured for this project.
---

# google-form-author

## Overview

This skill helps the agent create practical Google Forms for NECTEC users using the bundled Google Forms MCP server.

## Workflow

1. Understand the request.
   - Identify the form title.
   - Identify every question the user asked for.
   - Infer simple missing labels only when obvious.
   - Ask a clarification question only when the form cannot be created safely.

2. Create the form.
   - Call the MCP `create_form` tool first.
   - Pass only the form title to `create_form`.
   - Do not include description, questions, settings, or item payloads in the create call.
   - The Google Forms API only allows `info.title` during initial form creation.

3. Add questions after creation.
   - Use text questions for names, emails, IDs, comments, explanations, and open responses.
   - Use multiple choice questions when the user gives a fixed set of options.
   - For rating requests such as "1 to 5", create a multiple choice question with options `1`, `2`, `3`, `4`, and `5`.
   - Add one question at a time unless the MCP tool explicitly supports batching.

4. Report the result.
   - Summarize the form title and questions added.
   - Include any form ID, edit URL, responder URL, or other URL returned by the MCP tools.
   - If a tool fails, clearly say which step failed and do not claim the form was created.

## Google Forms Authoring Conventions

- Prefer concise, user-facing wording.
- Keep question text direct and easy to answer.
- Avoid adding extra questions the user did not request unless they are necessary for the stated purpose.
- Preserve the user's language when they write the request in a specific language.
- Do not invent a working link if the MCP tools do not return one.

## Common Mappings

- "name" -> short text question: "Name"
- "email" -> short text question: "Email"
- "rating from 1 to 5" -> multiple choice question with options 1 through 5
- "comments" or "feedback" -> paragraph/open text question
- "department", "role", or "session" with listed choices -> multiple choice question
