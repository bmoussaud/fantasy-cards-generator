# Issue-to-Copilot workflow

This repository requires **discussion → whole-team consultation → analysis →
qualified GitHub issue → one primary owner → explicit requester approval →
implementation** for new features, bugs, improvements, and behavior changes.
This applies to Squad and Copilot Coding Agent, including small fixes and
changes discovered during other work.

This is an **instructions-only policy** for complying agents, not a technical
approval system. Existing automation is unchanged; see
[Automation limits](#automation-limits) before labeling or assigning an issue.

## When intake is required

Recognize change intent, not keyword occurrences. "Add card search" enters
intake; "What does the search feature do?" is a factual question. Clarify
ambiguous intent rather than assuming permission to edit.

Factual questions, status checks, and explicit git/deployment operations that
introduce no new change scope remain direct operations under their existing
safeguards. "Deploy the approved revision" is not permission to add a newly
discovered fix. Discuss and qualify that fix separately.

Continuing an existing approved issue within its approved scope does not require
duplicate intake or renewed approval every turn. Check that the approval still
applies; materially changed scope returns to qualification and fresh approval.

## Stage 1: Qualify the issue with Squad

Discuss the problem, desired outcome, affected users, and constraints with the
requester. Ask meaningful unanswered questions, using information already
provided; do not repeat answers or manufacture a questionnaire.

Run the [Change Intake ceremony](../.squad/ceremonies.md#change-intake).
Obtain actual contributions from **Gandalf, Legolas, Aragorn, Gimli, Samwise,
Scribe, Ralph, Rai, and Fact Checker**. Each provides domain impact, risks,
questions, and an owner recommendation, or explicitly records no impact.
Attribute the real input; do not simulate participation. Missing or timed-out
input stays **pending**, never consensus. Scribe's logging alone does not count
as Scribe's qualification contribution. Existing genuine contributions may be
reused for unchanged scope, with their original context; do not claim they
approve a later revision.

Assess **@copilot separately** against the capability profile in
[team.md](../.squad/team.md). It is assessed, not consulted or assigned during
intake. Cross-team architecture/governance, security-critical, ambiguous, and
multi-system work is not suitable for autonomous Copilot execution.

Search existing GitHub issues before creating one. Update a matching issue
rather than duplicating it. Record the qualification in the issue before any
implementation branch, application edit, coding assignment, or implementation
handoff. Read-only investigation and qualification records are allowed during
intake; implementation scaffolding, tests, and product documentation are not.
If GitHub or required input is unavailable, report the blocker and retain draft
qualification; do not start implementation.

### Required issue contents

| Field | Required record |
|-------|-----------------|
| Problem and outcome | Current problem, expected behavior, affected users, discussion context |
| Scope | Included work, non-goals, constraints |
| Open questions | Unresolved questions and investigation items; state none only when true |
| Bug evidence | Reproduction steps/environment/evidence, expected versus actual behavior; missing reproduction is an explicit investigation item |
| Analysis | Risks, alternatives, dependencies and trade-offs |
| Acceptance | Observable acceptance criteria, verification/tests and documentation needs |
| Team input | Each of the nine members' actual contribution or explicit no-impact; pending input identified honestly; separate @copilot capability assessment |
| Ownership | Exactly one primary owner selected by Gandalf using routing and team recommendations; supporters and reviewers listed separately |
| Readiness and approval | Draft/qualified status, approved scope/revision and real requester approval context when received |

Incomplete requests remain drafts awaiting answers, not implementation-ready
issues. A bug without reproduction can receive read-only investigation during
qualification; it does not authorize a speculative fix. If implementation-like
investigation is necessary, qualify and seek approval for that bounded scope.

Name the proposed primary owner **in the issue body**, not with an assignee or
execution-triggering label. Ownership is accountability, not permission to start.

## Stage 2: Obtain explicit requester approval

Present the qualified issue and ask the requester to approve implementation of
that scope. Approval may be in chat or a GitHub comment. Natural language is
sufficient: "I approve implementation of #147 r2", or "I approuve #147" directly
after the r2 proposal, identifies the same approved scope. No exact phrase,
command, digest, or hash is required.

Record who approved, what scope/revision, when and where, with a comment link
or the actual chat quotation and surrounding proposal context. If an agent
records chat approval on GitHub, label it as an agent-recorded report, not a
requester-authored comment. Keep discussion records privacy-safe; omit secrets
and unnecessary personal data.

Issue creation, readiness (`go:yes`), owner labels, assignees, queue presence,
team consensus, and general commands such as "Ralph, go", "retry", "resume",
or "start the backlog" **are not approval**. A request to work on an issue
counts only when its context explicitly approves the qualified scope; if that
context is unclear, obtain clarification before implementation.

## Stage 3: Execute the approved scope

Only after qualification and approval may agents create implementation
branches/worktrees, code, apply execution-routing labels/assignees, or hand off
implementation. Pass the issue, approved scope, primary owner, supporters,
reviewers, and actual approval context into every implementation handoff,
including retries and resumed sessions. Missing approval means return to intake,
even if automation assigned the issue.

Route specialist work with `squad:{member}`. For suitable Copilot work, apply
`go:yes` and `squad:copilot` only after approval, then assign through the GitHub
issue page or supported Copilot issue-assignment API if not already assigned.
Labels describe readiness/routing; they do not prove approval.

Copilot works in an isolated remote environment, implements the issue, runs
the repository checks, and opens a pull request. The change is not complete
until the pull request passes CI and receives the required review.

The normal handoff is:

```text
qualified issue
    -> explicit requester approval of scope
    -> Copilot assignment
    -> remote implementation
    -> pull request
    -> CI and review
    -> merge
```

Keep the `@copilot` concurrency limit at one in-flight issue for this
repository. Before assigning another issue, confirm that Copilot has no open
assigned issue and no unmerged pull request. This avoids overlapping changes
and merge conflicts.

## Automation limits

The existing [triage workflow](../.github/workflows/squad-triage.yml),
[issue-assignment workflow](../.github/workflows/squad-issue-assign.yml), and
[heartbeat workflow](../.github/workflows/squad-heartbeat.yml) can route or
auto-assign work from labels without checking requester approval. In particular,
`squad` can trigger triage/assignment and `squad:copilot` can trigger Copilot
assignment without `go:yes`. Do not rely on readiness, capability, concurrency,
or consent being mechanically checked by these workflows.

**During intake, do not apply `squad`, `squad:*`, `go:yes`, queue/next-up labels,
or execution assignees.** Use issue-body text for draft status, pending answers,
and proposed owner. Existing labeled issues still need approval checked by
complying agents before pickup. Ralph may report or qualify them, not treat the
queue as authorization. Status-only requests remain read-only.

Ralph retry/resume and CLI watch commands do not confer approval. Watch/runtime
automation is not made technically safe by this document. No workflow YAML,
auto-assign flag, script, validator, approval command, hash mechanism, or new
tooling is introduced here. Any future technical enforcement requires a
separately qualified and approved issue; do not mass-edit existing issues.

## Review and merge

Every Copilot pull request must be reviewed like any other contribution.
Domain specialists review specialized changes, while tests, CI, security, and
documentation remain release gates. A successful remote implementation creates
a pull request; it does not bypass reviewer rejection/lockout, CI, worktree and
branch safety, commit, merge protection, or deployment safeguards. Requester
scope approval is distinct from reviewer approval and deployment permission.

For this repository, issue #144 is not a Copilot-assignment candidate because
it combines authentication, Microsoft Graph permissions, privacy, and storage
integration. It is routed to the backend specialist with dedicated testing and
privacy review.
