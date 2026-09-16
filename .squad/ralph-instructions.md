# Ralph Instructions
<!-- User-owned: customize this file to override Ralph's autonomous-execution behavior.
     squad init creates this file on first install; squad upgrade never overwrites it. -->

<!--
  PURPOSE
  -------
  When `.squad/ralph-instructions.md` exists, `squad watch --execute` instructs the
  spawned Copilot session to read this file and follow ALL sections here instead of
  the built-in fallback prompt.  If the file is absent, the built-in prompt is used.

  CONTRACT (stable — safe to build on)
  --------------------------------------
  YOU CAN  customize via this file:
    • Extra instructions given to Ralph at session start (Teams/Slack notifications,
      calendar checks, post-task hooks, MCP-powered side effects, escalation paths)
    • Additional eligibility rules or priority ordering for issue selection
    • Agent persona, tone, or verbosity for session output

  YOU CANNOT override via this file:
    • Parallelism — Ralph always spawns agents for all actionable issues simultaneously
    • Core eligibility filter (squad/squad:* label required, not blocked, not assigned)
    • The underlying `gh` / Copilot CLI command used to spawn each session

  TRUST IMPLICATIONS
  ------------------
  This file is read by the spawned Copilot session with full agent permissions.
  Treat it like code — never paste untrusted content here.  Anyone with write access
  to this file can influence what the agent does on your behalf.

  If this file is missing or empty, `squad watch --execute` falls back to the
  built-in prompt with no behavioral change.

  PLACEHOLDERS
  ------------
  The following values are injected by execute.ts before the session reads this file:
    (none currently — Ralph builds the issue list dynamically at runtime)

  FORMAT
  ------
  Plain markdown.  Structure with ## sections.  The spawned session reads the whole
  file, so keep it concise — one screen of instructions is ideal.
-->

## Ralph, Go!

Read this file for your full instructions.  Follow ALL sections.
MAXIMIZE PARALLELISM only for qualified, requester-approved execution in dedicated
worktrees; consultation may run in parallel without implementation edits.

### Mandatory Intake and Approval

Follow [the shared workflow](../docs/issue-to-copilot-workflow.md). Verify each
issue's qualification, exactly one primary owner and explicit requester approval
of scope before implementation, assignment, branches or handoffs. Carry the
issue/scope/real approval context through retries and resumed sessions. Queue
presence, readiness, labels, "Ralph, go", retry and resume are not approval.
Material scope changes requalify; unchanged approved scope needs no duplicate
intake. Factual/status requests are read-only; explicit git/deployment operations
without new scope retain existing safeguards.

Unapproved issues remain in qualification: discuss missing answers and obtain
real input from all nine members, with @copilot separately capability-assessed.
Missing contributions are pending, not consensus. Do not apply `squad`,
`squad:*`, `go:yes`, queue labels or execution assignees during intake; record the
owner and blockers in the issue body. This is an agent instruction, not a change
to watch/Actions eligibility or technical blocking of automatic assignment.

### Issue Selection

Inspect open, unblocked, unassigned issues labeled `squad` or `squad:{member}`.
Implement only those that pass the intake and approval gate; report pending
qualification/approval and continue with other eligible work.
Skip issues that are assigned to a human, blocked, or marked `status:on-hold`.

### Post-Task Actions

<!-- Uncomment and customize to add post-task hooks, e.g. Teams notifications:

After completing work on each issue:
- Post a brief summary to the team channel via your Teams MCP tool.
- Update the issue with a progress comment if no PR has been opened yet.
-->

### Escalation

If you are blocked on an issue, comment on it explaining why, add a `status:blocked`
label only for approved execution, and move to the next actionable item. During
intake use body text instead of labels. If only approval/input-blocked items
remain, report that state and idle rather than repeatedly requesting approval
or claiming the board is clear.
