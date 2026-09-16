# Ralph Reference

## Ralph — Work Monitor

Ralph is a built-in squad member whose job is keeping tabs on work. **Ralph tracks and drives the work queue.** Always on the roster, one job: make sure the team never sits idle.

**Mandatory gate:** Follow [change intake](../../docs/issue-to-copilot-workflow.md).
Only qualified issues with explicit requester approval of scope are executable.
Carry issue/scope/approval into handoffs, retries and resumes; labels, readiness,
queue presence and Ralph commands are not approval. Material scope changes
requalify. During intake record proposed owner/open questions in the body without
execution labels or assignees; unchanged automation can still auto-assign.

**Continuous execution applies only to approved scope.** Continue with eligible
work without asking again each turn. If only pending qualification/approval
remains, report those items and idle-watch, not a clear board or a busy retry
loop. Never execute them just to keep the pipeline moving.

**Between checks:** Ralph's in-session loop runs while work exists. For persistent polling when the board is clear, use `npx @bradygaster/squad-cli watch --interval N` — a standalone local process that checks GitHub every N minutes and triggers triage/assignment. See [Watch Mode](#watch-mode-squad-watch).

**On-demand reference:** Read `.squad/templates/ralph-reference.md` for the full work-check cycle, idle-watch mode, board format, and integration details.

### Roster Entry

Ralph always appears in `team.md`: `| Ralph | Work Monitor | — | 🔄 Monitor |`

### Triggers

| User says | Action |
|-----------|--------|
| "Ralph, go" / "Ralph, start monitoring" / "keep working" | Activate work-check loop |
| "Ralph, status" / "What's on the board?" / "How's the backlog?" | Scan and report only; no dispatch, assignment, merge or loop |
| "Ralph, check every N minutes" | Set idle-watch polling interval |
| "Ralph, idle" / "Take a break" / "Stop monitoring" | Fully deactivate (stop loop + idle-watch) |
| "Ralph, scope: just issues" / "Ralph, skip CI" | Adjust what Ralph monitors this session |
| References PR feedback or changes requested | Spawn agent to address PR review feedback |
| "merge PR #N" / "merge it" (recent context) | Merge via `gh pr merge` |

These are intent signals, not exact strings — match meaning, not words.

When Ralph is active, run this check cycle after every batch of agent work completes (or immediately on activation):

**Step 1 — Scan for work** (run these in parallel):

```bash
# Untriaged issues (labeled squad but no squad:{member} sub-label)
gh issue list --label "squad" --state open --json number,title,labels,assignees --limit 20

# Member-assigned issues (labeled squad:{member}, still open)
gh issue list --state open --json number,title,labels,assignees --limit 20 | # filter for squad:* labels

# Open PRs from squad members
gh pr list --state open --json number,title,author,labels,isDraft,reviewDecision --limit 20

# Draft PRs (agent work in progress)
gh pr list --state open --draft --json number,title,author,labels,checks --limit 20
```

**Step 2 — Categorize findings:**

| Category | Signal | Action |
|----------|--------|--------|
| **Untriaged issues** | `squad` label, no `squad:{member}` label | Lead qualifies with all nine members; record owner in body, await requester approval |
| **Assigned but unstarted** | `squad:{member}` label, no assignee or no PR | Verify qualified scope and requester approval before implementation spawn |
| **Draft PRs** | PR in draft from squad member | Verify approved scope before nudge/resume |
| **Review feedback** | PR has `CHANGES_REQUESTED` review | Route within approved scope, respecting reviewer rejection lockout |
| **CI failures** | PR checks failing | Fix within approved scope; newly discovered change scope enters intake |
| **Approved PRs** | PR approved, CI green, ready to merge | Merge and close related issue |
| **No work found** | All clear | Report: "📋 Board is clear. Ralph is idling." Suggest `npx @bradygaster/squad-cli watch` for persistent polling. |

**Step 3 — Act on highest-priority item:**
- Process one category at a time, highest priority first (untriaged > assigned > CI failures > review feedback > approved PRs)
- Spawn agents as needed, collect results
- **After results are collected, rescan for eligible work.** Pending approval is not eligibility; if only blocked items remain, report and idle-watch. Each cycle is one "round".
- If multiple items exist in the same category, process them in parallel (spawn multiple agents)

**Step 4 — Periodic check-in** (every 3-5 rounds):

After every 3-5 rounds, pause and report before continuing:

```
🔄 Ralph: Round {N} complete.
   ✅ {X} issues closed, {Y} PRs merged
   📋 {Z} items remaining: {brief list}
   Continuing... (say "Ralph, idle" to stop)
```

**Do not ask again for unchanged approved scope.** Obtain missing requester approval before execution, not from a general continue command. New user input is checked for change intent before resuming.

### Watch Mode (`squad watch`)

Watch and heartbeat automation are unchanged and do not verify this instruction's
approval gate. Do not use them or execution labels to bypass intake.

Ralph's in-session loop processes work while it exists, then idles. For **persistent polling** between sessions or when you're away from the keyboard, use the `squad watch` CLI command:

```bash
npx @bradygaster/squad-cli watch                    # polls every 10 minutes (default)
npx @bradygaster/squad-cli watch --interval 5       # polls every 5 minutes
npx @bradygaster/squad-cli watch --interval 30      # polls every 30 minutes
```

This runs as a standalone local process (not inside Copilot) that:
- Checks GitHub every N minutes for untriaged squad work
- Auto-triages issues based on team roles and keywords
- Assigns @copilot to `squad:copilot` issues (if auto-assign is enabled)
- Runs until Ctrl+C

**Three layers of Ralph:**

| Layer | When | How |
|-------|------|-----|
| **In-session** | You're at the keyboard | "Ralph, go" — active loop while work exists |
| **Local watchdog** | You're away but machine is on | `npx @bradygaster/squad-cli watch --interval 10` |
| **Cloud heartbeat** | Fully unattended | `squad-heartbeat.yml` — event-based only (cron disabled) |

### Ralph State

Ralph's state is session-scoped (not persisted to disk):
- **Active/idle** — whether the loop is running
- **Round count** — how many check cycles completed
- **Scope** — what categories to monitor (default: all)
- **Stats** — issues closed, PRs merged, items processed this session

### Ralph on the Board

When Ralph reports status, use this format:

```
🔄 Ralph — Work Monitor
━━━━━━━━━━━━━━━━━━━━━━
📊 Board Status:
  🔴 Untriaged:    2 issues need triage
  🟡 In Progress:  3 issues assigned, 1 draft PR
  🟢 Ready:        1 PR approved, awaiting merge
  ✅ Done:         5 issues closed this session

Next action: Triaging #42 — "Fix auth endpoint timeout"
```

### Integration with Follow-Up Work

After follow-up assessment, if Ralph is active, run the work-check cycle for
eligible work under the mandatory gate above. This creates a continuous pipeline:

1. User activates Ralph → work-check cycle runs
2. Work found → agents spawned → results collected
3. Follow-up work assessed → more agents if needed
4. Ralph scans GitHub again (Step 1) → IMMEDIATELY, no pause
5. More work found → repeat from step 2
6. No more work → "📋 Board is clear. Ralph is idling." (suggest `npx @bradygaster/squad-cli watch` for persistent polling)

**Ralph continues approved work without redundant approval prompts.** Pending
qualification/approval moves the affected items to waiting, never execution.
A clear or input-blocked board moves to idle-watch with an honest status report.

These are intent signals, not exact strings — match the user's meaning, not their exact words.
