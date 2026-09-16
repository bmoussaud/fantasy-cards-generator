# Issue-to-Copilot workflow

This repository uses a two-stage workflow for turning feature ideas into
remotely implemented changes:

1. **Squad qualification** — refine the idea into an implementation-ready
   GitHub issue.
2. **Copilot Coding Agent execution** — assign a qualified issue to Copilot so
   it can implement the change remotely and open a pull request.

## Stage 1: Qualify the issue with Squad

Start with a feature description, then iterate on the problem, desired outcome,
scope, risks, and acceptance criteria. The issue should identify the expected
behavior, relevant constraints, validation requirements, and any required
documentation before implementation begins.

Apply these labels when the issue is ready:

- `go:yes` — requirements and acceptance criteria are ready.
- `enhancement` and `type:feature` — the issue is a new capability.
- `priority:*` — the delivery priority.
- `release:*` — the intended release target, when known.

Route the issue to the appropriate owner:

- `squad:{member}` for specialized or cross-system work.
- `squad:copilot` for isolated tasks that match the Coding Agent capability
  profile.

Do not route architecture, security-critical, ambiguous, or multi-system
integration work directly to Copilot. Assign those issues to the relevant
Squad member for design and implementation.

## Stage 2: Execute remotely with Copilot

For a qualified issue labeled `go:yes` and `squad:copilot`, assign the issue to
Copilot Coding Agent from the GitHub issue page or through the supported GitHub
Copilot issue-assignment API.

Copilot works in an isolated remote environment, implements the issue, runs
the repository checks, and opens a pull request. The change is not complete
until the pull request passes CI and receives the required review.

The normal handoff is:

```text
qualified issue
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

## Automation safeguards

An automated label-to-assignment workflow may assign an issue only when both
`go:yes` and `squad:copilot` are present. It should:

- refuse `priority:p0`, security-critical, architecture, and ambiguous issues;
- verify that Copilot has no other in-flight issue or unmerged pull request;
- preserve `squad:{member}` assignments for specialized work;
- add a needs-review notice for tasks that require Squad review;
- leave CI, review, and merge protection enabled.

Labels alone should not silently start implementation unless the repository
maintains these safeguards and the assignment token is configured securely.

## Review and merge

Every Copilot pull request must be reviewed like any other contribution.
Domain specialists review specialized changes, while tests, CI, security, and
documentation remain release gates. A successful remote implementation creates
a pull request; it does not bypass review or merge protection.

For this repository, issue #144 is not a Copilot-assignment candidate because
it combines authentication, Microsoft Graph permissions, privacy, and storage
integration. It is routed to the backend specialist with dedicated testing and
privacy review.
