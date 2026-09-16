from __future__ import annotations

import httpx

from app.generation import ReferenceImageUpload

GRAPH_PROFILE_PHOTO_URL = "https://graph.microsoft.com/v1.0/me/photo/$value"
GRAPH_PROFILE_PHOTO_SCOPE = "User.Read"
MAX_PROFILE_PHOTO_BYTES = 4 * 1024 * 1024
PROFILE_PHOTO_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp"}


class ProfilePhotoFetchError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        error_code: str = "graph_photo_failed",
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.status_code = status_code


async def fetch_profile_photo(access_token: str) -> ReferenceImageUpload | None:
    if not access_token:
        raise ProfilePhotoFetchError(
            "Graph access token was not returned.", error_code="graph_token_missing"
        )

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                GRAPH_PROFILE_PHOTO_URL,
                headers={"Authorization": "Bearer " + access_token},
                timeout=10.0,
            )
    except httpx.HTTPError as exc:
        raise ProfilePhotoFetchError(
            "Graph profile photo request failed.", error_code="transport_error"
        ) from exc

    if response.status_code == 404:
        return None
    if response.status_code >= 400:
        raise ProfilePhotoFetchError(
            "Graph profile photo request was rejected.",
            error_code="graph_photo_rejected",
            status_code=response.status_code,
        )

    content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
    if content_type not in PROFILE_PHOTO_CONTENT_TYPES:
        raise ProfilePhotoFetchError(
            "Graph returned an unsupported profile photo type.", error_code="invalid_photo_upload"
        )
    if len(response.content) > MAX_PROFILE_PHOTO_BYTES:
        raise ProfilePhotoFetchError(
            "Graph returned an oversized profile photo.", error_code="invalid_photo_upload"
        )

    return ReferenceImageUpload(
        content=response.content,
        content_type=content_type,
        filename="entra-profile-photo",
    )
