# Copilot Coding Agent Member

On-demand reference for adding the GitHub Copilot coding agent (@copilot) to the Squad roster.

## Adding @copilot

When the user says "add copilot", "add the coding agent", or "use @copilot for issues":

1. **Add to team.md roster:**
   ```markdown
   | @copilot | Coding Agent | — | 🤖 Coding Agent |
   ```
2. **Add capability profile** (below the roster table):
   ```markdown
   <!-- copilot-auto-assign: true -->
   ### @copilot — Capability Profile

   | Capability | Level | Notes |
   |-----------|-------|-------|
   | Bug fixes (well-scoped) | 🟢 | Best for isolated, test-covered fixes |
   | Feature implementation | 🟡 | Works well with clear specs; may need review |
   | Refactoring | 🟡 | Handles mechanical refactors; verify scope |
   | Architecture decisions | 🔴 | Cannot make cross-cutting design choices |
   | Multi-repo coordination | 🔴 | Limited to single-repo context |
   | Test writing | 🟢 | Strong at adding tests for existing code |
   | Documentation | 🟢 | Generates docs from code effectively |
   ```
3. **Add routing entries** to routing.md for appropriate work types.
4. **Do not create** `charter.md` — @copilot uses `copilot-instructions.md` instead.

## Comparison: Spawned Agent vs. @copilot

| | Spawned Agent | @copilot |
|---|--------------|----------|
| Execution model | Sync sub-task within session | Async — picks up assigned issues |
| Branch convention | `squad/{issue}-{slug}` | `copilot/{slug}` |
| Trigger | Coordinator spawns directly | Issue assignment |
| Charter source | `.squad/agents/{name}/charter.md` | `.github/copilot-instructions.md` |
| Context window | Inherits full session context | Fresh context per issue |
| Reviewer gating | ✅ Enforced by coordinator | ✅ Via PR review process |
| Speed | Immediate (in-session) | Minutes (async queue) |

## Roster Format

In `team.md`, @copilot always appears as:

```markdown
| @copilot | Coding Agent | — | 🤖 Coding Agent |
```

- **No casting** — always "@copilot" (literal handle).
- **No charter file** — configuration lives in `.github/copilot-instructions.md`.
- **No history file** — work is tracked via PRs and issue comments.

## Auto-Assign Behavior

Follow [mandatory intake](../../docs/issue-to-copilot-workflow.md) first. During
qualification @copilot is capability-assessed, not consulted or assigned. Record
the proposed owner in the issue body without execution labels. Existing Actions
can auto-assign independently; this instruction does not technically gate them.

Controlled by the HTML comment in team.md:

```markdown
<!-- copilot-auto-assign: true -->
```

| Setting | Behavior |
|---------|----------|
| `true` | After qualified-scope requester approval and concurrency checks, Lead may assign suitable routed issues to @copilot |
| `false` | Lead presents recommendation; qualified-scope requester approval is still required before assignment |

## Lead Triage Integration

During triage, Lead evaluates each issue against @copilot's capability profile:

1. **🟢 Match** — Record recommendation; assign only after qualification and explicit requester approval.
2. **🟡 Match** — Same approval gate, with note: "⚠️ May need review — @copilot is 🟡 for this type of work."
3. **🔴 Match** — Skip @copilot; route to appropriate spawned agent or human.

## Routing Details

Add to `routing.md`:

```markdown
| bug fixes (isolated, test-covered) | @copilot 🤖 | Single-file fixes, test additions |
| documentation updates | @copilot 🤖 | README, API docs, inline comments |
| test coverage gaps | @copilot 🤖 | Adding missing test cases |
```

Work that routes to @copilot:
- Uses an existing qualified GitHub issue explicitly approved by the requester; no create-and-immediately-assign shortcut
- Passes issue, bounded scope and actual chat/comment approval context; readiness/labels/assignment alone are not approval
- Does NOT spawn a sub-agent — @copilot works asynchronously
- Coordinator reports: "🤖 Assigned #{number} to @copilot — will open a PR when ready."
- Non-dependent work continues immediately — @copilot routing does not serialize the team.

## Monitoring @copilot Work

On each watch cycle (or when user asks "status"):
- Check for open PRs from `copilot/*` branches.
- Report: "🤖 @copilot: {N} PRs open ({list}). {M} issues assigned, pending."
