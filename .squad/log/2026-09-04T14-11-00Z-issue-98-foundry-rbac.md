Gimli (DevOps / Infra) — 2026-09-04T14:11:00Z

Action: Clarified the Foundry project-endpoint vs account-endpoint RBAC model in docs and opened PR #110.

What changed:
- Updated docs/architecture-agents-foundry.md with recommended project-endpoint usage for hosted agents and notes on RBAC scope implications.
- Filed follow-up implementation issue #109 for Bicep/RBAC work contingent on a hosted-agent runtime contract.

Outcome: PR #110 opened (docs), recommendation to defer infra RBAC code changes until hosted-agent runtime contract is finalized.
