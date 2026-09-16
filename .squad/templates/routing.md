# Work Routing

How to decide who handles what.

**Repository gate:** Follow `docs/issue-to-copilot-workflow.md` before execution.
New changes require discussion, all nine members' actual input, analysis, a
deduplicated qualified GitHub issue, one primary owner and explicit requester
approval. Supporters/reviewers are separate; @copilot is capability-assessed,
not consulted. Record proposed ownership in the body without execution labels
or assignees during intake: unchanged automation can auto-assign on labels.
All execution/fan-out below is post-approval only, not permission from labels.

## Routing Table

| Work Type | Route To | Examples |
|-----------|----------|----------|
| {domain 1} | {Name} | {example tasks} |
| {domain 2} | {Name} | {example tasks} |
| {domain 3} | {Name} | {example tasks} |
| Code review | {Name} | Review PRs, check quality, suggest improvements |
| Testing | {Name} | Write tests, find edge cases, verify fixes |
| Scope & priorities | {Name} | What to build next, trade-offs, decisions |
| Session logging | Scribe | Automatic — never needs routing |
| RAI review | Rai | Content safety, bias checks, credential detection, ethical review |

## Issue Routing

| Label | Action | Who |
|-------|--------|-----|
| `squad` | Check qualification/approval; record proposed owner in body until approved | Lead |
| `squad:{name}` | Verify qualified scope and requester approval before pickup | Named member |

### How Issue Assignment Works

1. For existing labeled issues, the **Lead** checks qualification and requester approval before execution labels/assignees.
2. A `squad:{member}` label names a routing candidate; pickup requires approval of qualified scope.
3. Members can reassign by removing their label and adding another member's label.
4. Do not use `squad` as an intake inbox label; it can trigger assignment automation.

## Rules

1. **Qualify first, eager only within approved scope** — consultation is not an implementation assignment.
2. **Scribe always runs** after substantial work, always as `mode: "background"`. Never blocks.
3. **Quick facts → coordinator answers directly.** Don't spawn an agent for "what port does the server run on?"
4. **When two agents could handle it**, pick the one whose domain is the primary concern.
5. **"Team, ..." → fan-out.** Spawn all relevant agents in parallel as `mode: "background"`.
6. **Anticipate downstream work.** If a feature is being built, spawn the tester to write test cases from requirements simultaneously.
7. **Issue-labeled work** — when a `squad:{member}` label is applied to an issue, route to that member. The Lead handles all `squad` (base label) triage.
