from __future__ import annotations

import secrets
from pathlib import Path
from urllib.parse import parse_qs
from uuid import uuid4

from authlib.integrations.base_client.errors import OAuthError
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.exceptions import HTTPException as FastAPIHTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from app.auth import (
    AUTH_NONCE_SESSION_KEY,
    AUTH_SESSION_KEY,
    AuthenticatedUser,
    build_claims_options,
    build_logout_redirect_target,
    create_oauth_client,
    ensure_auth_configured,
    extract_user_claims,
    get_authenticated_owner,
    get_session_user,
    load_auth_settings,
    require_api_user,
    require_authenticated_user,
)
from app.generation import (
    AppServices,
    ArtworkRetryBody,
    CardGenerateBody,
    CardGenerationService,
    CardResponseModel,
    client_ip_from_request,
    create_services,
)
from app.health import NotApplicableHealthProbe, build_healthz_payload, run_dependency_probes
from app.library import CardLibraryService, create_asset_url_signer
from app.problems import ProblemDetails
from app.settings import SettingsError, load_app_settings
from app.telemetry import (
    enrich_request_span,
    normalize_error_code,
    normalize_route,
    safe_log,
    valid_request_id,
)

load_dotenv()


def create_app(services: AppServices | None = None) -> FastAPI:
    auth_settings = load_auth_settings()
    app_settings = load_app_settings()
    app_services = services or create_services(app_settings)
    card_service = CardGenerationService(app_services)
    card_library_service = CardLibraryService(
        app_services.card_repository,
        asset_url_signer=create_asset_url_signer(app_settings),
    )
    templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

    app = FastAPI(title="Fantasy Cards Generator")
    app.state.services = app_services
    app.mount(
        "/static",
        StaticFiles(directory=str(Path(__file__).parent / "static")),
        name="static",
    )
    app.add_middleware(
        SessionMiddleware,
        secret_key=auth_settings.session_secret_key,
        session_cookie="fantasy_cards_session",
        same_site="lax",
        https_only=True,
    )

    @app.middleware("http")
    async def add_request_context(request: Request, call_next):
        supplied_request_id = request.headers.get("x-request-id")
        request.state.request_id = (
            supplied_request_id if valid_request_id(supplied_request_id) else uuid4().hex
        )
        try:
            response = await call_next(request)
        except Exception:
            route = normalize_route(getattr(request.scope.get("route"), "path", None))
            enrich_request_span(
                request_id=request.state.request_id,
                route=route,
                status_code=500,
            )
            safe_log(
                "request.failed",
                request_id=request.state.request_id,
                attributes={
                    "http.route": route,
                    "http.response.status_code": 500,
                    "fcg.error_code": "internal_error",
                    "fcg.outcome": "failed",
                },
            )
            raise
        route = normalize_route(getattr(request.scope.get("route"), "path", None))
        enrich_request_span(
            request_id=request.state.request_id,
            route=route,
            status_code=response.status_code,
        )
        safe_log(
            "request.completed",
            request_id=request.state.request_id,
            attributes={
                "http.route": route,
                "http.response.status_code": response.status_code,
                "fcg.outcome": "completed" if response.status_code < 400 else "failed",
                "fcg.error_code": (
                    "none"
                    if response.status_code < 400
                    else getattr(request.state, "error_code", "internal_error")
                ),
            },
        )
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    def template_context(request: Request, **context: object) -> dict[str, object]:
        csrf_token = app_services.csrf_protector.issue(request)
        return {
            "request": request,
            "user": get_session_user(request),
            "auth_configured": auth_settings.is_configured,
            "csrf_token": csrf_token,
            "generate_idempotency_key": uuid4().hex,
            **context,
        }

    @app.exception_handler(ProblemDetails)
    async def handle_problem_details(request: Request, exc: ProblemDetails):
        request.state.error_code = normalize_error_code(exc.error_code)
        if request.url.path.startswith("/ui/"):
            response = templates.TemplateResponse(
                request,
                "partials/generation_error.html",
                template_context(
                    request,
                    error_title=exc.title,
                    error_detail=exc.detail,
                    next_idempotency_key=uuid4().hex,
                ),
                status_code=exc.status_code,
            )
            for key, value in exc.headers.items():
                response.headers[key] = value
            return response
        return JSONResponse(
            exc.as_dict(request),
            status_code=exc.status_code,
            headers={"Content-Type": "application/problem+json", **exc.headers},
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(request: Request, exc: RequestValidationError):
        problem = ProblemDetails(
            status_code=422,
            title="Unprocessable Entity",
            detail="The request body or form payload was invalid.",
            type="/problems/validation-error",
            error_code="validation_error",
            extra={"errors": exc.errors()},
        )
        return await handle_problem_details(request, problem)

    @app.exception_handler(FastAPIHTTPException)
    async def handle_http_exception(request: Request, exc: FastAPIHTTPException):
        if request.url.path.startswith("/api/v1/"):
            problem = ProblemDetails(
                status_code=exc.status_code,
                title="Unauthorized" if exc.status_code == 401 else "HTTP Error",
                detail=str(exc.detail),
                type="/problems/http-exception",
                error_code="unauthorized" if exc.status_code == 401 else "http_error",
                headers=exc.headers or {},
            )
            return await handle_problem_details(request, problem)
        request.state.error_code = "unauthorized" if exc.status_code == 401 else "internal_error"
        return JSONResponse(
            {"detail": exc.detail},
            status_code=exc.status_code,
            headers=exc.headers or {},
        )

    @app.exception_handler(SettingsError)
    async def handle_settings_error(request: Request, exc: SettingsError):
        problem = ProblemDetails(
            status_code=503,
            title="Service Unavailable",
            detail=str(exc),
            type="/problems/configuration-error",
            error_code="configuration_error",
        )
        return await handle_problem_details(request, problem)

    @app.get("/", response_class=HTMLResponse)
    async def home(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "index.html",
            template_context(request, page_title="Fantasy Cards Generator"),
        )

    @app.get("/app", response_class=HTMLResponse)
    async def app_shell(
        request: Request,
        user: AuthenticatedUser = Depends(require_authenticated_user),
    ) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "app_shell.html",
            template_context(request, page_title="Generate a card", user=user),
        )

    @app.get("/my/cards", response_class=HTMLResponse)
    async def my_cards(
        request: Request,
        user: AuthenticatedUser = Depends(require_api_user),
    ) -> HTMLResponse:
        owner = get_authenticated_owner(request)
        if owner is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                headers={"WWW-Authenticate": "Session"},
                detail="Authentication required.",
            )
        cards = await card_library_service.list_cards(owner)
        return templates.TemplateResponse(
            request,
            "my_cards.html",
            template_context(
                request,
                page_title="My Cards",
                user=user,
                cards=cards,
            ),
        )

    @app.get("/my/cards/{card_id}", response_class=HTMLResponse)
    async def my_card_detail(
        card_id: str,
        request: Request,
        user: AuthenticatedUser = Depends(require_api_user),
    ) -> HTMLResponse:
        owner = get_authenticated_owner(request)
        if owner is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                headers={"WWW-Authenticate": "Session"},
                detail="Authentication required.",
            )
        card = await card_library_service.get_card(owner, card_id)
        if card is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="No card was found for this user.",
            )
        return templates.TemplateResponse(
            request,
            "my_card_detail.html",
            template_context(
                request,
                page_title=card.name,
                user=user,
                card=card,
            ),
        )

    @app.get("/auth/login")
    async def login(request: Request) -> RedirectResponse:
        if get_session_user(request) is not None:
            return RedirectResponse(url="/app", status_code=status.HTTP_303_SEE_OTHER)

        ensure_auth_configured(auth_settings)
        oauth_client = create_oauth_client(auth_settings)
        nonce = secrets.token_urlsafe(32)
        request.session[AUTH_NONCE_SESSION_KEY] = nonce

        return await oauth_client.authorize_redirect(
            request,
            auth_settings.redirect_uri,
            nonce=nonce,
        )

    @app.get("/auth/callback")
    async def auth_callback(request: Request) -> RedirectResponse:
        ensure_auth_configured(auth_settings)
        nonce = request.session.pop(AUTH_NONCE_SESSION_KEY, None)
        if not nonce:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Missing login state. Start the sign-in flow again.",
            )

        oauth_client = create_oauth_client(auth_settings)

        try:
            server_metadata = await oauth_client.load_server_metadata()
            claims_options = build_claims_options(server_metadata.get("issuer"))
            token = await oauth_client.authorize_access_token(
                request,
                claims_options=claims_options,
            )
            claims = token.get("userinfo")
            if claims is None:
                raise RuntimeError("Authentication response did not include validated userinfo.")
        except OAuthError as exc:
            request.session.pop(AUTH_SESSION_KEY, None)
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Authentication failed: {exc.error}",
            ) from exc
        except Exception as exc:
            request.session.pop(AUTH_SESSION_KEY, None)
            safe_log(
                "auth.callback_failed",
                request_id=request.state.request_id,
                attributes={
                    "fcg.outcome": "failed",
                    "fcg.error_code": normalize_error_code(type(exc).__name__),
                },
            )
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Authentication failed while validating the Entra callback.",
            ) from exc

        request.session[AUTH_SESSION_KEY] = extract_user_claims(claims)
        return RedirectResponse(url="/app", status_code=status.HTTP_303_SEE_OTHER)

    @app.get("/auth/logout")
    async def logout(request: Request) -> RedirectResponse:
        request.session.clear()
        return RedirectResponse(
            url=build_logout_redirect_target(auth_settings),
            status_code=status.HTTP_303_SEE_OTHER,
        )

    @app.get("/healthz")
    async def healthz(request: Request) -> JSONResponse:
        services: AppServices = request.app.state.services
        results = await run_dependency_probes(
            probes=[
                (
                    services.cosmos_health_probe or NotApplicableHealthProbe("cosmos"),
                    services.settings.healthz_cosmos_timeout_ms / 1000,
                ),
                (
                    services.blob_health_probe or NotApplicableHealthProbe("blob"),
                    services.settings.healthz_blob_timeout_ms / 1000,
                ),
            ],
            request_id=request.state.request_id,
        )
        payload = build_healthz_payload(results)
        return JSONResponse(
            payload,
            status_code=200 if payload["status"] == "ok" else 503,
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/partials/ping", response_class=HTMLResponse)
    async def ping_partial(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "partials/ping.html",
            template_context(request, message="HTMX is wired."),
        )

    @app.post("/api/v1/cards/generate", response_model=CardResponseModel)
    async def api_generate_card(
        request: Request,
        body: CardGenerateBody,
        _: AuthenticatedUser = Depends(require_api_user),
    ) -> CardResponseModel:
        app_services.csrf_protector.validate(request, body.csrfToken)
        owner = get_authenticated_owner(request)
        if owner is None:
            raise ProblemDetails(
                status_code=401,
                title="Unauthorized",
                detail="Authentication required.",
                type="/problems/unauthorized",
                error_code="unauthorized",
                headers={"WWW-Authenticate": "Session"},
            )
        return await card_service.generate_card(
            owner=owner,
            prompt=body.prompt,
            idempotency_key=body.idempotencyKey or uuid4().hex,
            request_id=request.state.request_id,
            client_ip=client_ip_from_request(
                request,
                trusted_proxy_hops=app_services.settings.trusted_proxy_hops,
            ),
        )

    @app.post("/api/v1/cards/{card_id}/artwork/retry", response_model=CardResponseModel)
    async def api_retry_artwork(
        card_id: str,
        request: Request,
        body: ArtworkRetryBody,
        _: AuthenticatedUser = Depends(require_api_user),
    ) -> CardResponseModel:
        app_services.csrf_protector.validate(request, body.csrfToken)
        owner = get_authenticated_owner(request)
        if owner is None:
            raise ProblemDetails(
                status_code=401,
                title="Unauthorized",
                detail="Authentication required.",
                type="/problems/unauthorized",
                error_code="unauthorized",
                headers={"WWW-Authenticate": "Session"},
            )
        return await card_service.retry_artwork(
            owner=owner,
            card_id=card_id,
            idempotency_key=body.idempotencyKey or uuid4().hex,
            request_id=request.state.request_id,
            client_ip=client_ip_from_request(
                request,
                trusted_proxy_hops=app_services.settings.trusted_proxy_hops,
            ),
        )

    @app.post("/ui/cards/generate", response_class=HTMLResponse)
    async def ui_generate_card(
        request: Request,
    ) -> HTMLResponse:
        form = await _parse_form_payload(request)
        owner = get_authenticated_owner(request)
        if owner is None:
            raise ProblemDetails(
                status_code=401,
                title="Unauthorized",
                detail="Authentication required.",
                type="/problems/unauthorized",
                error_code="unauthorized",
            )
        app_services.csrf_protector.validate(request, form.get("csrf_token"))
        raw_quality = form.get("quality", "low")
        image_quality = raw_quality if raw_quality in {"low", "medium", "high"} else "low"
        result = await card_service.generate_card(
            owner=owner,
            prompt=form.get("prompt", ""),
            idempotency_key=form.get("idempotency_key") or uuid4().hex,
            request_id=request.state.request_id,
            client_ip=client_ip_from_request(
                request,
                trusted_proxy_hops=app_services.settings.trusted_proxy_hops,
            ),
            image_quality=image_quality,
        )
        return templates.TemplateResponse(
            request,
            "partials/card_result.html",
            template_context(
                request,
                card=result,
                next_idempotency_key=uuid4().hex,
                retry_idempotency_key=uuid4().hex,
            ),
        )

    @app.post("/ui/cards/{card_id}/artwork/retry", response_class=HTMLResponse)
    async def ui_retry_artwork(
        card_id: str,
        request: Request,
    ) -> HTMLResponse:
        form = await _parse_form_payload(request)
        owner = get_authenticated_owner(request)
        if owner is None:
            raise ProblemDetails(
                status_code=401,
                title="Unauthorized",
                detail="Authentication required.",
                type="/problems/unauthorized",
                error_code="unauthorized",
            )
        app_services.csrf_protector.validate(request, form.get("csrf_token"))
        result = await card_service.retry_artwork(
            owner=owner,
            card_id=card_id,
            idempotency_key=form.get("idempotency_key") or uuid4().hex,
            request_id=request.state.request_id,
            client_ip=client_ip_from_request(
                request,
                trusted_proxy_hops=app_services.settings.trusted_proxy_hops,
            ),
        )
        return templates.TemplateResponse(
            request,
            "partials/card_result.html",
            template_context(
                request,
                card=result,
                next_idempotency_key=uuid4().hex,
                retry_idempotency_key=uuid4().hex,
            ),
        )

    @app.get("/cards/{card_id}/image")
    async def card_image(
        card_id: str,
        request: Request,
    ) -> Response:
        owner = get_authenticated_owner(request)
        if owner is None:
            raise ProblemDetails(
                status_code=401,
                title="Unauthorized",
                detail="Authentication required.",
                type="/problems/unauthorized",
                error_code="unauthorized",
            )
        payload, content_type = await card_service.fetch_image(owner, card_id)
        return StreamingResponse(iter([payload]), media_type=content_type)

    return app


async def _parse_form_payload(request: Request) -> dict[str, str]:
    body = await request.body()
    parsed = parse_qs(body.decode("utf-8"), keep_blank_values=True)
    return {key: values[-1] for key, values in parsed.items()}


app = create_app()
