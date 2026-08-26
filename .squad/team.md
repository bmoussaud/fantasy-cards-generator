# Squad Team

> fantasy-cards-generator

## Coordinator

| Name | Role | Notes |
|------|------|-------|
| Squad | Coordinator | Routes work, enforces handoffs and reviewer gates. |

## Members

| Name | Role | Charter | Status |
|------|------|---------|--------|
| Gandalf | Lead / Architect | .squad/agents/gandalf/charter.md | 🏗️ Active |
| Legolas | Frontend Dev | .squad/agents/legolas/charter.md | ⚛️ Active |
| Aragorn | Backend Dev | .squad/agents/aragorn/charter.md | 🔧 Active |
| Gimli | DevOps / Infra | .squad/agents/gimli/charter.md | ⚙️ Active |
| Samwise | Tester | .squad/agents/samwise/charter.md | 🧪 Active |
| Scribe | Scribe | .squad/agents/scribe/charter.md | 📋 Scribe |
| Ralph | Work Monitor | .squad/agents/ralph/charter.md | 🔄 Ralph |
| Rai | RAI Reviewer | .squad/agents/Rai/charter.md | 🛡️ RAI |
| Fact Checker | Fact Checker | .squad/agents/fact-checker/charter.md | 🔍 Verifier |

## Coding Agent

<!-- copilot-auto-assign: true -->

| Name | Role | Charter | Status |
|------|------|---------|--------|
| @copilot | Coding Agent | — | 🤖 Coding Agent |

### Capabilities

**🟢 Good fit — auto-route when enabled:**
- Bug fixes with clear reproduction steps
- Test coverage (adding missing tests, fixing flaky tests)
- Lint/format fixes and code style cleanup
- Dependency updates and version bumps
- Small isolated features with clear specs
- Boilerplate/scaffolding generation
- Documentation fixes and README updates

**🟡 Needs review — route to @copilot but flag for squad member PR review:**
- Medium features with clear specs and acceptance criteria
- Refactoring with existing test coverage
- API endpoint additions following established patterns
- Migration scripts with well-defined schemas

**🔴 Not suitable — route to squad member instead:**
- Architecture decisions and system design
- Multi-system integration requiring coordination
- Ambiguous requirements needing clarification
- Security-critical changes (auth, encryption, access control)
- Performance-critical paths requiring benchmarking
- Changes requiring cross-team discussion

## Project Context

- **Project:** fantasy-cards-generator — web application on Azure using Azure AI Foundry AI services
- **Owner:** Benoit Moussaud
- **Created:** 2026-08-26
- **Universe:** The Lord of the Rings

## Issue Source

**Repository:** bmoussaud/fantasy-cards-generator
**Connected:** 2026-08-26
**Platform:** GitHub
**Filters:**
- Labels: `squad`, `squad:{member}`

> ⚠️ Not yet queryable — `gh` CLI is unauthenticated in this environment. Run `gh auth login` (or set `GH_TOKEN`) to enable issue listing/triage.
