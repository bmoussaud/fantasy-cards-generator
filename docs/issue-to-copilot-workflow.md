# Issue-to-Copilot workflow

New features, bugs, improvements, and behavior changes require **discussion,
analysis, a qualified GitHub issue, one accountable owner, and explicit requester
approval before implementation**. This applies to HVE Core agents and Copilot
Coding Agent, including small fixes discovered during other work.

HVE Core supplies the Research, Plan, Implement, Review workflow; it does not
grant permission to execute a plan or deploy a change. Setup is documented in
[HVE Core development setup](hve-core.md).

## Qualify the scope

Discuss the problem, expected outcome, affected users, constraints, alternatives,
and unanswered questions. Investigate existing code and search for a matching
GitHub issue before creating one. Update a matching issue rather than duplicating
it. Missing reproduction or external evidence is an investigation item, not
permission for a speculative fix.

Use the RPI Agent to research and plan. Obtain real domain input proportionate
to the change: backend, UI/accessibility, testing, infrastructure, architecture,
security/privacy, responsible AI, and evidence verification as applicable.
Record actual contributions or explicit no-impact findings; missing input stays
pending. Do not simulate a retired named team or invent consensus.

The qualified issue records the problem, scope/non-goals, analysis/trade-offs,
dependencies, bug reproduction/evidence, open questions, measurable acceptance
criteria, test/documentation needs, one primary owner, supporters/reviewers, and
readiness/approval context. Authentication, permissions, privacy, or cross-system
architecture require appropriate specialist review, not unsupervised assignment.

Qualification is read-only except for discussion and issue/planning records.
Before approval, do not create implementation scaffolding, application edits,
coding assignments, or execution handoffs. If required information or GitHub
access is unavailable, report the blocker and keep qualification in draft.

## Obtain requester approval

Present the qualified issue and obtain explicit approval of its implementation
scope. Natural-language approval in chat or an issue comment is sufficient when
it clearly identifies that scope. Record who approved, what scope/revision,
when and where. Attribute agent-recorded chat approval as such; never present it
as a requester-authored GitHub comment.

Issue creation, labels, assignees, readiness, queue presence, team agreement,
and generic "retry"/"resume"/"start the backlog" requests are not approval.
Creating or labeling a new issue ends at triage, not immediate implementation.

## Implement and review

After approval, use HVE's implementation/review flow within the approved scope.
Carry the issue, owner, scope, reviewers, and actual approval context into every
handoff and resumed session. Already-approved unchanged work can continue without
duplicate intake; material scope changes need fresh qualification and approval.

Factual/status questions and explicitly requested git/deployment operations
that add no new change scope remain direct, subject to their own safeguards.
Deployment approval never authorizes an unrelated fix or credential change.

Keep at most **one in-flight Copilot Coding Agent issue** for this repository:
before assigning another, check for an open assigned issue or unmerged PR.
Do not open a PR, commit, push, merge, or deploy unless requested. When a PR is
requested, target `main`, use a Conventional Commits title, and reference the
issue (`Closes #N` only when closure is intended).

Every PR requires appropriate independent review and the existing
[application CI](../.github/workflows/pr-validation.yml). A rejected artifact
remains blocked until findings are addressed and independently re-reviewed;
do not self-approve or bypass branch protection. Scope approval, review approval,
merge permission, and deployment permission are separate gates.

## Automation limits

This is an instructions-only policy, not a technical authorization system.
The retired framework's triage, label synchronization, auto-assignment,
heartbeat, and branch-promotion workflows are removed. HVE plugin defaults do
not recreate those automations or migrate existing GitHub issues/labels.
Existing issue labels confer no authority. Any new execution automation requires
a separately qualified and approved change.
