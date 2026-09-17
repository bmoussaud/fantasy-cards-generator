from __future__ import annotations

import secrets
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode
from uuid import uuid4

from authlib.integrations.base_client.errors import OAuthError
from dotenv import load_dotenv
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.exceptions import HTTPException as FastAPIHTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from starlette.datastructures import UploadFile
from starlette.middleware.sessions import SessionMiddleware

from app.auth import (
    AUTH_NONCE_SESSION_KEY,
    AUTH_PROFILE_IMPORT_SESSION_KEY,
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
from app.deletion import DeletionService
from app.entra_profile import GRAPH_PROFILE_PHOTO_SCOPE, fetch_profile_photo
from app.generation import (
    AppServices,
    ArtworkRetryBody,
    AuthenticatedOwner,
    CardGenerateBody,
    CardGenerationService,
    CardResponseModel,
    ReferenceImageUpload,
    client_ip_from_request,
    create_services,
)
from app.health import NotApplicableHealthProbe, build_healthz_payload, run_dependency_probes
from app.library import CardLibraryService
from app.photos import (
    PROFILE_PHOTO_IMPORT_SOURCE,
    SavedPhotoListResponseModel,
    SavedPhotoResponseModel,
    SavedPhotoService,
)
from app.problems import ProblemDetails
from app.settings import SettingsError, load_app_settings
from app.telemetry import (
    configure_auth_access_logging,
    enrich_request_span,
    normalize_error_code,
    normalize_route,
    safe_log,
    valid_request_id,
)

load_dotenv()

ALLOWED_PHOTO_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp"}
MAX_REFERENCE_PHOTO_BYTES = 5 * 1024 * 1024
PROFILE_PHOTO_IMPORT_MESSAGES = {
    "imported": "Your Microsoft profile photo was imported into My Photos.",
    "already_imported": "Your Microsoft profile photo is already in My Photos.",
    "no_photo": (
        "Microsoft did not return a profile photo for this account. "
        "You can upload a photo to My Photos instead."
    ),
    "failed": (
        "Your Microsoft profile photo could not be imported. You can upload a photo "
        "instead, or choose photo import the next time you sign in."
    ),
    "consent_denied": (
        "Microsoft photo access was not granted. No photo was imported. "
        "Sign in without photo import, or try again and allow access."
    ),
    "invalid_state": (
        "The photo import session is invalid or expired. Please start sign-in again."
    ),
    "unavailable": (
        "Photo import is not available in the current state. "
        "Check My Photos or refresh the page before trying again."
    ),
}


@dataclass(slots=True)
class ParsedCardGenerateRequest:
    prompt: str
    idempotency_key: str | None
    csrf_token: str | None
    image_quality: str | None = None
    reference_image: ReferenceImageUpload | None = None
    saved_photo_id: str | None = None
    save_photo: bool = False
    photo_label: str | None = None


def create_app(services: AppServices | None = None) -> FastAPI:
    configure_auth_access_logging()
    auth_settings = load_auth_settings()
    app_settings = load_app_settings()
    app_services = services or create_services(app_settings)
    card_service = CardGenerationService(app_services)
    card_library_service = CardLibraryService(app_services.card_repository)
    deletion_service = DeletionService(
        settings=app_services.settings,
        card_repository=app_services.card_repository,
        audit_repository=app_services.audit_repository,
        deletion_audit_repository=app_services.deletion_audit_repository,
        asset_store=app_services.asset_store,
        saved_photo_repository=app_services.saved_photo_repository,
        profile_photo_import_state_repository=app_services.profile_photo_import_state_repository,
        photo_asset_store=app_services.photo_asset_store,
    )
    photo_service = SavedPhotoService(
        settings=app_services.settings,
        repository=app_services.saved_photo_repository,
        asset_store=app_services.photo_asset_store,
        moderation_service=app_services.photo_moderation_service,
    )
    profile_import_repository = app_services.profile_photo_import_state_repository
    templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

    def profile_import_redirect(
        request: Request,
        result: str,
        *,
        stage: str,
        error_code: str = "none",
        http_status: int | None = None,
    ) -> RedirectResponse:
        request.session["profile_photo_import_notice"] = {
            "message": PROFILE_PHOTO_IMPORT_MESSAGES[result],
            "is_error": result not in {"imported", "already_imported", "no_photo"},
            "request_id": request.state.request_id,
        }
        safe_log(
            "auth.profile_photo_import_result",
            request_id=request.state.request_id,
            attributes={
                "fcg.stage": stage,
                "fcg.outcome": result,
                "fcg.error_code": error_code,
                "http.response.status_code": http_status,
            },
        )
        return RedirectResponse(
            url="/app" if get_session_user(request) is not None else "/auth/login",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        del app
        try:
            probe = app_services.agent_health_probe
            if probe is None:
                raise SettingsError("Foundry agent readiness probe is not configured.")
            result = await probe.check(app_services.settings.foundry_agent_timeout_seconds)
            if result.status != "ok":
                safe_log(
                    "dependency.failed",
                    attributes={
                        "fcg.dependency": "foundry_agent",
                        "fcg.outcome": "failed",
                        "fcg.error_code": result.error_category,
                        "fcg.duration_ms": result.duration_ms,
                    },
                )
                raise SettingsError(
                    f"Foundry agent startup validation failed: {result.error_category}."
                )
            yield
        finally:
            if app_services.agent_client is not None and hasattr(
                app_services.agent_client, "aclose"
            ):
                await app_services.agent_client.aclose()

    app = FastAPI(title="Fantasy Cards Generator", lifespan=lifespan)
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
                    error_code=request.state.error_code,
                    next_idempotency_key=uuid4().hex,
                ),
                status_code=exc.status_code,
            )
            for key, value in exc.headers.items():
                response.headers[key] = value
            response.headers["X-Generation-Error"] = request.state.error_code
            return response
        return JSONResponse(
            exc.as_dict(request),
            status_code=exc.status_code,
            headers={"Content-Type": "application/problem+json", **exc.headers},
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(request: Request, exc: RequestValidationError):
        detail = "The request body or form payload was invalid."
        if request.url.path == "/ui/cards/generate" and any(
            error.get("loc") in (("prompt",), ("body", "prompt")) for error in exc.errors()
        ):
            detail = "Use a card prompt of 12 to 400 characters."
        problem = ProblemDetails(
            status_code=422,
            title="Unprocessable Entity",
            detail=detail,
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
            template_context(
                request,
                page_title="Generate a card",
                user=user,
                profile_photo_import_notice=request.session.pop(
                    "profile_photo_import_notice", None
                ),
            ),
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
                deleted_count=_query_count(request, "deleted"),
                failed_count=_query_count(request, "failed"),
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

    @app.get("/my/photos/library", response_class=HTMLResponse)
    async def my_photo_library(
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
        return templates.TemplateResponse(
            request,
            "my_photos.html",
            template_context(
                request,
                page_title="My Photos",
                user=user,
                saved_photo_max_bytes=app_services.settings.saved_photo_max_bytes,
            ),
        )

    @app.get("/my/account", response_class=HTMLResponse)
    async def my_account(
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
        return templates.TemplateResponse(
            request,
            "my_account.html",
            template_context(
                request,
                page_title="My Account",
                user=user,
                owner=owner,
                audit_retention_days=app_services.settings.audit_retention_days,
            ),
        )

    @app.post("/my/photos", response_model=SavedPhotoResponseModel, status_code=201)
    async def save_my_photo(
        request: Request,
        _: AuthenticatedUser = Depends(require_api_user),
    ) -> SavedPhotoResponseModel:
        form = await request.form()
        app_services.csrf_protector.validate(
            request,
            _form_string(form, "csrf_token", "csrfToken"),
        )
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
        reference_image = await _parse_reference_image(form.get("photo"))
        if reference_image is None:
            raise ProblemDetails(
                status_code=422,
                title="Invalid Photo Upload",
                detail="A photo file is required.",
                type="/problems/invalid-photo-upload",
                error_code="invalid_photo_upload",
            )
        return await photo_service.save_photo(
            owner=owner,
            photo=reference_image,
            label=_form_string(form, "label"),
        )

    @app.get("/my/photos", response_model=SavedPhotoListResponseModel)
    async def list_my_photos(
        request: Request,
        _: AuthenticatedUser = Depends(require_api_user),
    ) -> SavedPhotoListResponseModel:
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
        return await photo_service.list_photos(owner)

    @app.get("/my/photos/{photo_id}/image")
    async def my_photo_image(
        photo_id: str,
        request: Request,
        _: AuthenticatedUser = Depends(require_api_user),
    ) -> Response:
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
        payload, content_type = await photo_service.fetch_original(owner, photo_id)
        return StreamingResponse(iter([payload]), media_type=content_type)

    @app.get("/my/photos/{photo_id}/thumbnail")
    async def my_photo_thumbnail(
        photo_id: str,
        request: Request,
        _: AuthenticatedUser = Depends(require_api_user),
    ) -> Response:
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
        payload, content_type = await photo_service.fetch_thumbnail(owner, photo_id)
        return StreamingResponse(iter([payload]), media_type=content_type)

    @app.delete("/my/photos/{photo_id}", status_code=204)
    async def delete_my_photo(
        photo_id: str,
        request: Request,
        _: AuthenticatedUser = Depends(require_api_user),
    ) -> Response:
        app_services.csrf_protector.validate(
            request,
            request.headers.get("x-csrf-token") or request.query_params.get("csrfToken"),
        )
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
        await photo_service.delete_photo(
            owner,
            photo_id,
            import_state_repository=profile_import_repository,
        )
        return Response(status_code=204)

    @app.post("/my/cards/{card_id}/delete", status_code=303)
    async def delete_my_card(
        card_id: str,
        request: Request,
        background_tasks: BackgroundTasks,
        _: AuthenticatedUser = Depends(require_api_user),
    ) -> Response:
        form = await request.form()
        app_services.csrf_protector.validate(
            request,
            _form_string(form, "csrf_token", "csrfToken"),
        )
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
        await deletion_service.delete_card(
            owner=owner,
            card_id=card_id,
            request_id=request.state.request_id,
            schedule_cleanup=background_tasks.add_task,
        )
        return RedirectResponse(
            url="/my/cards", status_code=status.HTTP_303_SEE_OTHER, background=background_tasks
        )

    @app.post("/my/cards/batch-delete", status_code=303)
    async def batch_delete_my_cards(
        request: Request,
        background_tasks: BackgroundTasks,
        _: AuthenticatedUser = Depends(require_api_user),
    ) -> Response:
        form = await request.form()
        app_services.csrf_protector.validate(
            request,
            _form_string(form, "csrf_token", "csrfToken"),
        )
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
        card_ids = [str(value).strip() for value in form.getlist("card_ids") if str(value).strip()]
        if not card_ids:
            return RedirectResponse(url="/my/cards", status_code=status.HTTP_303_SEE_OTHER)
        result = await deletion_service.delete_cards(
            owner=owner,
            card_ids=card_ids,
            request_id=request.state.request_id,
            schedule_cleanup=background_tasks.add_task,
        )
        query = urlencode({"deleted": result.deleted_count, "failed": result.failed_count})
        return RedirectResponse(
            url=f"/my/cards?{query}",
            status_code=status.HTTP_303_SEE_OTHER,
            background=background_tasks,
        )

    @app.post("/my/account/delete", status_code=303)
    async def delete_my_account(
        request: Request,
        background_tasks: BackgroundTasks,
        _: AuthenticatedUser = Depends(require_api_user),
    ) -> Response:
        form = await request.form()
        app_services.csrf_protector.validate(
            request,
            _form_string(form, "csrf_token", "csrfToken"),
        )
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
        await deletion_service.delete_account(
            owner=owner,
            request_id=request.state.request_id,
            schedule_cleanup=background_tasks.add_task,
        )
        request.session.clear()
        return RedirectResponse(
            url="/", status_code=status.HTTP_303_SEE_OTHER, background=background_tasks
        )

    @app.get("/auth/login")
    async def login_page(request: Request) -> Response:
        if get_session_user(request) is not None:
            return RedirectResponse(url="/app", status_code=status.HTTP_303_SEE_OTHER)

        ensure_auth_configured(auth_settings)
        return templates.TemplateResponse(
            request,
            "login.html",
            template_context(
                request,
                page_title="Sign in",
                profile_photo_import_notice=request.session.pop(
                    "profile_photo_import_notice", None
                ),
            ),
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/auth/login")
    async def login(request: Request) -> RedirectResponse:
        form = await request.form()
        app_services.csrf_protector.validate(request, form.get("csrf_token"))
        if get_session_user(request) is not None:
            return RedirectResponse(url="/app", status_code=status.HTTP_303_SEE_OTHER)
        ensure_auth_configured(auth_settings)
        import_requested = form.get("import_profile_photo") == "true"
        request.session.pop(AUTH_PROFILE_IMPORT_SESSION_KEY, None)
        nonce = secrets.token_urlsafe(32)
        request.session[AUTH_NONCE_SESSION_KEY] = nonce
        if import_requested:
            state_value = secrets.token_urlsafe(32)
            request.session[AUTH_PROFILE_IMPORT_SESSION_KEY] = {"state": state_value}
            scopes = tuple(dict.fromkeys((*auth_settings.scopes, GRAPH_PROFILE_PHOTO_SCOPE)))
            oauth_client = create_oauth_client(auth_settings, scopes=scopes)
            return await oauth_client.authorize_redirect(
                request, auth_settings.redirect_uri, nonce=nonce, state=state_value
            )
        oauth_client = create_oauth_client(auth_settings)
        return await oauth_client.authorize_redirect(
            request,
            auth_settings.redirect_uri,
            nonce=nonce,
        )

    @app.get("/auth/callback")
    async def auth_callback(request: Request) -> RedirectResponse:
        ensure_auth_configured(auth_settings)
        import_context = request.session.pop(AUTH_PROFILE_IMPORT_SESSION_KEY, None)
        nonce = request.session.pop(AUTH_NONCE_SESSION_KEY, None)
        if not nonce:
            if isinstance(import_context, dict):
                return profile_import_redirect(
                    request, "invalid_state", stage="oauth_state", error_code="invalid_oauth_state"
                )
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Missing login state. Start the sign-in flow again.",
            )

        import_flow = isinstance(import_context, dict)
        if import_flow:
            state_value = import_context.get("state")
            if not isinstance(state_value, str) or request.query_params.get("state") != state_value:
                return profile_import_redirect(
                    request, "invalid_state", stage="oauth_state", error_code="invalid_oauth_state"
                )
            oauth_client = create_oauth_client(
                auth_settings,
                scopes=tuple(dict.fromkeys((*auth_settings.scopes, GRAPH_PROFILE_PHOTO_SCOPE))),
            )
        else:
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
            if import_flow:
                return profile_import_redirect(
                    request,
                    "consent_denied" if exc.error == "access_denied" else "failed",
                    stage="oauth_token",
                    error_code="oauth_error",
                )
            request.session.pop(AUTH_SESSION_KEY, None)
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Authentication failed: {exc.error}",
            ) from exc
        except Exception as exc:
            if import_flow:
                return profile_import_redirect(
                    request, "failed", stage="oauth_token", error_code="oauth_error"
                )
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

        authenticated_user = extract_user_claims(claims)
        request.session[AUTH_SESSION_KEY] = authenticated_user
        if import_flow:
            owner_id = authenticated_user["owner_id"]
            import_stage = "import_claim"
            import_result = "failed"
            import_error_code = "none"
            import_http_status = None
            try:
                if not await profile_import_repository.claim(owner_id):
                    existing = await profile_import_repository.get(owner_id)
                    if existing is not None and existing.status == "imported":
                        return profile_import_redirect(
                            request, "already_imported", stage=import_stage
                        )
                    return profile_import_redirect(
                        request, "unavailable", stage=import_stage, error_code="unavailable"
                    )
                import_stage = "profile_photo_fetch"
                access_token = token.get("access_token")
                photo = await fetch_profile_photo(
                    str(access_token) if isinstance(access_token, str) else ""
                )
                if photo is None:
                    import_stage = "import_complete"
                    if not await profile_import_repository.complete_no_photo(owner_id):
                        raise RuntimeError("Profile photo import state changed during import.")
                    import_result = "no_photo"
                else:
                    import_stage = "photo_save"
                    stored = await photo_service.save_photo(
                        owner=AuthenticatedOwner(
                            owner_id=owner_id,
                            tenant_id=authenticated_user["tenant_id"],
                            object_id=authenticated_user["object_id"],
                            subject=authenticated_user["sub"],
                            display_name=authenticated_user["name"],
                            email=authenticated_user["email"],
                        ),
                        photo=photo,
                        label="Microsoft profile photo",
                        source=PROFILE_PHOTO_IMPORT_SOURCE,
                        source_key="me/photo",
                    )
                    import_stage = "import_complete"
                    completed = await profile_import_repository.complete_import(
                        owner_id,
                        stored.photoId,
                    )
                    if not completed:
                        await photo_service.delete_photo(
                            AuthenticatedOwner(
                                owner_id=owner_id,
                                tenant_id=authenticated_user["tenant_id"],
                                object_id=authenticated_user["object_id"],
                                subject=authenticated_user["sub"],
                                display_name=authenticated_user["name"],
                                email=authenticated_user["email"],
                            ),
                            stored.photoId,
                        )
                        import_result = "unavailable"
                        import_error_code = "unavailable"
                    else:
                        import_result = "imported"
            except Exception as exc:
                import_error_code = normalize_error_code(
                    getattr(exc, "error_code", type(exc).__name__)
                )
                import_http_status = getattr(exc, "status_code", None)
                try:
                    await profile_import_repository.reset_claim(owner_id)
                except Exception as reset_exc:
                    safe_log(
                        "auth.profile_photo_import_reset_failed",
                        request_id=request.state.request_id,
                        attributes={
                            "fcg.error_code": normalize_error_code(type(reset_exc).__name__),
                            "fcg.outcome": "failed",
                        },
                    )
                safe_log(
                    "auth.profile_photo_import_failed",
                    request_id=request.state.request_id,
                    attributes={
                        "fcg.error_code": normalize_error_code(type(exc).__name__),
                        "fcg.outcome": "failed",
                    },
                )
            return profile_import_redirect(
                request,
                import_result,
                stage=import_stage,
                error_code=import_error_code,
                http_status=import_http_status,
            )
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
                (
                    services.agent_health_probe or NotApplicableHealthProbe("agent"),
                    services.settings.foundry_agent_timeout_seconds,
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

    @app.get("/livez")
    async def livez() -> JSONResponse:
        return JSONResponse({"status": "ok"}, headers={"Cache-Control": "no-store"})

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
        _: AuthenticatedUser = Depends(require_api_user),
    ) -> CardResponseModel:
        payload = await _parse_card_generate_request(request)
        app_services.csrf_protector.validate(request, payload.csrf_token)
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
        if payload.save_photo:
            if payload.reference_image is None:
                raise ProblemDetails(
                    status_code=422,
                    title="Saved Photo Requires Upload",
                    detail="save_photo=true requires a fresh photo upload.",
                    type="/problems/saved-photo-requires-upload",
                    error_code="saved_photo_requires_upload",
                )
            await photo_service.save_photo(
                owner=owner,
                photo=payload.reference_image,
                label=payload.photo_label,
            )
        if payload.saved_photo_id is not None:
            payload.reference_image = await photo_service.load_reference_image(
                owner,
                payload.saved_photo_id,
            )
        return await card_service.generate_card(
            owner=owner,
            prompt=payload.prompt,
            idempotency_key=payload.idempotency_key or uuid4().hex,
            request_id=request.state.request_id,
            client_ip=client_ip_from_request(
                request,
                trusted_proxy_hops=app_services.settings.trusted_proxy_hops,
            ),
            reference_image=payload.reference_image,
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

    @app.delete("/api/v1/cards/{card_id}", status_code=204)
    async def api_delete_card(
        card_id: str,
        request: Request,
        background_tasks: BackgroundTasks,
        _: AuthenticatedUser = Depends(require_api_user),
    ) -> Response:
        app_services.csrf_protector.validate(
            request,
            request.headers.get("x-csrf-token") or request.query_params.get("csrfToken"),
        )
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
        await deletion_service.delete_card(
            owner=owner,
            card_id=card_id,
            request_id=request.state.request_id,
            schedule_cleanup=background_tasks.add_task,
        )
        return Response(status_code=204, background=background_tasks)

    @app.delete("/api/v1/account", status_code=204)
    async def api_delete_account(
        request: Request,
        background_tasks: BackgroundTasks,
        _: AuthenticatedUser = Depends(require_api_user),
    ) -> Response:
        app_services.csrf_protector.validate(
            request,
            request.headers.get("x-csrf-token") or request.query_params.get("csrfToken"),
        )
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
        await deletion_service.delete_account(
            owner=owner,
            request_id=request.state.request_id,
            schedule_cleanup=background_tasks.add_task,
        )
        request.session.clear()
        return Response(status_code=204, background=background_tasks)

    @app.post("/ui/cards/generate", response_class=HTMLResponse)
    async def ui_generate_card(
        request: Request,
    ) -> HTMLResponse:
        payload = await _parse_card_generate_request(request)
        owner = get_authenticated_owner(request)
        if owner is None:
            raise ProblemDetails(
                status_code=401,
                title="Unauthorized",
                detail="Authentication required.",
                type="/problems/unauthorized",
                error_code="unauthorized",
            )
        app_services.csrf_protector.validate(request, payload.csrf_token)
        if payload.save_photo:
            if payload.reference_image is None:
                raise ProblemDetails(
                    status_code=422,
                    title="Saved Photo Requires Upload",
                    detail="save_photo=true requires a fresh photo upload.",
                    type="/problems/saved-photo-requires-upload",
                    error_code="saved_photo_requires_upload",
                )
            await photo_service.save_photo(
                owner=owner,
                photo=payload.reference_image,
                label=payload.photo_label,
            )
        if payload.saved_photo_id is not None:
            payload.reference_image = await photo_service.load_reference_image(
                owner,
                payload.saved_photo_id,
            )
        raw_quality = payload.image_quality or "low"
        image_quality = raw_quality if raw_quality in {"low", "medium", "high"} else "low"
        result = await card_service.generate_card(
            owner=owner,
            prompt=payload.prompt,
            idempotency_key=payload.idempotency_key or uuid4().hex,
            request_id=request.state.request_id,
            client_ip=client_ip_from_request(
                request,
                trusted_proxy_hops=app_services.settings.trusted_proxy_hops,
            ),
            image_quality=image_quality,
            reference_image=payload.reference_image,
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


async def _parse_card_generate_request(request: Request) -> ParsedCardGenerateRequest:
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type == "application/json":
        body = _validate_card_generate_body(await request.json())
        return ParsedCardGenerateRequest(
            prompt=body.prompt,
            idempotency_key=body.idempotencyKey,
            csrf_token=body.csrfToken,
            saved_photo_id=body.savedPhotoId,
        )

    form = await request.form()
    body = _validate_card_generate_body(
        {
            "prompt": _form_string(form, "prompt"),
            "idempotencyKey": _form_string(form, "idempotency_key", "idempotencyKey"),
            "csrfToken": _form_string(form, "csrf_token", "csrfToken"),
            "savedPhotoId": _form_string(form, "saved_photo_id", "savedPhotoId"),
        }
    )
    parsed = ParsedCardGenerateRequest(
        prompt=body.prompt,
        idempotency_key=body.idempotencyKey,
        csrf_token=body.csrfToken,
        image_quality=_form_string(form, "quality"),
        reference_image=await _parse_reference_image(form.get("photo")),
        saved_photo_id=body.savedPhotoId,
        save_photo=_form_bool(form, "save_photo", "savePhoto"),
        photo_label=_form_string(form, "photo_label", "photoLabel"),
    )
    if parsed.reference_image is not None and parsed.saved_photo_id is not None:
        raise ProblemDetails(
            status_code=422,
            title="Conflicting Photo Inputs",
            detail="Provide either photo or saved_photo_id, but not both.",
            type="/problems/photo-reference-conflict",
            error_code="photo_reference_conflict",
        )
    if parsed.save_photo and parsed.saved_photo_id is not None:
        raise ProblemDetails(
            status_code=422,
            title="Invalid Saved Photo Request",
            detail="save_photo=true can only be used with a fresh photo upload.",
            type="/problems/saved-photo-requires-upload",
            error_code="saved_photo_requires_upload",
        )
    return parsed


def _validate_card_generate_body(payload: object) -> CardGenerateBody:
    try:
        return CardGenerateBody.model_validate(payload)
    except ValidationError as exc:
        raise RequestValidationError(exc.errors()) from exc


async def _parse_form_payload(request: Request) -> dict[str, str]:
    form = await request.form()
    return {
        key: value for key, value in form.items() if isinstance(key, str) and isinstance(value, str)
    }


def _query_count(request: Request, key: str) -> int:
    raw_value = request.query_params.get(key)
    if raw_value is None:
        return 0
    try:
        value = int(raw_value)
    except ValueError:
        return 0
    return value if value > 0 else 0


def _form_string(form, *keys: str) -> str | None:
    for key in keys:
        value = form.get(key)
        if isinstance(value, str):
            normalized = value.strip()
            if normalized:
                return normalized
    return None


def _form_bool(form, *keys: str) -> bool:
    value = _form_string(form, *keys)
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


async def _parse_reference_image(value: object) -> ReferenceImageUpload | None:
    if value is None:
        return None
    if not isinstance(value, UploadFile):
        raise ProblemDetails(
            status_code=422,
            title="Invalid Photo Upload",
            detail="The photo field must be uploaded as a file.",
            type="/problems/invalid-photo-upload",
            error_code="invalid_photo_upload",
        )
    filename = value.filename or None
    content_type = (value.content_type or "").lower()
    if not filename:
        return None
    if content_type not in ALLOWED_PHOTO_CONTENT_TYPES:
        raise ProblemDetails(
            status_code=415,
            title="Unsupported Photo Type",
            detail="Photo uploads must be JPEG, PNG, or WebP images.",
            type="/problems/unsupported-photo-type",
            error_code="unsupported_photo_type",
        )
    payload = await value.read()
    if not payload:
        raise ProblemDetails(
            status_code=422,
            title="Invalid Photo Upload",
            detail="The uploaded photo was empty.",
            type="/problems/invalid-photo-upload",
            error_code="invalid_photo_upload",
        )
    if len(payload) > MAX_REFERENCE_PHOTO_BYTES:
        raise ProblemDetails(
            status_code=413,
            title="Photo Too Large",
            detail="Photo uploads must be 5 MB or smaller.",
            type="/problems/photo-too-large",
            error_code="photo_too_large",
        )
    return ReferenceImageUpload(
        content=payload,
        content_type=content_type,
        filename=filename,
    )


app = create_app()
