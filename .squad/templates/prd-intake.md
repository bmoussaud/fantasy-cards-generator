# PRD Intake

On-demand reference for ingesting a PRD, decomposing it into work items, and managing updates.

**Repository gate:** Follow [change intake](../../docs/issue-to-copilot-workflow.md).
A PRD, decomposition table or offline backlog is not a qualified GitHub issue or
approval to implement. Discuss unanswered questions, obtain all nine members'
real input and separately assess @copilot. Deduplicate/create qualified GitHub
issues with one primary owner each before requester approval and execution.
If GitHub/input is unavailable, keep draft qualification and report the blocker.
Existing PRD change-detection metadata below is not an approval hash or verifier.

## Triggers

| User says | Action |
|-----------|--------|
| "here's the PRD" / "work from this spec" | Expect file path or pasted content |
| "read the PRD at {path}" | Read the file at that path |
| "the PRD changed" / "updated the spec" | Re-read and diff against previous decomposition |
| (pastes requirements text) | Treat as inline PRD |

## Intake Flow

1. **Detect source:** File path, pasted text, or URL. Store a reference in `.squad/team.md` under `## PRD Source`.
2. **Store PRD reference:**
   ```markdown
   ## PRD Source

   **Path:** {path-or-inline}
   **Ingested:** {ISO date}
   **Hash:** {sha256 of content, for change detection}
   ```
3. **Spawn Lead (sync, premium bump)** with decomposition prompt (see below).
4. **Qualify and present GitHub issues**, with bounded scopes, actual team input and one primary owner each, for explicit requester approval.
5. **On approval of those scopes:** Record the real chat/comment context and route with issue/scope/approval, respecting dependency order. Do not apply execution labels or assignees during intake.

## Lead Decomposition Spawn Template

```
You are the Lead, decomposing a PRD into actionable work items.

PRD CONTENT:
{full PRD text}

TEAM ROSTER:
{roster from team.md}

TASK: Break this PRD into discrete, implementable work items. For each item provide:
- Title (imperative mood, concise)
- Description (acceptance criteria, technical notes)
- Estimated complexity: S / M / L
- Dependencies (list other item titles this blocks on)
- Suggested assignee (agent name from roster, based on expertise match)

OUTPUT FORMAT:
Return a markdown table:

| # | Title | Complexity | Dependencies | Assignee | Status |
|---|-------|-----------|--------------|----------|--------|
| 1 | {title} | {S/M/L} | — | {agent} | pending |

RULES:
- Items must be independently implementable (no item requires partial completion of another).
- Maximum 1 day of work per item (split larger items).
- Respect team expertise — don't assign frontend work to a backend specialist.
- Order by dependency graph (items with no deps first).
- Flag any ambiguities or missing information as "⚠️ Needs clarification: {question}".
```

## Work Item Presentation Format

Present to user as:

```
📋 PRD decomposed into {N} work items:

| # | Title | Size | Depends on | Assignee |
|---|-------|------|-----------|----------|
| 1 | ... | S | — | {Agent} |
| 2 | ... | M | #1 | {Agent} |

Ready to proceed? I'll route items respecting the dependency order.
⚠️ Clarifications needed: {list any flagged items}
```

## Mid-Project Updates

When the user says the PRD changed:

1. Re-read the PRD content.
2. Compute diff against stored hash.
3. Spawn Lead (sync) with a delta-decomposition prompt:
   - Show only NEW or CHANGED sections.
   - Ask Lead to identify: new items, modified items, obsoleted items.
4. Present changes to user:
   ```
   📋 PRD update detected:
   - New items: {count}
   - Modified: {count}
   - Obsoleted: {count} (will be cancelled if approved)

   {table of changes}

   Approve these updates?
   ```
5. Requalify material changes in the affected GitHub issues and obtain fresh
   requester approval before changed-scope execution. Then update/cancel
   obsoleted work as authorized and hand off issue/scope/approval.

## State Tracking

Active PRD state lives in team.md:
- `## PRD Source` section (path, date, hash)
- Work items tracked as issues (GitHub) or in `.squad/backlog.md` (offline mode)
- Completion percentage displayed in status checks
