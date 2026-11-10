# Project instructions

This repository uses [Microsoft HVE Core](https://github.com/microsoft/hve-core)
through the repository plugin defaults in [settings.json](copilot/settings.json).
Use the Copilot harness and the **RPI Agent** for research, planning,
implementation, and review. Setup and verification are documented in
[HVE Core setup](../docs/hve-core.md).

## Change governance

Follow the [issue-to-Copilot workflow](../docs/issue-to-copilot-workflow.md):
discuss and analyze new feature, bug, improvement, or behavior-change scope;
qualify a deduplicated GitHub issue with one accountable owner; obtain explicit
requester approval before implementation. Continue already-approved work without
repeating intake unless its scope materially changes. Labels, assignment,
automation, and a plan are not requester approval.

Record real specialist input and unresolved questions honestly; never invent
reviews or consensus. Authentication, privacy, permissions, infrastructure, and
cross-cutting architecture need appropriate independent review. Requester scope
approval does not authorize deployment, credential changes, commits, pushes,
pull requests, or merges that have not been requested.

## Engineering conventions

- Python 3.12+, `uv`, and `pyproject.toml` are the application/tooling baseline.
  Keep the FastAPI + Jinja2 + HTMX server-rendered UI; do not introduce a separate
  JavaScript application without an approved architecture change.
- Azure Container Apps is the hosting platform. Provision using Bicep,
  preferring Azure Verified Modules where suitable, and orchestrate with `azd`.
  The only deployment environments are `dev` and `prod`.
- Use feature branches targeting `main`; commit messages and PR titles follow
  Conventional Commits: `<type>[optional scope]: <description>`.
- Read the applicable path-specific instructions in `.github/instructions/`
  and existing code/tests before editing. Update tests and directly related docs
  when changing contracts; preserve unrelated user changes.
- Keep secrets out of source, prompts, logs, and working notes. Do not read
  `.env` files. Local application entry points use `python-dotenv`; deployed
  configuration comes from injected environment values and Key Vault.
- For architecture diagrams, retain the project-specific
  [architecture-diagram-author skill](skills/architecture-diagram-author/SKILL.md).
- Keep durable decisions in maintained project documentation, not ignored
  `.copilot-tracking/` working files. The [historical decision records](../docs/history/README.md)
  are provenance only, not current instructions or permission to resume old work.

## Validation

Use the smallest relevant checks first. The application PR workflow remains
[pr-validation.yml](workflows/pr-validation.yml).

```bash
uv sync --frozen --group dev --extra hosted-agent
uv run ruff check .
uv run black --check .
uv run --extra hosted-agent pytest
```

For infrastructure changes, also restore/build `infra/main.bicep`; ARM expression
changes require an explicitly authorized `azd provision --preview` because Bicep
compilation alone cannot catch ARM runtime type errors. Report static checks,
mocked tests, live checks, and unverified assumptions separately.
