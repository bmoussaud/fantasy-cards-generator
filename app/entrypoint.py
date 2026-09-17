def _load_app():
    from dotenv import load_dotenv

    load_dotenv()

    from app.settings import SettingsError, load_app_settings, load_telemetry_settings
    from app.telemetry import configure_telemetry

    app_settings = load_app_settings()
    telemetry_settings = load_telemetry_settings()
    telemetry_ready = configure_telemetry(telemetry_settings)
    if app_settings.ai_mode == "live" and not telemetry_ready:
        raise SettingsError("Mandatory application telemetry failed to initialize.")

    # Import only after telemetry can patch instrumented libraries.
    from app.main import app

    return app


app = _load_app()
