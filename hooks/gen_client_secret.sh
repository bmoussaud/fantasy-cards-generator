#!/bin/bash

set -euo pipefail

APPID_PROPERTY_NAME="${1:-}"
CLIENT_SECRET_PROPERTY_NAME="${2:-}"

if [[ -z "${APPID_PROPERTY_NAME}" || -z "${CLIENT_SECRET_PROPERTY_NAME}" ]]; then
  echo "Usage: $0 <APPID_PROPERTY_NAME> <CLIENT_SECRET_PROPERTY_NAME>" >&2
  exit 1
fi

APP_ID=$(azd env get-values 2>/dev/null | sed -n "s/^${APPID_PROPERTY_NAME}=\"\\{0,1\\}\\(.*\\)\"$/\\1/p" | tail -n 1)

if [[ -z "${APP_ID}" ]]; then
  echo "Skipping client secret generation: ${APPID_PROPERTY_NAME} is not set. App registration deployment may be disabled." >&2
  exit 0
fi

end_date=$(date -u -d '+3 months' '+%Y-%m-%dT%H:%M:%SZ')

client_secret=$(az ad app credential reset \
  --id "${APP_ID}" \
  --append \
  --display-name "gen-$(date +%Y%m%d%H%M%S)" \
  --end-date "${end_date}" \
  --query password -o tsv)

if [[ -z "${client_secret}" || "${client_secret}" == "null" ]]; then
  echo "Failed to obtain client secret from az ad app credential reset" >&2
  exit 1
fi

if ! azd env set "${CLIENT_SECRET_PROPERTY_NAME}" "${client_secret}" >/dev/null; then
  echo "Failed to store client secret in azd environment" >&2
  exit 1
fi
