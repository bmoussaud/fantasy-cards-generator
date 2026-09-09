from pydantic import ValidationError

from app.telemetry import configure_telemetry
from hosted_agents.card_orchestrator.server import create_host
from hosted_agents.card_orchestrator.settings import RuntimeSettings


def main() -> None:
    try:
        settings = RuntimeSettings.from_env()
    except (ValidationError, ValueError):
        raise SystemExit("Invalid card-orchestrator configuration.") from None
    # Initialise sanitised telemetry export before the host disables payload instrumentation.
    # Fails open when APPLICATIONINSIGHTS_CONNECTION_STRING is absent or TELEMETRY_ENABLED=false.
    configure_telemetry()
    create_host(settings).run(port=8088)


if __name__ == "__main__":
    main()
