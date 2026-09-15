#!/usr/bin/env bash
set -euo pipefail

error_file="$(mktemp)"
trap 'rm -f "$error_file"' EXIT

azure_env_name="$(azd env get-value AZURE_ENV_NAME 2>/dev/null || true)"

if [ "${azure_env_name:-}" != "dev" ]; then
  echo "Entra user provisioning is only enabled in the dev environment."
  exit 0
fi

if ! command -v az >/dev/null 2>&1; then
  echo "Entra user provisioning failed: Azure CLI (az) is required." >&2
  exit 1
fi

if ! command -v curl >/dev/null 2>&1; then
  echo "Entra user provisioning failed: curl is required for photo upload." >&2
  exit 1
fi

password="$(azd env get-value ENTRA_USER_INITIAL_PASSWORD 2>/dev/null || true)"
if [ -z "$password" ]; then
  echo "Entra user provisioning failed: set ENTRA_USER_INITIAL_PASSWORD in the dev azd environment." >&2
  exit 1
fi

upn_domain="$(az rest --method get \
  --url 'https://graph.microsoft.com/v1.0/domains?$select=id,isDefault,isVerified' \
  --query "value[?isDefault && isVerified].id | [0]" -o tsv 2>/dev/null || true)"
if [ -z "$upn_domain" ]; then
  echo "Entra user provisioning failed: could not detect the default verified tenant domain." >&2
  exit 1
fi

echo "upn_domain: $upn_domain"
entra_target_tenant_id="$(azd env get-value ENTRA_TARGET_TENANT_ID 2>/dev/null || true)"
entra_user_principal_name="$(azd env get-value ENTRA_USER_PRINCIPAL_NAME 2>/dev/null || true)"
echo "ENTRA_TARGET_TENANT_ID: $entra_target_tenant_id"
echo "ENTRA_USER_PRINCIPAL_NAME: $entra_user_principal_name"

create_user_if_missing() {
  local display_name="$1"
  local upn="$2"
  local mail_nickname="$3"
  local user_id

  user_id="$(az ad user show --id "$upn" --query id -o tsv 2>/dev/null || true)"
  if [ -n "$user_id" ]; then
      printf '%s already exists.\n' "$display_name" >&2
    printf '%s\n' "$user_id"
    return
  fi

  if ! user_id="$(az ad user create \
    --display-name "$display_name" \
    --user-principal-name "$upn" \
    --mail-nickname "$mail_nickname" \
    --password "$password" \
    --force-change-password-next-sign-in true \
    --query id -o tsv --only-show-errors 2>"$error_file")"; then
    echo "Entra user provisioning failed: could not create ${display_name}." >&2
    sed 's/[[:space:]]*$//' "$error_file" >&2
    return 1
  fi
  printf '%s created.\n' "$display_name" >&2
  printf '%s\n' "$user_id"
}

upload_photo() {
  local user_id="$1"
  local display_name="$2"
  local photo_path="$3"

  if [ ! -f "$photo_path" ]; then
    echo "Entra user provisioning failed: ${photo_path} for ${display_name} is missing." >&2
    return 1
  fi

  local access_token
  local http_status

  if ! access_token="$(az account get-access-token \
      --resource-type ms-graph \
      --query accessToken -o tsv 2>"$error_file")" || [ -z "$access_token" ]; then
      echo "Entra user provisioning failed: could not obtain a Microsoft Graph access token." >&2
      sed 's/[[:space:]]*$//' "$error_file" >&2
      return 1
  fi

  http_status="$(curl --silent --show-error \
      --output "$error_file" \
      --write-out '%{http_code}' \
      --request PUT \
      --header "Authorization: Bearer ${access_token}" \
      --header 'Content-Type: image/jpeg' \
      --data-binary "@${photo_path}" \
      "https://graph.microsoft.com/v1.0/users/${user_id}/photo/\$value")" || {
      echo "Entra user provisioning failed: photo upload request did not complete." >&2
      sed 's/[[:space:]]*$//' "$error_file" >&2
      return 1
  }

  if [ "$http_status" -lt 200 ] || [ "$http_status" -ge 300 ]; then
      echo "Entra user provisioning failed: could not apply ${display_name}'s photo." >&2
      sed 's/[[:space:]]*$//' "$error_file" >&2
      return 1
  fi
}

paul_id="$(create_user_if_missing 'Paul Smith' "paul.smith@${upn_domain}" 'paul.smith')"
jane_id="$(create_user_if_missing 'Jane Smith' "jane.smith@${upn_domain}" 'jane.smith')"

[ -n "$paul_id" ] || {
  echo "Entra user provisioning failed: Paul Smith has no object ID." >&2
  exit 1
}
[ -n "$jane_id" ] || {
  echo "Entra user provisioning failed: Jane Smith has no object ID." >&2
  exit 1
}

upload_photo "$paul_id" "Paul Smith" "pics/paul_smith.jpg"
upload_photo "$jane_id" "Jane Smith" "pics/jane_smith.jpg"
echo "Entra users reconciled successfully."
