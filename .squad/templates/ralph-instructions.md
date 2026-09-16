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

Follow `docs/issue-to-copilot-workflow.md` and the installed coordinator's
Mandatory Change Intake gate. Verify the qualified issue, one primary owner,
approved scope and real requester approval context before implementation,
assignment or branches; pass them through every handoff, retry and resume.
Labels, readiness, queues and Ralph commands are not approval. Material changes
requalify; unchanged approved work does not repeat intake.

Pending qualification or approval means report and continue with other eligible
work, not code. Obtain all nine members' real input; @copilot is assessed, not
consulted. Avoid execution labels/assignees during intake and record owner/open
questions in the issue body. Status-only requests are read-only. This governs
complying sessions, not the unchanged watch/Actions eligibility or auto-assignment.

### Issue Selection

Inspect every open, unblocked, unassigned issue labeled `squad` or `squad:{member}`;
implement only when qualified and explicitly requester-approved.
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
intake record blockers in the body without execution labels. When only pending
approval/input remains, report and idle; do not claim the board is clear.
