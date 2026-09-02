### 2026-09-02: Healthz infra wiring uses container-scoped blob RBAC and plain timeout knobs
**By:** Gimli
**What:** Narrowed the Container App's Storage Blob Data Contributor assignment from the storage account to the `card-assets` blob container resource, and wired `HEALTHZ_COSMOS_TIMEOUT_MS` / `HEALTHZ_BLOB_TIMEOUT_MS` through azd+Bicep as plain integer settings defaulting to `1500`.
**Why:** The health probe only needs container metadata access plus the app's existing card-asset read/write/delete path within that one container, so account-wide blob scope was broader than necessary. The timeout knobs are non-secret operational limits and belong in IaC next to the existing ACA probe cadence so probe cost stays bounded and auditable.
