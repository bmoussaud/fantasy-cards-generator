# Work Routing

How to decide who handles what.

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
| `squad` | Triage: analyze issue, assign `squad:{member}` label | Lead |
| `squad:{name}` | Pick up issue and complete the work | Named member |

### How Issue Assignment Works

1. When a GitHub issue gets the `squad` label, the **Lead** triages it — analyzing content, assigning the right `squad:{member}` label, and commenting with triage notes.
2. When a `squad:{member}` label is applied, that member picks up the issue in their next session.
3. Members can reassign by removing their label and adding another member's label.
4. The `squad` label is the "inbox" — untriaged issues waiting for Lead review.
5. During triage, Gandalf (Lead) also checks each issue against @copilot's capability profile in `team.md`. Auto-assign is **enabled** — 🟢/🟡 matches are assigned to `@copilot` automatically via `gh issue edit --add-assignee @copilot`; 🔴 matches route to a squad member instead.
6. **@copilot concurrency limit: max 1 in-flight.** Before auto-assigning a new issue to `@copilot`, Gandalf checks whether `@copilot` already has an open, unmerged PR (or an assigned issue without a merged/closed PR yet) via `gh pr list --assignee "@copilot" --state open` and `gh issue list --assignee "@copilot" --state open`. If one exists, do **not** assign the new issue yet — leave it labeled `squad` (or add a `squad:queued-copilot` label) so it's picked up on the next triage pass once the prior PR is merged/closed. This avoids parallel `@copilot` branches touching overlapping files and causing merge conflicts. To change the limit, edit the number here and re-triage.

## Rules

1. **Eager by default** — spawn all agents who could usefully start work, including anticipatory downstream work.
2. **Scribe always runs** after substantial work, always as `mode: "background"`. Never blocks.
3. **Quick facts → coordinator answers directly.** Don't spawn an agent for "what port does the server run on?"
4. **When two agents could handle it**, pick the one whose domain is the primary concern.
5. **"Team, ..." → fan-out.** Spawn all relevant agents in parallel as `mode: "background"`.
6. **Anticipate downstream work.** If a feature is being built, spawn the tester to write test cases from requirements simultaneously.
7. **Issue-labeled work** — when a `squad:{member}` label is applied to an issue, route to that member. The Lead handles all `squad` (base label) triage.
