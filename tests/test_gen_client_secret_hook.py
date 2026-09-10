import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOK = REPO_ROOT / "hooks" / "gen_client_secret.sh"

MOCK_AZURE_COMMANDS = r"""
azd() {
  if [[ "$1 $2" == "env get-values" ]]; then
    printf '%s\n' "${MOCK_AZD_ENV_VALUES}"
    return 0
  fi
  if [[ "$1 $2" == "env set" ]]; then
    printf 'AZD_ENV_SET %s %s\n' "$3" "$4" >&2
    return 0
  fi
  printf 'Unexpected azd call: %s\n' "$*" >&2
  return 2
}
az() {
  printf 'AZ_CREDENTIAL_RESET %s\n' "$*" >&2
  printf '%s\n' "mock-client-secret"
}
export -f az azd
exec "$@"
"""


def _run_hook(env_values: str) -> subprocess.CompletedProcess[str]:
    env = os.environ | {"MOCK_AZD_ENV_VALUES": env_values}
    return subprocess.run(
        [
            "bash",
            "-c",
            MOCK_AZURE_COMMANDS,
            "--",
            str(HOOK),
            "ENTRA_CLIENT_ID",
            "ENTRA_CLIENT_SECRET",
            "ENTRA_APP_REGISTRATION_MANAGED",
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_azure_yaml_passes_authoritative_managed_registration_output() -> None:
    azure_yaml = (REPO_ROOT / "azure.yaml").read_text()
    assert (
        "./hooks/gen_client_secret.sh ENTRA_CLIENT_ID ENTRA_CLIENT_SECRET "
        "ENTRA_APP_REGISTRATION_MANAGED"
    ) in azure_yaml


def test_managed_registration_generates_and_stores_secret() -> None:
    result = _run_hook(
        'ENTRA_CLIENT_ID="managed-client-id"\n' 'ENTRA_APP_REGISTRATION_MANAGED="true"'
    )

    assert result.returncode == 0
    assert "AZ_CREDENTIAL_RESET ad app credential reset --id managed-client-id" in result.stderr
    assert "--append" in result.stderr
    assert "AZD_ENV_SET ENTRA_CLIENT_SECRET mock-client-secret" in result.stderr


@pytest.mark.parametrize(
    ("env_values", "message"),
    [
        (
            'ENTRA_CLIENT_ID="external-client-id"\n' 'ENTRA_APP_REGISTRATION_MANAGED="false"',
            "this deployment does not manage the Entra app registration",
        ),
        (
            'ENTRA_CLIENT_ID=""\nENTRA_APP_REGISTRATION_MANAGED="false"',
            "this deployment does not manage the Entra app registration",
        ),
        ('ENTRA_CLIENT_ID="external-client-id"', "is not set"),
    ],
)
def test_unmanaged_disabled_or_unset_ownership_never_rotates(env_values: str, message: str) -> None:
    result = _run_hook(env_values)

    assert result.returncode == 0
    assert message in result.stderr
    assert "AZ_CREDENTIAL_RESET" not in result.stderr
    assert "AZD_ENV_SET" not in result.stderr


def test_unexpected_ownership_value_fails_without_rotation() -> None:
    result = _run_hook(
        'ENTRA_CLIENT_ID="external-client-id"\n' 'ENTRA_APP_REGISTRATION_MANAGED="unexpected"'
    )

    assert result.returncode == 1
    assert "must be true or false" in result.stderr
    assert "AZ_CREDENTIAL_RESET" not in result.stderr
    assert "AZD_ENV_SET" not in result.stderr


def test_managed_registration_still_requires_client_id() -> None:
    result = _run_hook('ENTRA_CLIENT_ID=""\nENTRA_APP_REGISTRATION_MANAGED="true"')

    assert result.returncode == 0
    assert "ENTRA_CLIENT_ID is not set" in result.stderr
    assert "AZ_CREDENTIAL_RESET" not in result.stderr
    assert "AZD_ENV_SET" not in result.stderr
