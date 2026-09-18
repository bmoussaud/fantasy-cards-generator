#!/bin/sh
# Preserve the currently deployed web image across ordinary azd provision runs.
set -eu

resource_group="$(
  AZURE_DEV_USER_AGENT=microsoft_foundry_skill \
    azd env get-value AZURE_RESOURCE_GROUP 2>/dev/null || true
)"
container_app="$(
  AZURE_DEV_USER_AGENT=microsoft_foundry_skill \
    azd env get-value AZURE_CONTAINER_APP_NAME 2>/dev/null || true
)"

if [ -z "$resource_group" ] || [ -z "$container_app" ]; then
  exit 0
fi

if ! az account get-access-token \
  --resource https://management.azure.com/ \
  --output none >/dev/null 2>&1; then
  echo "ERROR: Unable to obtain an Azure CLI token. Run 'az login' and retry." >&2
  exit 1
fi

if ! current_image="$(
  az containerapp list \
    --resource-group "$resource_group" \
    --query "[?name=='$container_app'].properties.template.containers[0].image | [0]" \
    --output tsv 2>/dev/null
)"; then
  echo "ERROR: Failed to inspect the currently deployed web image." >&2
  exit 1
fi

if [ -z "$current_image" ]; then
  exit 0
fi

if [ "${#current_image}" -gt 512 ] || printf '%s' "$current_image" | grep -q '[[:space:]]'; then
  echo "ERROR: The deployed web image reference is invalid." >&2
  exit 1
fi

if ! AZURE_DEV_USER_AGENT=microsoft_foundry_skill azd env set \
  "CONTAINER_IMAGE=$current_image" >/dev/null; then
  echo "ERROR: Failed to preserve the currently deployed web image." >&2
  exit 1
fi

echo "Current web image preserved for infrastructure provisioning." >&2
