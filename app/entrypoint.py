def _load_app():
    from dotenv import load_dotenv

    load_dotenv()

    from app.telemetry import configure_telemetry

    configure_telemetry()

    # Import only after telemetry can patch instrumented libraries.
    from app.main import app

    return app


app = _load_app()
