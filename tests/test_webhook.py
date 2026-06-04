"""End-to-end webhook + merge pipeline tests with Strava mocked via respx.

The app's outbound HTTP (default AsyncHTTPTransport) is intercepted by respx;
the test's calls into the app go through httpx.ASGITransport, which respx does
not patch, so the two don't interfere.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
import respx
from asgi_lifespan import LifespanManager

from app.config import get_settings
from app.main import app

HOST = "www.strava.com"
BASE_URL = "http://testserver"


def _event(object_id: int, owner_id: int = 42) -> dict:
    return {
        "aspect_type": "create",
        "object_type": "activity",
        "object_id": object_id,
        "owner_id": owner_id,
        "subscription_id": 1,
        "event_time": 1735725600,
        "updates": {},
    }


def _register_common_routes(router: respx.MockRouter) -> dict:
    """Register OAuth + activity/stream fetches; return the mutable route map."""
    router.route(method="POST", host=HOST, path="/oauth/token").mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "access-123",
                "refresh_token": "test-refresh-token",
                "expires_at": 9999999999,
                "expires_in": 21600,
                "token_type": "Bearer",
            },
        )
    )

    garmin = {
        "id": 111,
        "external_id": "garmin_push_99.fit",
        "device_name": "Garmin Forerunner 965",
        "type": "WeightTraining",
        "sport_type": "WeightTraining",
        "start_date": "2026-01-01T10:00:00Z",
        "elapsed_time": 3600,
        "has_heartrate": True,
        "athlete": {"id": 42},
        "sets": [],
    }
    hevy = {
        "id": 222,
        "external_id": "hevy-abc",
        "type": "WeightTraining",
        "sport_type": "WeightTraining",
        "start_date": "2026-01-01T10:00:20Z",
        "elapsed_time": 3500,
        "has_heartrate": False,
        "athlete": {"id": 42},
        "sets": [
            {"exercise": {"name": "Bench Press"}, "reps": 5, "weight": 80.0,
             "weight_units": "kilograms"},
            {"exercise": {"name": "Back Squat"}, "reps": 5, "weight": 100.0},
        ],
    }

    router.route(method="GET", host=HOST, path="/api/v3/activities/111").mock(
        return_value=httpx.Response(200, json=garmin)
    )
    router.route(method="GET", host=HOST, path="/api/v3/activities/222").mock(
        return_value=httpx.Response(200, json=hevy)
    )
    router.route(
        method="GET", host=HOST, path="/api/v3/activities/111/streams"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "heartrate": {"data": [70, 72, 74, 76, 78], "series_type": "time"},
                "time": {"data": [0, 60, 120, 180, 240], "series_type": "time"},
            },
        )
    )
    return {}


def _uploaded_json(request: httpx.Request) -> dict:
    """Extract the JSON `file` part from a multipart /uploads request body."""
    ctype = request.headers["content-type"]
    boundary = ctype.split("boundary=")[1].encode()
    for part in request.content.split(b"--" + boundary):
        if b'name="file"' in part:
            blank = part.find(b"\r\n\r\n")
            return json.loads(part[blank + 4 :].rstrip(b"\r\n-"))
    raise AssertionError("no file part in multipart upload")


async def _drain(ctx, max_iters: int = 500) -> None:
    """Await all per-event background tasks (and any they transitively await)."""
    for _ in range(max_iters):
        await asyncio.sleep(0)
        tasks = [t for t in list(ctx.background_tasks) if not t.done()]
        if not tasks:
            return
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.fixture
def merge_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MANAGE_WEBHOOK_SUBSCRIPTION", "false")
    monkeypatch.setenv("UPLOAD_POLL_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("UPLOAD_POLL_MAX_ATTEMPTS", "3")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# --- simple endpoints ------------------------------------------------------


@pytest.mark.usefixtures("merge_env")
async def test_health() -> None:
    with respx.mock(assert_all_called=False) as router:
        _register_common_routes(router)
        async with LifespanManager(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url=BASE_URL
            ) as client:
                resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


@pytest.mark.usefixtures("merge_env")
async def test_webhook_validation_ok() -> None:
    with respx.mock(assert_all_called=False) as router:
        _register_common_routes(router)
        async with LifespanManager(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url=BASE_URL
            ) as client:
                resp = await client.get(
                    "/webhook",
                    params={
                        "hub.mode": "subscribe",
                        "hub.verify_token": "test-verify-token",
                        "hub.challenge": "challenge-xyz",
                    },
                )
    assert resp.status_code == 200
    assert resp.json() == {"hub.challenge": "challenge-xyz"}


@pytest.mark.usefixtures("merge_env")
async def test_webhook_validation_bad_token() -> None:
    with respx.mock(assert_all_called=False) as router:
        _register_common_routes(router)
        async with LifespanManager(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url=BASE_URL
            ) as client:
                resp = await client.get(
                    "/webhook",
                    params={
                        "hub.mode": "subscribe",
                        "hub.verify_token": "wrong",
                        "hub.challenge": "challenge-xyz",
                    },
                )
    assert resp.status_code == 403


# --- full merge flow -------------------------------------------------------


@pytest.mark.usefixtures("merge_env")
async def test_full_merge_flow_deletes_originals() -> None:
    with respx.mock(assert_all_called=False) as router:
        _register_common_routes(router)
        upload_route = router.route(
            method="POST", host=HOST, path="/api/v3/uploads"
        ).mock(
            return_value=httpx.Response(
                201,
                json={
                    "id": 999,
                    "id_str": "999",
                    "external_id": None,
                    "error": None,
                    "status": "Your activity is ready.",
                    "activity_id": 555,
                },
            )
        )
        del_garmin = router.route(
            method="DELETE", host=HOST, path="/api/v3/activities/111"
        ).mock(return_value=httpx.Response(204))
        del_hevy = router.route(
            method="DELETE", host=HOST, path="/api/v3/activities/222"
        ).mock(return_value=httpx.Response(204))

        async with LifespanManager(app):
            ctx = app.state.ctx
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url=BASE_URL
            ) as client:
                r1 = await client.post("/webhook", json=_event(111))
                r2 = await client.post("/webhook", json=_event(222))
                assert r1.status_code == 200
                assert r2.status_code == 200
                await _drain(ctx)

        # The merged activity was uploaded exactly once.
        assert upload_route.call_count == 1
        payload = _uploaded_json(upload_route.calls[0].request)
        assert payload["version"] == "1.0"
        assert payload["start_time"] == "2026-01-01T10:00:00Z"  # from Garmin
        assert payload["elapsed_time"] == 3600
        assert len(payload["sets"]) == 2
        assert payload["sets"][0]["exercise_type"] == "BENCH_PRESS_GENERIC"
        assert payload["streams"]["heartrate"] == [70, 72, 74, 76, 78]
        assert payload["streams"]["time"] == [0, 60, 120, 180, 240]

        # Both originals were deleted; the merged id is remembered.
        assert del_garmin.called
        assert del_hevy.called
        assert 555 in ctx.processed_activity_ids
        assert {111, 222} <= ctx.processed_activity_ids
        assert ctx.matcher.pending_count() == 0


@pytest.mark.usefixtures("merge_env")
async def test_merge_keeps_originals_when_delete_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DELETE_ORIGINALS", "false")
    get_settings.cache_clear()

    with respx.mock(assert_all_called=False) as router:
        _register_common_routes(router)
        router.route(method="POST", host=HOST, path="/api/v3/uploads").mock(
            return_value=httpx.Response(
                201, json={"id": 999, "error": None, "activity_id": 555}
            )
        )
        del_garmin = router.route(
            method="DELETE", host=HOST, path="/api/v3/activities/111"
        ).mock(return_value=httpx.Response(204))
        del_hevy = router.route(
            method="DELETE", host=HOST, path="/api/v3/activities/222"
        ).mock(return_value=httpx.Response(204))

        async with LifespanManager(app):
            ctx = app.state.ctx
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url=BASE_URL
            ) as client:
                await client.post("/webhook", json=_event(111))
                await client.post("/webhook", json=_event(222))
                await _drain(ctx)

        assert not del_garmin.called
        assert not del_hevy.called
        assert 555 in ctx.processed_activity_ids


@pytest.mark.usefixtures("merge_env")
async def test_single_activity_waits_for_partner() -> None:
    with respx.mock(assert_all_called=False) as router:
        _register_common_routes(router)
        upload_route = router.route(
            method="POST", host=HOST, path="/api/v3/uploads"
        ).mock(return_value=httpx.Response(201, json={"id": 1, "activity_id": 2}))

        async with LifespanManager(app):
            ctx = app.state.ctx
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url=BASE_URL
            ) as client:
                await client.post("/webhook", json=_event(111))
                await _drain(ctx)

        # Only the Garmin half arrived: nothing uploaded, it stays buffered.
        assert upload_route.call_count == 0
        assert ctx.matcher.pending_count() == 1


@pytest.mark.usefixtures("merge_env")
async def test_non_strength_activity_ignored() -> None:
    with respx.mock(assert_all_called=False) as router:
        _register_common_routes(router)
        # Activity 111 re-mocked as a Run.
        router.route(method="GET", host=HOST, path="/api/v3/activities/111").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": 111,
                    "type": "Run",
                    "sport_type": "Run",
                    "start_date": "2026-01-01T10:00:00Z",
                    "elapsed_time": 1800,
                    "has_heartrate": True,
                    "athlete": {"id": 42},
                    "sets": [],
                },
            )
        )
        async with LifespanManager(app):
            ctx = app.state.ctx
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url=BASE_URL
            ) as client:
                await client.post("/webhook", json=_event(111))
                await _drain(ctx)
        assert ctx.matcher.pending_count() == 0


@pytest.mark.usefixtures("merge_env")
async def test_accepted_webhook_logs_full_raw_body(capsys) -> None:
    # On the success path we log the complete incoming payload so any
    # unexpected field/value/format is diagnosable from logs alone. The app
    # logs JSON to stdout (configure_logging clears caplog's handler at
    # startup), so we parse stdout like the logging tests do.
    with respx.mock(assert_all_called=False) as router:
        _register_common_routes(router)
        async with LifespanManager(app):
            ctx = app.state.ctx
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url=BASE_URL
            ) as client:
                await client.post("/webhook", json=_event(111))
                await _drain(ctx)

    records = [
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if line.strip().startswith("{")
    ]
    ingest = [
        r
        for r in records
        if r.get("stage") == "webhook_ingest"
        and r.get("message") == "Webhook event received"
    ]
    assert ingest, "expected a webhook_ingest 'received' log line"
    raw = json.loads(ingest[0]["raw_body"])
    # The full event body is present, including fields we don't extract.
    assert raw["object_id"] == 111
    assert raw["subscription_id"] == 1
    assert raw["event_time"] == 1735725600
    assert "updates" in raw


@pytest.mark.usefixtures("merge_env")
async def test_malformed_webhook_acknowledged() -> None:
    with respx.mock(assert_all_called=False) as router:
        _register_common_routes(router)
        async with LifespanManager(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url=BASE_URL
            ) as client:
                resp = await client.post(
                    "/webhook", content=b"{not json", headers={"content-type": "application/json"}
                )
    # We acknowledge malformed payloads (200) so Strava doesn't retry forever.
    assert resp.status_code == 200
    assert resp.json() == {"status": "ignored"}


# --- manual POST /merge ----------------------------------------------------


@pytest.fixture
def admin_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MANAGE_WEBHOOK_SUBSCRIPTION", "false")
    monkeypatch.setenv("UPLOAD_POLL_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("UPLOAD_POLL_MAX_ATTEMPTS", "3")
    monkeypatch.setenv("ADMIN_TOKEN", "secret-admin")
    monkeypatch.setenv("DELETE_ORIGINALS", "false")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _post_merge(client: httpx.AsyncClient, body: dict, token: str | None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return await client.post("/merge", json=body, headers=headers)


@pytest.mark.usefixtures("merge_env")
async def test_merge_endpoint_disabled_without_admin_token() -> None:
    with respx.mock(assert_all_called=False) as router:
        _register_common_routes(router)
        async with LifespanManager(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url=BASE_URL
            ) as client:
                resp = await _post_merge(
                    client, {"garmin_id": 111, "hevy_id": 222}, token=None
                )
    assert resp.status_code == 503


@pytest.mark.usefixtures("admin_env")
async def test_merge_endpoint_rejects_bad_token() -> None:
    with respx.mock(assert_all_called=False) as router:
        _register_common_routes(router)
        async with LifespanManager(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url=BASE_URL
            ) as client:
                resp = await _post_merge(
                    client, {"garmin_id": 111, "hevy_id": 222}, token="wrong"
                )
    assert resp.status_code == 401


@pytest.mark.usefixtures("admin_env")
async def test_merge_endpoint_dry_run_returns_payload_without_upload() -> None:
    with respx.mock(assert_all_called=False) as router:
        _register_common_routes(router)
        upload = router.route(
            method="POST", host=HOST, path="/api/v3/uploads"
        ).mock(return_value=httpx.Response(201, json={"id": 1, "activity_id": 2}))
        async with LifespanManager(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url=BASE_URL
            ) as client:
                resp = await _post_merge(
                    client,
                    {"garmin_id": 111, "hevy_id": 222, "dry_run": True},
                    token="secret-admin",
                )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "dry_run"
    assert body["payload"]["version"] == "1.0"
    assert len(body["payload"]["sets"]) == 2
    assert upload.call_count == 0  # nothing uploaded


@pytest.mark.usefixtures("admin_env")
async def test_merge_endpoint_real_run_uploads() -> None:
    with respx.mock(assert_all_called=False) as router:
        _register_common_routes(router)
        upload = router.route(
            method="POST", host=HOST, path="/api/v3/uploads"
        ).mock(
            return_value=httpx.Response(201, json={"id": 999, "activity_id": 555})
        )
        async with LifespanManager(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url=BASE_URL
            ) as client:
                resp = await _post_merge(
                    client,
                    {"garmin_id": 111, "hevy_id": 222},
                    token="secret-admin",
                )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "merged"
    assert body["merged_activity_id"] == 555
    assert upload.call_count == 1


@pytest.mark.usefixtures("admin_env")
async def test_merge_endpoint_non_pair_is_422() -> None:
    with respx.mock(assert_all_called=False) as router:
        _register_common_routes(router)
        # Re-mock 222 as a second Garmin activity -> not a Garmin+Hevy pair.
        router.route(method="GET", host=HOST, path="/api/v3/activities/222").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": 222,
                    "external_id": "garmin_push_22.fit",
                    "device_name": "Garmin Forerunner 965",
                    "type": "WeightTraining",
                    "sport_type": "WeightTraining",
                    "start_date": "2026-01-01T10:00:00Z",
                    "elapsed_time": 3600,
                    "has_heartrate": True,
                    "athlete": {"id": 42},
                    "sets": [],
                },
            )
        )
        async with LifespanManager(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url=BASE_URL
            ) as client:
                resp = await _post_merge(
                    client,
                    {"garmin_id": 111, "hevy_id": 222},
                    token="secret-admin",
                )
    assert resp.status_code == 422
    assert resp.json()["status"] == "no_op"
