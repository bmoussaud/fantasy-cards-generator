# Work Routing

How to decide who handles what.

## Qualification Before Execution

Follow [mandatory change intake](../docs/issue-to-copilot-workflow.md) before
implementation routing. Every new feature, bug, improvement, or behavior change
requires discussion, real input from all nine members, analysis, a deduplicated
qualified GitHub issue, and explicit requester approval. Gandalf synthesizes
recommendations into **exactly one primary owner**, with supporters and reviewers
separate. @copilot is capability-assessed, not consulted during intake.

Record the proposed owner in the issue body. Do not apply execution labels
(`squad`, `squad:*`, `go:yes`, queue labels) or assignees during qualification:
unchanged automation can auto-assign on labels. Labels, owner, readiness, and
Ralph commands do not authorize implementation. Route unapproved issues to
qualification only; carry issue/scope/approval into implementation handoffs.
Material scope changes return to intake; unchanged approved work continues.

## Routing Table

| Work Type | Route To | Examples |
|-----------|----------|----------|
| Architecture & Azure AI Foundry design | Gandalf | Service boundaries, model/agent choice, cross-cutting design |
| Frontend / Web UI | Legolas | React components, UX, client-side API integration |
| Backend / API / AI Foundry integration | Aragorn | API endpoints, Azure AI Foundry SDK calls, business logic |
| Infra & Deployment | Gimli | Bicep/Terraform, CI/CD, Azure resource provisioning, monitoring |
| Code review | Gandalf | Review PRs, check quality, suggest improvements |
| Testing | Samwise | Write tests, find edge cases, verify fixes |
| Scope & priorities | Gandalf | What to build next, trade-offs, decisions |
| Session logging | Scribe | Automatic — never needs routing |
| RAI review | Rai | Content safety, bias checks, credential detection, ethical review |
| Fact-check / Devil's Advocate | Fact Checker | Verify claims, challenge design assumptions, pre-mortems |

## Issue Routing

| Label | Action | Who |
|-------|--------|-----|
| `squad` | Inspect qualification/approval; propose owner in body until approved | Lead |
| `squad:{name}` | Verify qualified scope and requester approval before pickup | Named member |

### How Issue Assignment Works

1. For an existing `squad` issue, the **Lead** checks qualification and approval before applying execution labels; absent either, discuss and update the draft issue.
2. A `squad:{member}` label selects a routing candidate, not permission to start. Verify the approval record first.
3. After approval, members can reassign execution routing while preserving exactly one accountable primary owner and the approved scope.
4. Do not use `squad` as an intake inbox label: it can trigger unchanged assignment automation.
5. During triage, Gandalf checks @copilot's capability profile in `team.md` without assigning it. Auto-assign is **enabled**, but complying agents may assign 🟢/🟡 matches only after requester approval; 🔴 matches route to a squad member. This instruction does not technically gate existing Actions.
6. **@copilot concurrency limit: max 1 in-flight.** Before auto-assigning a new issue to `@copilot`, Gandalf checks whether `@copilot` already has an open, unmerged PR (or an assigned issue without a merged/closed PR yet) via `gh pr list --assignee "@copilot" --state open` and `gh issue list --assignee "@copilot" --state open`. If one exists, do **not** assign the new issue yet — leave it labeled `squad` (or add a `squad:queued-copilot` label) so it's picked up on the next triage pass once the prior PR is merged/closed. This avoids parallel `@copilot` branches touching overlapping files and causing merge conflicts. To change the limit, edit the number here and re-triage.

## Rules

1. **Qualify first, eager only within approved scope** — intake consultation is not an implementation assignment.
2. **Scribe always runs** after substantial work, always as `mode: "background"`. Never blocks.
3. **Quick facts → coordinator answers directly.** Don't spawn an agent for "what port does the server run on?"
4. **When two agents could handle it**, pick the one whose domain is the primary concern.
5. **"Team, ..." → check intent.** New changes require all nine members' genuine qualification input; approved execution may fan out to relevant implementers.
6. **Anticipate downstream work.** If a feature is being built, spawn the tester to write test cases from requirements simultaneously.
7. **Issue-labeled work** — verify qualification and requester approval before pickup; otherwise route to intake. Factual/status and explicit git/deployment operations with no new change scope remain direct under existing safeguards.
