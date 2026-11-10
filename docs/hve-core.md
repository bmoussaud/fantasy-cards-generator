# HVE Core development setup

This repository uses [Microsoft HVE Core](https://github.com/microsoft/hve-core)
with the upstream **repository default** plugin installation route. The shared
[Copilot settings](../.github/copilot/settings.json) register the
`microsoft/hve-core` marketplace and enable `hve-core@hve-core` for contributors
using the Copilot harness. This delivers the full upstream agents, skills,
instructions, and canvases without copying or maintaining framework files here.

The route follows upstream `main`; it is not a pinned production dependency.
HVE Core is explicitly experimental and rapidly evolving. Review upstream
changes and enterprise policy before adopting updates. Application dependencies
and deployment configuration are independent of this development framework.

## Start a session

1. Open this trusted repository in VS Code with GitHub Copilot, or use Copilot CLI.
   The devcontainer installs Copilot CLI; no separate HVE extension is required.
2. In VS Code, start a **Copilot** harness session, not a **Local** harness session.
   Review/accept any marketplace trust prompt yourself. Enterprise-managed
   settings may override repository defaults.
3. Reload the VS Code window after changing customizations. Confirm **RPI Agent**
   in the agent picker and `/hve-core:rpi-research` among the available skills.
   In Copilot CLI, use `/help` and check for `hve-core:rpi-agent`.
4. Follow [the project change workflow](issue-to-copilot-workflow.md).
   HVE's Research, Plan, Implement, Review flow does not replace requester
   approval, independent review, CI, or deployment authorization.

If your client does not offer the repository-default plugin, use upstream's
manual plugin installation in that client:

```bash
copilot plugin marketplace add microsoft/hve-core
copilot plugin install hve-core@hve-core
```

VS Code also discovers CLI-installed plugins. Do not install an additional HVE
extension, repository copy, or clone-discovery route alongside this plugin:
that can duplicate agents/skills. Users may disable the plugin; repository
defaults do not prove that it loaded in a particular running session.

To update a manually installed plugin:

```bash
copilot plugin marketplace update hve-core
copilot plugin update hve-core@hve-core
```

HVE's research, plans, review notes, and work-item scratch files belong in ignored
`.copilot-tracking/`. Keep durable project guidance in maintained documentation
or `.github/instructions/`, not only in scratch files.

## Project guidance and migration

Always-on guidance lives in [Copilot instructions](../.github/copilot-instructions.md),
with path-specific [application](../.github/instructions/application.instructions.md)
and [infrastructure](../.github/instructions/infrastructure.instructions.md)
instructions. The project-specific architecture diagram skill remains available.

Squad's coordinator, bundled framework skills/templates, team state, MCP state
server, editor default, devcontainer CLI installation, label/assignment/heartbeat
automation, and placeholder release workflows have been removed. The real
[application PR validation](../.github/workflows/pr-validation.yml) is retained.
No replacement auto-assignment, autonomous queue, release, or deployment
automation is introduced.

Durable project conventions were distilled into the instructions above;
[historical decisions](history/README.md) remain for provenance only.
Historical author names, branch names, issue labels, and past execution records
are not active integration. Existing untracked `.squad/` state and
`.squad-workstream` remain ignored; migration does not require deleting local
logs or notes. Retirement checks reject tracked Squad files, not preserved local
state. Legacy `.squad` Docker exclusions/tests prevent that state from entering
images. Existing GitHub labels/issues,
branch protections, and global tools used by other repositories are not changed.
If remote branch protection requires a retired workflow check, a maintainer
must update that setting separately.

## Authoritative references

- [HVE Core installation and repository defaults](https://github.com/microsoft/hve-core/blob/main/docs/getting-started/install.md)
- [HVE Core RPI methodology](https://github.com/microsoft/hve-core/tree/main/docs/rpi)
- [Squad repository installation surfaces](https://github.github.io/squad/docs/get-started/installation/)

The installation route was checked against these upstream documents on
2026-11-10. Client discovery still needs the checks above in a fresh session.
