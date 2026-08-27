from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_dockerfile_serves_fastapi_on_port_8000() -> None:
    dockerfile = (REPO_ROOT / "Dockerfile").read_text()

    assert "FROM python:3.12-slim" in dockerfile
    assert "USER appuser" in dockerfile
    assert "EXPOSE 8000" in dockerfile
    assert '"uvicorn", "app.main:app"' in dockerfile
    assert '"--host", "0.0.0.0"' in dockerfile
    assert '"--port", "8000"' in dockerfile


def test_azd_service_wires_container_app_and_acr() -> None:
    azure_yaml = (REPO_ROOT / "azure.yaml").read_text()

    assert "services:" in azure_yaml
    assert "web:" in azure_yaml
    assert "host: containerapp" in azure_yaml
    assert "registry: ${AZURE_CONTAINER_REGISTRY_ENDPOINT}" in azure_yaml
    assert "resources:" in azure_yaml
    assert "port: 8000" in azure_yaml


def test_bicep_exposes_azd_container_outputs_without_helloworld_image() -> None:
    main_bicep = (REPO_ROOT / "infra" / "main.bicep").read_text()
    container_apps_bicep = (REPO_ROOT / "infra" / "modules" / "container-apps.bicep").read_text()

    assert "mcr.microsoft.com/azuredocs/containerapps-helloworld" not in main_bicep
    assert "mcr.microsoft.com/azuredocs/containerapps-helloworld" not in container_apps_bicep
    assert "modules/container-registry.bicep" in main_bicep
    assert "output AZURE_CONTAINER_REGISTRY_ENDPOINT" in main_bicep
    assert "output AZURE_CONTAINER_APP_NAME" in main_bicep
    assert "targetPort: 8000" in container_apps_bicep
    assert "registries:" in container_apps_bicep
