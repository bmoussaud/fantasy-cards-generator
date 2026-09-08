from pydantic import ValidationError

from hosted_agents.card_orchestrator.server import create_host
from hosted_agents.card_orchestrator.settings import RuntimeSettings


def main() -> None:
    try:
        settings = RuntimeSettings.from_env()
    except (ValidationError, ValueError):
        raise SystemExit("Invalid card-orchestrator configuration.") from None
    create_host(settings).run(port=8088)


if __name__ == "__main__":
    main()
