from pydantic import ValidationError

from app.telemetry import configure_telemetry
from hosted_agents.card_orchestrator.settings import RuntimeSettings


def main() -> None:
    try:
        settings = RuntimeSettings.from_env()
    except (ValidationError, ValueError):
        raise SystemExit("Invalid card-orchestrator configuration.") from None
    # Foundry injects the linked project's reserved Application Insights setting.
    # Mandatory monitoring gate: the hosted agent must not start without telemetry.
    if not configure_telemetry():
        raise SystemExit(
            "Hosted agent requires telemetry; monitoring initialisation did not succeed."
        )
    # Deferred import: azure.ai.agentserver is only needed after the startup gate passes.
    from hosted_agents.card_orchestrator.server import create_host

    create_host(settings).run(port=8088)


if __name__ == "__main__":
    main()
