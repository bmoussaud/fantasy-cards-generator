# Project Context

- **Owner:** Benoit Moussaud
- **Project:** Web application running on Azure, using AI Services provided by Azure AI Foundry
- **Stack:** TBD — Azure AI Foundry (models/agents), Azure hosting (App Service/Container Apps), frontend framework TBD
- **Created:** 2026-08-26T09:07:51Z

## Learnings

<!-- Append new learnings below. Each entry is something lasting about the project. -->
📌 Team update (2026-08-26T09:17:30Z): Initial architecture decisions were proposed in .squad/decisions.md (Azure App Service hosting; Azure AI Foundry text + image model flow), pending user confirmation on open questions.
📌 Team policy (2026-08-26T10:05:50Z): Use Conventional Commits for every future commit in this repo (`<type>[optional scope]: <description>`).
📌 Work update (2026-09-01T17:19:00.962+00:00): Updated deployment-config regression test coverage for preprovision hook safety and Bicep secret-gating logic. Test suite: 17/17 passing. Coverage includes hook file structure/safety guards, azure.yaml wiring, and double-gate verification (ACA native secret + env secretRef both gated by same non-empty condition). All tests redacted; no secret values exposed in test output or fixtures.
📌 Team update (2026-09-04T13:10:31.843+00:00): Gandalf published an architecture spec at docs/architecture-agents-foundry.md proposing a Foundry-hosted card-orchestrator agent (Microsoft Agent Framework); review for your domain's implications (backend integration / infra RBAC+Bicep / UI states / test strategy).
📌 Issue #125 quality review & approval (2026-09-09T12:25:19Z): Defined quality bar and conducted initial review of issue #125. **REJECTED** on first pass: factual RBAC description error required correction. Rejection triggered Aragorn's lockout per reviewer-protocol. After Gandalf independently corrected the RBAC blocker (two assignments conditional on `enableFoundryAgentAccess`, Cognitive Services User unconditional), re-reviewed and **APPROVED**. Issue #125 now ready for implementation.
📌 PR #126 two-pass quality review (2026-09-09T14:34:30Z): **First pass:** Rejected due to factual RBAC error in issue description (Aragorn incorrectly claimed certain roles were conditional when they were unconditional). **Second pass:** After Legolas revalidated (verified telemetry path fix in commit 4983c6e, client lifecycle, fail-closed startup gate) and Gandalf corrected the RBAC conditionals, approved implementation. Final validation: 723 passed/2 skipped; five error-path scenarios verified (non-retryable 400/401/422/parse/500, retryable timeout/429/5xx); telemetry state correct on all paths; client close on exceptions; budget bounds preserved. Ruff/Black passed. PR #126 merged 2026-09-09T14:34:30Z. Demonstrated strict quality gate and lockout-recovery discipline.
