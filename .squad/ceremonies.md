# Ceremonies

> Team meetings that happen before or after work. Each squad configures their own.

## Change Intake

| Field | Value |
|-------|-------|
| **Trigger** | auto |
| **When** | before |
| **Condition** | new feature, bug, improvement, or behavior-change intent; material change to approved scope |
| **Facilitator** | Gandalf |
| **Participants** | Gandalf, Legolas, Aragorn, Gimli, Samwise, Scribe, Ralph, Rai, Fact Checker |
| **Time budget** | focused |
| **Enabled** | yes |

**Agenda:**
1. Discuss problem, desired outcome, affected users and constraints; ask only meaningful unanswered questions.
2. Gather actual domain impact, risks, questions and owner recommendations from all nine members, or each member's explicit no-impact. Missing input stays pending, not consensus. Separately assess @copilot's capability; do not claim consultation or assign it.
3. Analyze scope/non-goals, alternatives, dependencies, acceptance criteria, tests and documentation. Bugs include reproduction evidence and expected versus actual behavior; missing reproduction is an explicit investigation item.
4. Search for a matching GitHub issue, then update it or create one containing the discussion, unresolved questions, analysis and attributed contributions. Incomplete qualification remains a draft.
5. Gandalf names exactly one primary owner in the issue body, with separate supporters/reviewers. Avoid execution labels and assignees: unchanged automation may start assignment.
6. Present the qualified issue for explicit requester approval in chat or a GitHub comment. Record the real approval context and scope, then carry it into implementation handoffs. Ownership/readiness/consensus/Ralph commands are not approval.

**Mandatory gate:** Follow [the shared workflow](../docs/issue-to-copilot-workflow.md).
No implementation branch, coding, assignment or handoff before qualification and
approval. A timeout, missing member input, Scribe's background logging, ceremony
cooldown or Design Review does not waive this gate. Reuse genuine prior input for
unchanged scope without claiming approval of revisions. Material changes
requalify and require fresh approval; unchanged approved execution does not repeat
intake every turn. Factual/status and explicit git/deployment operations without
new change scope remain direct, subject to existing safeguards.

---

## Design Review

| Field | Value |
|-------|-------|
| **Trigger** | auto |
| **When** | before |
| **Condition** | multi-agent task involving 2+ agents modifying shared systems |
| **Facilitator** | lead |
| **Participants** | all-relevant |
| **Time budget** | focused |
| **Enabled** | ✅ yes |

**Agenda:**
1. Review the task and requirements
2. Agree on interfaces and contracts between components
3. Identify risks and edge cases
4. Assign action items

---

## Retrospective

| Field | Value |
|-------|-------|
| **Trigger** | auto |
| **When** | after |
| **Condition** | build failure, test failure, or reviewer rejection |
| **Facilitator** | lead |
| **Participants** | all-involved |
| **Time budget** | focused |
| **Enabled** | ✅ yes |

**Agenda:**
1. What happened? (facts only)
2. Root cause analysis
3. What should change?
4. Action items for next iteration


---

## Retrospective with Enforcement

| Field | Value |
|-------|-------|
| **Trigger** | auto |
| **When** | weekly |
| **Condition** | No *retrospective* log in .squad/log/ within the last 7 days |
| **Facilitator** | lead |
| **Participants** | all |
| **Time budget** | focused |
| **Enabled** | yes |
| **Enforcement skill** | retro-enforcement |

**Agenda:**
1. What shipped this week? (closed issues, merged PRs)
2. What did not ship? (open issues, blockers)
3. Root cause on any failures
4. Action items -- each MUST become a GitHub Issue labeled retro-action

**Coordinator integration:**
At round start, call Test-RetroOverdue (see skill retro-enforcement). If overdue, run this ceremony before the work queue.

**Why GitHub Issues, not markdown:**
Production data: 0% completion across 6 retros using markdown checklists, 100% after switching to GitHub Issues.
