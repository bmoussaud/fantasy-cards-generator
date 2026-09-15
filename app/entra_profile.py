from __future__ import annotations

import httpx

from app.generation import ReferenceImageUpload

GRAPH_PROFILE_PHOTO_URL = "https://graph.microsoft.com/v1.0/me/photo/$value"
GRAPH_PROFILE_PHOTO_SCOPE = "User.Read"
MAX_PROFILE_PHOTO_BYTES = 4 * 1024 * 1024
PROFILE_PHOTO_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp"}


class ProfilePhotoFetchError(RuntimeError):
    pass


async def fetch_profile_photo(access_token: str) -> ReferenceImageUpload | None:
    if not access_token:
        raise ProfilePhotoFetchError("Graph access token was not returned.")

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                GRAPH_PROFILE_PHOTO_URL,
                headers={"Authorization": "Bearer " + access_token},
                timeout=10.0,
            )
    except httpx.HTTPError as exc:
        raise ProfilePhotoFetchError("Graph profile photo request failed.") from exc

    if response.status_code == 404:
        return None
    if response.status_code >= 400:
        raise ProfilePhotoFetchError("Graph profile photo request was rejected.")

    content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
    if content_type not in PROFILE_PHOTO_CONTENT_TYPES:
        raise ProfilePhotoFetchError("Graph returned an unsupported profile photo type.")
    if len(response.content) > MAX_PROFILE_PHOTO_BYTES:
        raise ProfilePhotoFetchError("Graph returned an oversized profile photo.")

    return ReferenceImageUpload(
        content=response.content,
        content_type=content_type,
        filename="entra-profile-photo",
    )
