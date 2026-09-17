#!/bin/bash

set -euo pipefail

APPID_PROPERTY_NAME="${1:-}"
CLIENT_SECRET_PROPERTY_NAME="${2:-}"
MANAGED_REGISTRATION_PROPERTY_NAME="${3:-}"

if [[ -z "${APPID_PROPERTY_NAME}" || -z "${CLIENT_SECRET_PROPERTY_NAME}" || -z "${MANAGED_REGISTRATION_PROPERTY_NAME}" ]]; then
  echo "Usage: $0 <APPID_PROPERTY_NAME> <CLIENT_SECRET_PROPERTY_NAME> <MANAGED_REGISTRATION_PROPERTY_NAME>" >&2
  exit 1
fi

AZD_ENV_VALUES=$(azd env get-values 2>/dev/null)

get_azd_value() {
  printf '%s\n' "${AZD_ENV_VALUES}" \
    | sed -n "s/^${1}=\"\\{0,1\\}\\(.*\\)\"$/\\1/p" \
    | tail -n 1
}

MANAGED_REGISTRATION=$(get_azd_value "${MANAGED_REGISTRATION_PROPERTY_NAME}")

case "${MANAGED_REGISTRATION}" in
  true)
    ;;
  false)
    echo "Skipping client secret generation: this deployment does not manage the Entra app registration." >&2
    exit 0
    ;;
  "")
    echo "Skipping client secret generation: ${MANAGED_REGISTRATION_PROPERTY_NAME} is not set." >&2
    exit 0
    ;;
  *)
    echo "Refusing client secret generation: ${MANAGED_REGISTRATION_PROPERTY_NAME} must be true or false, got '${MANAGED_REGISTRATION}'." >&2
    exit 1
    ;;
esac

APP_ID=$(get_azd_value "${APPID_PROPERTY_NAME}")

if [[ -z "${APP_ID}" ]]; then
  echo "Skipping client secret generation: ${APPID_PROPERTY_NAME} is not set. App registration deployment may be disabled." >&2
  exit 0
fi

EXISTING_CLIENT_SECRET=$(get_azd_value "${CLIENT_SECRET_PROPERTY_NAME}")

if [[ -n "${EXISTING_CLIENT_SECRET}" ]]; then
  echo "Skipping client secret generation: ${CLIENT_SECRET_PROPERTY_NAME} is already set." >&2
  unset EXISTING_CLIENT_SECRET
  exit 0
fi

end_date=$(date -u -d '+3 months' '+%Y-%m-%dT%H:%M:%SZ')

client_secret=$(az ad app credential reset \
  --id "${APP_ID}" \
  --append \
  --display-name "gen-$(date +%Y%m%d%H%M%S)" \
  --end-date "${end_date}" \
  --query password -o tsv) || {
  status=$?
  echo "Failed to generate client secret. If the error requires reauthentication, run:" >&2
  echo "az login --tenant 31b6a5c6-8762-4d6b-bf6e-f37931c67a75 --use-device-code" >&2
  echo "Then rerun the postprovision hook." >&2
  exit "${status}"
}

if [[ -z "${client_secret}" || "${client_secret}" == "null" ]]; then
  echo "Failed to obtain client secret from az ad app credential reset" >&2
  exit 1
fi

if ! azd env set "${CLIENT_SECRET_PROPERTY_NAME}" "${client_secret}" >/dev/null; then
  echo "Failed to store client secret in azd environment" >&2
  exit 1
fi
