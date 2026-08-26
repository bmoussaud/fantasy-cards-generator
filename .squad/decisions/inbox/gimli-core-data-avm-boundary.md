### 2026-08-26: AVM boundary for core data services
**By:** Gimli
**What:** Provision Cosmos DB, Storage, and the Azure AI Foundry account through AVM resource modules, but keep the Azure AI Foundry project as a native Bicep child resource because the account AVM does not cover project creation.
**Why:** This keeps us aligned with the team's AVM-first policy while still shipping the missing Foundry project binding in source control. Model deployment names, versions, and SKUs are parameterized because exact catalog/quota support must be confirmed against the live target subscription and region.
