# Strava Strength Activity Merger — Architecture & Implementation Spec

## Overview

A lightweight webhook-driven service that intercepts new strength activities synced from both Garmin and Hevy, merges them into a single activity containing Garmin's HR stream and Hevy's exercise/set data, and replaces the two originals with the merged result.

---

## Language & Framework

**Python 3.12 + FastAPI**

Rationale:
- Minimal boilerplate for webhook HTTP endpoints
- Native async support for concurrent Strava API calls
- Rich ecosystem for the data manipulation needed (httpx, pydantic)
- Lightest viable container footprint for a single-purpose service
- No need for a heavier framework; this service has exactly one job

Dependencies:
```
fastapi
uvicorn
httpx
pydantic
pydantic-settings
python-json-logger
```

No database required. State is held in-memory with a short TTL.

---

## High-Level Flow

```
Garmin syncs activity → Strava → webhook POST → service
Hevy syncs activity   → Strava → webhook POST → service
                                                    │
                              ┌─────────────────────┘
                              ▼
                   Is it a strength activity?
                         No → ignore
                         Yes → store in pending buffer
                              │
                              ▼
                   Does a matching pending activity exist?
                   (same athlete, start time within 5 min,
                    different source app)
                         No → wait (TTL: 10 min)
                         Yes → proceed with merge
                              │
                              ▼
                   Fetch full activity details from both
                   Fetch HR stream from Garmin activity
                   Fetch set/exercise data from Hevy activity
                              │
                              ▼
                   Build merged JSON upload payload
                   (sets from Hevy + heartrate stream from Garmin)
                              │
                              ▼
                   Upload merged activity via Strava JSON upload
                   Poll until upload confirmed
                              │
                              ▼
                   DELETE_ORIGINALS=true?
                     Yes → Delete Garmin activity
                           Delete Hevy activity
                     No  → Leave both intact, log activity IDs
                           of all three for manual cleanup
```

---

## Strava API Interactions

### Authentication
- OAuth2 with refresh tokens, one athlete (you), stored as env vars
- On startup, refresh the access token; refresh proactively before expiry
- Scope required: `activity:read_all,activity:write`

### Webhook subscription
- Strava requires a publicly reachable HTTPS endpoint for webhook delivery
- On startup, the service verifies the subscription exists and creates it if not
- Webhook validation (GET with `hub.challenge`) is handled automatically

### Endpoints used

| Purpose | Method | Endpoint |
|---|---|---|
| Receive webhook events | POST | `/webhook` (your service) |
| Validate webhook subscription | GET | `/webhook` (your service) |
| Get activity detail | GET | `/api/v3/activities/{id}` |
| Get activity streams | GET | `/api/v3/activities/{id}/streams?keys=heartrate,time` |
| Upload merged activity | POST | `/api/v3/uploads` (JSON format) |
| Poll upload status | GET | `/api/v3/uploads/{upload_id}` |
| Delete activity | DELETE | `/api/v3/activities/{id}` |

### Identifying source app
Strava activity detail includes a `device_name` or `external_id` field. The `external_id` from Garmin-sourced activities contains a `.fit` suffix; Hevy-sourced activities have a distinct external_id pattern. Additionally, `athlete.id` + start time window is the primary matching key.

Identification logic (in priority order):
1. `external_id` ends in `.fit` → Garmin
2. Check activity `name` patterns or `device_name` field
3. Fallback: whichever of the pair lacks set data is Garmin; whichever has sets is Hevy

### Merged JSON upload format

```json
{
  "sport_type": "WeightTraining",
  "start_time": "<ISO8601 from Garmin activity>",
  "elapsed_time": <seconds, from Garmin activity>,
  "description": "<optional: generated summary>",
  "visibility": "<from MERGED_ACTIVITY_VISIBILITY config>",
  "sets": [
    {
      "id": 0,
      "exercise": { "name": "Romanian Deadlift" },
      "reps": 8,
      "weight": 60.0,
      "weight_units": "kilograms",
      "start_index": 0,
      "end_index": 45
    }
  ],
  "streams": {
    "time": { "data": [0, 1, 2, ...] },
    "heartrate": { "data": [72, 75, 78, ...] }
  }
}
```

Notes:
- `visibility` maps as follows: `everyone` → `"everyone"`, `followers_only` → `"followers_only"`, `only_me` → `"only_me"`. Default is `only_me` so merged activities are private until you verify the output looks correct.
- `start_index` / `end_index` in set objects refer to positions in the time stream; these can be approximated from set timestamps relative to activity start if available, or omitted (field is optional)
- `weight_units` should match your preference (kilograms)
- All set data is sourced from the Hevy activity's set list as returned by Strava's GET activity endpoint

---

## In-Memory Pending Buffer

A simple dict keyed by `athlete_id:start_time_bucket` (bucketed to 5-minute windows).

```python
pending: dict[str, PendingActivity] = {}

@dataclass
class PendingActivity:
    activity_id: int
    source: Literal["garmin", "hevy"]
    start_time: datetime
    received_at: datetime  # for TTL eviction
```

TTL eviction: a background task runs every 2 minutes and removes entries older than 10 minutes. This handles the case where only one activity arrives (e.g. Hevy not synced).

No persistence needed — if the service restarts mid-merge, the worst case is both activities remain on Strava unmerged, same as today.

---

## Project Structure

```
strava-merger/
├── app/
│   ├── main.py           # FastAPI app, webhook endpoints
│   ├── config.py         # Settings via pydantic-settings (env vars)
│   ├── auth.py           # Token refresh logic
│   ├── strava.py         # Strava API client (all HTTP calls)
│   ├── matcher.py        # Pending buffer, match logic, TTL eviction
│   ├── merger.py         # Build merged JSON payload
│   └── models.py         # Pydantic models for Strava payloads
├── k8s/
│   ├── deployment.yaml
│   ├── service.yaml
│   ├── ingress.yaml
│   └── secret.yaml       # Template only, no real values
├── Dockerfile
├── requirements.txt
└── README.md
```

---

## Configuration (Environment Variables)

| Variable | Description |
|---|---|
| `STRAVA_CLIENT_ID` | OAuth app client ID |
| `STRAVA_CLIENT_SECRET` | OAuth app client secret |
| `STRAVA_REFRESH_TOKEN` | Initial refresh token for your athlete account |
| `STRAVA_WEBHOOK_VERIFY_TOKEN` | Arbitrary secret string for webhook validation |
| `PUBLIC_BASE_URL` | Publicly reachable base URL of this service (e.g. `https://strava-merger.yourdomain.com`) |
| `PENDING_TTL_SECONDS` | How long to wait for matching activity (default: 600) |
| `MATCH_WINDOW_SECONDS` | Start time tolerance for matching (default: 300) |
| `MERGED_ACTIVITY_VISIBILITY` | Privacy of the merged activity: `everyone`, `followers_only`, or `only_me` (default: `only_me`) |
| `DELETE_ORIGINALS` | Whether to delete the Garmin and Hevy source activities after a successful merge: `true` or `false` (default: `true`) |
| `LOG_LEVEL` | `INFO` or `DEBUG` |

Secrets (`STRAVA_*`, `STRAVA_WEBHOOK_VERIFY_TOKEN`) go into a Kubernetes Secret. All other variables go into a ConfigMap.

---

## Kubernetes Manifests

### Deployment

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: strava-merger
  namespace: default
spec:
  replicas: 1          # must be 1; in-memory state is not shared
  selector:
    matchLabels:
      app: strava-merger
  template:
    metadata:
      labels:
        app: strava-merger
    spec:
      containers:
        - name: strava-merger
          image: strava-merger:latest
          ports:
            - containerPort: 8000
          envFrom:
            - secretRef:
                name: strava-merger-secret
            - configMapRef:
                name: strava-merger-config
          readinessProbe:
            httpGet:
              path: /health
              port: 8000
            initialDelaySeconds: 5
            periodSeconds: 10
          resources:
            requests:
              cpu: 50m
              memory: 64Mi
            limits:
              cpu: 200m
              memory: 128Mi
```

**replicas: 1 is required** — the pending buffer is in-memory. If you later want HA, replace the dict with a Redis-backed store (straightforward refactor).

### Service

```yaml
apiVersion: v1
kind: Service
metadata:
  name: strava-merger
spec:
  selector:
    app: strava-merger
  ports:
    - port: 80
      targetPort: 8000
```

### Ingress

Assumes your existing ingress controller and cert-manager setup on the cluster.

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: strava-merger
  annotations:
    cert-manager.io/cluster-issuer: letsencrypt-prod
spec:
  rules:
    - host: strava-merger.yourdomain.com
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: strava-merger
                port:
                  number: 80
  tls:
    - hosts:
        - strava-merger.yourdomain.com
      secretName: strava-merger-tls
```

### ConfigMap (template)

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: strava-merger-config
data:
  PUBLIC_BASE_URL: "https://strava-merger.yourdomain.com"
  PENDING_TTL_SECONDS: "600"
  MATCH_WINDOW_SECONDS: "300"
  MERGED_ACTIVITY_VISIBILITY: "only_me"
  DELETE_ORIGINALS: "true"
  LOG_LEVEL: "INFO"
```

### Secret (template)

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: strava-merger-secret
type: Opaque
stringData:
  STRAVA_CLIENT_ID: ""
  STRAVA_CLIENT_SECRET: ""
  STRAVA_REFRESH_TOKEN: ""
  STRAVA_WEBHOOK_VERIFY_TOKEN: ""
```

---

## Dockerfile

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ ./app/
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

---

## Key Implementation Notes for Claude Code

1. **Webhook validation endpoint** — Strava sends a GET to `/webhook` with `hub.mode=subscribe`, `hub.verify_token`, and `hub.challenge`. Respond with `{"hub.challenge": "<value>"}` and 200. This must work before any activity events arrive.

2. **Token refresh** — The access token expires every 6 hours. Implement a wrapper around all Strava API calls that checks expiry and refreshes proactively. Store the current token and expiry in-memory; persist the latest refresh token back to the environment or a mounted secret if possible, otherwise log it so it can be manually updated.

3. **Race condition** — Both webhook events may arrive within milliseconds of each other. Use `asyncio.Lock` per athlete when processing a match to prevent double-processing.

4. **Upload polling** — The JSON upload is asynchronous. After POST to `/api/v3/uploads`, poll GET `/api/v3/uploads/{upload_id}` with 1-second intervals until `activity_id` is non-null or `error` is set. Only proceed to the deletion step after confirming the upload succeeded and the merged activity ID is known.

5. **Deletion behaviour** — Controlled by `DELETE_ORIGINALS`. When `true`, delete both source activities after confirmed upload. When `false`, skip deletion entirely and log a message at INFO level containing the Garmin activity ID, Hevy activity ID, and the new merged activity ID so you can manually clean up. This is useful during initial rollout to verify merge output before enabling permanent deletion.

6. **Merged activity visibility** — The `MERGED_ACTIVITY_VISIBILITY` value must be validated at startup against the allowed set (`everyone`, `followers_only`, `only_me`). Default to `only_me`. Pass the value directly into the JSON upload payload as the `visibility` field. Defaulting to `only_me` means a misconfigured or first-time deploy will never accidentally make an activity more public than intended.

7. **Activity identification** — When both activities arrive and are matched, fetch full detail for both. The one with a non-empty `sets` array (from the Strava GET activity response) is Hevy. The one with a populated `heartrate` stream is Garmin. Verify both conditions before proceeding.

8. **Graceful no-op** — If after fetching full details one activity has no sets and the other also has no HR stream, log a warning and do not delete either. Fail safe: leave both activities intact rather than risk data loss.

9. **Health endpoint** — `GET /health` returns `{"status": "ok"}` for the readiness probe.

10. **Logging** — Log at INFO: webhook received, match found, upload succeeded, merged activity ID, deletions confirmed (or skipped with IDs if `DELETE_ORIGINALS=false`). Log at DEBUG: full payloads. Never log access tokens.

---

## Diagnostic Logging Requirements

This service is likely to encounter failures on first deploy due to undocumented API behaviour, JSON schema mismatches, or unexpected Strava response shapes. All failures must be logged with enough context that the bug can be diagnosed and fixed from logs alone, without needing to reproduce the failure.

### Logging format

Use structured JSON logging throughout (use the `python-json-logger` package). Every log entry must include:

```json
{
  "timestamp": "<ISO8601>",
  "level": "INFO|WARNING|ERROR",
  "stage": "<which stage of the pipeline failed>",
  "athlete_id": "<int>",
  "garmin_activity_id": "<int or null>",
  "hevy_activity_id": "<int or null>",
  "message": "<human-readable summary>",
  ...stage-specific fields...
}
```

Add `python-json-logger` to `requirements.txt`.

### Stage-specific diagnostic fields

Each pipeline stage must emit a structured ERROR log on failure containing all of the following fields. These are the minimum required for Claude Code to diagnose the issue without trial-and-error.

#### Stage: `webhook_ingest`
Failures: unexpected payload shape, missing fields, JSON parse errors.
```json
{
  "stage": "webhook_ingest",
  "raw_body": "<full raw request body as string>",
  "headers": { "<relevant headers>" },
  "parse_error": "<exception message and type>"
}
```

#### Stage: `activity_fetch`
Failures: non-200 response, unexpected response schema, missing expected fields.
```json
{
  "stage": "activity_fetch",
  "activity_id": "<int>",
  "http_method": "GET",
  "url": "<full URL called>",
  "http_status": "<int>",
  "response_body": "<full response body as string>",
  "expected_fields": ["sets", "start_date", "elapsed_time", "..."],
  "missing_fields": ["<fields present in expected but absent in response>"],
  "error": "<exception message>"
}
```

#### Stage: `stream_fetch`
Failures: HR stream absent, non-200, stream data malformed.
```json
{
  "stage": "stream_fetch",
  "activity_id": "<int>",
  "url": "<full URL called>",
  "http_status": "<int>",
  "response_body": "<full response body as string>",
  "keys_requested": ["heartrate", "time"],
  "keys_returned": ["<list of keys actually present in response>"],
  "heartrate_sample_count": "<int or null>",
  "time_sample_count": "<int or null>"
}
```

#### Stage: `source_identification`
Failures: unable to determine which activity is Garmin and which is Hevy.
```json
{
  "stage": "source_identification",
  "activity_a_id": "<int>",
  "activity_a_external_id": "<string>",
  "activity_a_device_name": "<string>",
  "activity_a_has_sets": "<bool>",
  "activity_a_has_heartrate": "<bool>",
  "activity_b_id": "<int>",
  "activity_b_external_id": "<string>",
  "activity_b_device_name": "<string>",
  "activity_b_has_sets": "<bool>",
  "activity_b_has_heartrate": "<bool>",
  "reason": "<why identification failed>"
}
```

#### Stage: `payload_build`
Failures: set data in unexpected format, missing required fields for JSON upload, type errors during construction.
```json
{
  "stage": "payload_build",
  "hevy_sets_raw": "<full sets array from Hevy activity as received from Strava API>",
  "garmin_start_date": "<string>",
  "garmin_elapsed_time": "<int>",
  "heartrate_stream_length": "<int>",
  "time_stream_length": "<int>",
  "constructed_payload": "<the payload dict that was being built, even if incomplete>",
  "error": "<exception type, message, and full traceback>"
}
```

#### Stage: `upload`
Failures: non-200/201 from upload endpoint, upload polling timeout, upload error field set by Strava.
```json
{
  "stage": "upload",
  "http_method": "POST",
  "url": "<full URL>",
  "request_payload": "<full JSON payload sent — redact nothing except tokens>",
  "http_status": "<int>",
  "response_body": "<full response body>",
  "upload_id": "<int or null>",
  "poll_attempts": "<int>",
  "final_poll_response": "<full body of last poll response>",
  "strava_error_message": "<error field from Strava upload status if set>"
}
```

#### Stage: `delete`
Failures: non-200 response on delete, unexpected error.
```json
{
  "stage": "delete",
  "activity_id": "<int>",
  "source": "garmin|hevy",
  "http_status": "<int>",
  "response_body": "<full response body>",
  "error": "<exception message>"
}
```

### Additional requirements

- **Full tracebacks** — all ERROR logs must include the full Python traceback as a `traceback` field (use `traceback.format_exc()`), not just the exception message.
- **Never truncate** — response bodies and payloads must never be truncated in error logs. If a response body is very large (>50KB), log the first 10KB and note the total byte count.
- **Log the full outbound payload on upload failure** — the single most common failure mode will be a schema mismatch on the JSON upload. The full payload must be in the log.
- **Warn on unexpected fields** — if the Strava API returns fields not present in the Pydantic models, log a WARNING with the unexpected field names and values. This signals that the API has changed or that assumptions about the schema are wrong.
- **Startup validation log** — on startup, log all resolved config values at INFO level (excluding secrets). This confirms the running configuration without requiring a deployment inspection.

### Retrieving logs from the cluster

Add to README:
```bash
kubectl logs -n default deployment/strava-merger --follow
kubectl logs -n default deployment/strava-merger --since=1h | grep '"level":"ERROR"'
```



- Multi-athlete support (currently hardcoded to one refresh token)
- Redis-backed pending store for HA/multi-replica
- Persistent refresh token rotation (manual rotation required on expiry if not addressed)
- Handling the case where Hevy sends but Garmin doesn't (already handled by TTL eviction — Hevy activity remains untouched on Strava)
- Handling the case where Garmin sends but Hevy doesn't (same — Garmin activity remains, no HR+sets merge but also no data loss)
