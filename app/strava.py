"""Strava HTTP client: every outbound call to the Strava API.

All authenticated calls go through :meth:`StravaClient._request`, which injects
a fresh bearer token (refreshing via :class:`~app.auth.TokenManager` when
needed). Non-2xx responses raise :class:`StravaApiError`, which carries the
method, URL, status and full response body so the orchestrator can emit the
stage-specific diagnostic logs the spec mandates without re-deriving anything.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx
from pydantic import ValidationError

from .auth import TokenManager
from .config import Settings
from .models import (
    PushSubscription,
    StravaActivity,
    StreamData,
    UploadStatus,
)

API_BASE = "https://www.strava.com/api/v3"
SUBSCRIPTIONS_URL = f"{API_BASE}/push_subscriptions"


class StravaApiError(Exception):
    """Raised on a non-2xx response or an unparseable success body."""

    def __init__(
        self,
        message: str,
        *,
        method: str,
        url: str,
        status_code: int | None = None,
        body: str | None = None,
        parse_error: str | None = None,
    ) -> None:
        super().__init__(message)
        self.method = method
        self.url = url
        self.status_code = status_code
        self.body = body
        self.parse_error = parse_error


@dataclass
class FetchResult:
    activity: StravaActivity
    raw: dict[str, Any]
    url: str


@dataclass
class StreamsResult:
    streams: dict[str, StreamData]
    url: str
    raw: Any
    keys_returned: list[str] = field(default_factory=list)


@dataclass
class UploadResult:
    status: UploadStatus
    url: str
    raw: Any


def _body_text(resp: httpx.Response) -> str:
    try:
        return resp.text
    except Exception:  # pragma: no cover - defensive
        return "<unreadable response body>"


class StravaClient:
    def __init__(
        self,
        settings: Settings,
        tokens: TokenManager,
        client: httpx.AsyncClient,
        logger: logging.Logger,
    ) -> None:
        self._settings = settings
        self._tokens = tokens
        self._client = client
        self._log = logger

    # -- low-level ---------------------------------------------------------

    async def _request(
        self, method: str, url: str, **kwargs: Any
    ) -> httpx.Response:
        token = await self._tokens.get_access_token(self._client)
        headers = {"Authorization": f"Bearer {token}"}
        headers.update(kwargs.pop("headers", {}) or {})
        return await self._client.request(method, url, headers=headers, **kwargs)

    # -- activities --------------------------------------------------------

    async def get_activity(self, activity_id: int) -> FetchResult:
        url = f"{API_BASE}/activities/{activity_id}"
        resp = await self._request("GET", url, params={"include_all_efforts": "false"})
        if resp.status_code != 200:
            raise StravaApiError(
                f"GET activity {activity_id} returned {resp.status_code}",
                method="GET",
                url=url,
                status_code=resp.status_code,
                body=_body_text(resp),
            )
        try:
            raw = resp.json()
        except Exception as exc:
            raise StravaApiError(
                f"GET activity {activity_id} returned non-JSON body",
                method="GET",
                url=url,
                status_code=resp.status_code,
                body=_body_text(resp),
                parse_error=f"{type(exc).__name__}: {exc}",
            ) from exc
        try:
            activity = StravaActivity.model_validate(raw)
        except ValidationError as exc:
            raise StravaApiError(
                f"GET activity {activity_id} body failed validation",
                method="GET",
                url=url,
                status_code=resp.status_code,
                body=_body_text(resp),
                parse_error=str(exc),
            ) from exc
        return FetchResult(activity=activity, raw=raw, url=url)

    async def get_streams(
        self, activity_id: int, keys: list[str]
    ) -> StreamsResult:
        url = f"{API_BASE}/activities/{activity_id}/streams"
        resp = await self._request(
            "GET",
            url,
            params={"keys": ",".join(keys), "key_by_type": "true"},
        )
        if resp.status_code != 200:
            raise StravaApiError(
                f"GET streams for {activity_id} returned {resp.status_code}",
                method="GET",
                url=url,
                status_code=resp.status_code,
                body=_body_text(resp),
            )
        try:
            raw = resp.json()
        except Exception as exc:
            raise StravaApiError(
                f"GET streams for {activity_id} returned non-JSON body",
                method="GET",
                url=url,
                status_code=resp.status_code,
                body=_body_text(resp),
                parse_error=f"{type(exc).__name__}: {exc}",
            ) from exc

        streams: dict[str, StreamData] = {}
        # key_by_type=true => {"heartrate": {...}, "time": {...}}; older/list
        # form => [{"type": "heartrate", ...}, ...]. Handle both.
        if isinstance(raw, dict):
            for key, value in raw.items():
                if isinstance(value, dict):
                    streams[key] = StreamData.model_validate(value)
        elif isinstance(raw, list):
            for item in raw:
                if isinstance(item, dict) and "type" in item:
                    streams[item["type"]] = StreamData.model_validate(item)

        return StreamsResult(
            streams=streams,
            url=url,
            raw=raw,
            keys_returned=list(streams.keys()),
        )

    async def delete_activity(self, activity_id: int) -> httpx.Response:
        url = f"{API_BASE}/activities/{activity_id}"
        resp = await self._request("DELETE", url)
        if resp.status_code not in (200, 204):
            raise StravaApiError(
                f"DELETE activity {activity_id} returned {resp.status_code}",
                method="DELETE",
                url=url,
                status_code=resp.status_code,
                body=_body_text(resp),
            )
        return resp

    # -- uploads -----------------------------------------------------------

    async def create_upload(
        self, payload: dict[str, Any], *, sport_type: str
    ) -> UploadResult:
        # Strava's structured strength format is a multipart file upload: the
        # JSON payload is sent as a `file` with data_type=json, not a JSON body.
        url = f"{API_BASE}/uploads"
        file_bytes = json.dumps(payload).encode("utf-8")
        resp = await self._request(
            "POST",
            url,
            files={"file": ("merged.json", file_bytes, "application/json")},
            data={"data_type": "json", "sport_type": sport_type},
        )
        if resp.status_code not in (200, 201):
            raise StravaApiError(
                f"POST upload returned {resp.status_code}",
                method="POST",
                url=url,
                status_code=resp.status_code,
                body=_body_text(resp),
            )
        try:
            raw = resp.json()
            status = UploadStatus.model_validate(raw)
        except (ValidationError, ValueError) as exc:
            raise StravaApiError(
                "POST upload returned an unparseable body",
                method="POST",
                url=url,
                status_code=resp.status_code,
                body=_body_text(resp),
                parse_error=f"{type(exc).__name__}: {exc}",
            ) from exc
        return UploadResult(status=status, url=url, raw=raw)

    async def get_upload(self, upload_id: int) -> UploadResult:
        url = f"{API_BASE}/uploads/{upload_id}"
        resp = await self._request("GET", url)
        if resp.status_code != 200:
            raise StravaApiError(
                f"GET upload {upload_id} returned {resp.status_code}",
                method="GET",
                url=url,
                status_code=resp.status_code,
                body=_body_text(resp),
            )
        try:
            raw = resp.json()
            status = UploadStatus.model_validate(raw)
        except (ValidationError, ValueError) as exc:
            raise StravaApiError(
                f"GET upload {upload_id} returned an unparseable body",
                method="GET",
                url=url,
                status_code=resp.status_code,
                body=_body_text(resp),
                parse_error=f"{type(exc).__name__}: {exc}",
            ) from exc
        return UploadResult(status=status, url=url, raw=raw)

    # -- push subscriptions ------------------------------------------------

    async def list_subscriptions(self) -> list[PushSubscription]:
        resp = await self._client.get(
            SUBSCRIPTIONS_URL,
            params={
                "client_id": self._settings.strava_client_id,
                "client_secret": self._settings.strava_client_secret,
            },
        )
        if resp.status_code != 200:
            raise StravaApiError(
                f"GET subscriptions returned {resp.status_code}",
                method="GET",
                url=SUBSCRIPTIONS_URL,
                status_code=resp.status_code,
                body=_body_text(resp),
            )
        return [PushSubscription.model_validate(item) for item in resp.json()]

    async def create_subscription(
        self, callback_url: str, verify_token: str
    ) -> PushSubscription:
        resp = await self._client.post(
            SUBSCRIPTIONS_URL,
            data={
                "client_id": self._settings.strava_client_id,
                "client_secret": self._settings.strava_client_secret,
                "callback_url": callback_url,
                "verify_token": verify_token,
            },
        )
        if resp.status_code not in (200, 201):
            raise StravaApiError(
                f"POST subscription returned {resp.status_code}",
                method="POST",
                url=SUBSCRIPTIONS_URL,
                status_code=resp.status_code,
                body=_body_text(resp),
            )
        return PushSubscription.model_validate(resp.json())

    async def delete_subscription(self, subscription_id: int) -> None:
        url = f"{SUBSCRIPTIONS_URL}/{subscription_id}"
        resp = await self._client.delete(
            url,
            params={
                "client_id": self._settings.strava_client_id,
                "client_secret": self._settings.strava_client_secret,
            },
        )
        if resp.status_code not in (200, 204):
            raise StravaApiError(
                f"DELETE subscription {subscription_id} returned {resp.status_code}",
                method="DELETE",
                url=url,
                status_code=resp.status_code,
                body=_body_text(resp),
            )
