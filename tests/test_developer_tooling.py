"""Offline contracts for the repository's Copilot development framework."""

import json
import re
import subprocess
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_hve_plugin_uses_upstream_repository_defaults() -> None:
    settings = json.loads((ROOT / ".github/copilot/settings.json").read_text())

    assert settings == {
        "extraKnownMarketplaces": {
            "hve-core": {"source": {"source": "github", "repo": "microsoft/hve-core"}}
        },
        "enabledPlugins": {"hve-core@hve-core": True},
    }


def test_retired_framework_has_no_executable_integration() -> None:
    assert not any(path.is_file() for path in (ROOT / ".squad").rglob("*"))
    assert not any(path.is_file() for path in (ROOT / ".github").rglob("*squad*"))
    assert json.loads((ROOT / ".mcp.json").read_text()) == {"mcpServers": {}}
    assert (ROOT / ".github/workflows/pr-validation.yml").is_file()
    assert (ROOT / ".github/skills/architecture-diagram-author/SKILL.md").is_file()

    bootstrap = (ROOT / ".devcontainer/post-create.sh").read_text()
    assert "@github/copilot" in bootstrap
    assert "squad" not in bootstrap.lower()
    editor_settings = ROOT / ".vscode/settings.json"
    if editor_settings.exists():
        assert "squad" not in editor_settings.read_text().lower()

    container = json.loads((ROOT / ".devcontainer/devcontainer.json").read_text())
    extensions = container["customizations"]["vscode"]["extensions"]
    assert "ise-hve-essentials.hve-core" not in extensions


def test_hve_working_artifacts_are_ignored_and_excluded_from_web_image() -> None:
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", ".copilot-tracking/research/notes.md"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == ".copilot-tracking/research/notes.md"
    assert ".copilot-tracking" in (ROOT / ".dockerignore").read_text().splitlines()


@pytest.mark.parametrize(
    ("name", "apply_to"),
    [
        ("application", "app/**,hosted_agents/**,tests/**,pyproject.toml"),
        (
            "infrastructure",
            "infra/**,deployments/**,hooks/**,azure.yaml,deploy.sh,Dockerfile,"
            "**/Dockerfile*,.github/workflows/**",
        ),
    ],
)
def test_project_instructions_have_discoverable_path_scopes(name: str, apply_to: str) -> None:
    text = (ROOT / f".github/instructions/{name}.instructions.md").read_text()
    assert text.startswith("---\n")
    frontmatter = text.split("---", 2)[1]
    assert f'applyTo: "{apply_to}"' in frontmatter
    assert "description:" in frontmatter


@pytest.mark.parametrize(
    "relative_path",
    [
        "README.md",
        ".github/copilot-instructions.md",
        ".github/instructions/application.instructions.md",
        ".github/instructions/infrastructure.instructions.md",
        "docs/hve-core.md",
        "docs/issue-to-copilot-workflow.md",
        "docs/history/README.md",
        "docs/history/development-decisions-2026.md",
        "docs/history/development-decisions-archive-2026.md",
    ],
)
def test_migration_document_links_resolve(relative_path: str) -> None:
    document = ROOT / relative_path
    text = document.read_text(encoding="utf-8")
    for target in re.findall(r"\]\(([^)]+)\)", text):
        parsed = urlsplit(target)
        if parsed.scheme or parsed.netloc or not parsed.path:
            continue
        destination = document.parent / unquote(parsed.path)
        assert destination.exists(), f"{relative_path}: missing link target {target}"
