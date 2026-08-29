### 2026-08-29: Use the supported gpt-5.5 chat-completions contract
**By:** Aragorn
**What:** Keep the deployed gpt-5.5 model on Azure OpenAI `chat/completions` with API version `2025-03-01-preview`, omit non-default `temperature`, and require every strict JSON-schema property, including `schemaVersion`. Parse only Azure's structured error code/message for bounded diagnostics and redact request content before logging.
**Why:** The live deployment advertises both chat-completion and Responses capabilities. Managed-identity probes showed `temperature: 0.2` is rejected, then strict schema was rejected because `schemaVersion` was optional; removing the parameter and making all properties required returned HTTP 200.
