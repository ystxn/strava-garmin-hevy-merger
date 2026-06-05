# Strava Strength Activity Merger

A small webhook-driven FastAPI service that watches for new **strength
activities** synced to Strava from **Garmin** and **Hevy**, merges them into a
single activity — Garmin's heart-rate stream + Hevy's exercise/set data — and
optionally deletes the two originals.

It runs as a single replica with all state held in memory (a short-lived
"pending" buffer). No database.

---

## How it works

```
Garmin → Strava ─┐                         ┌─ webhook POST /webhook
                 ├─ Strava push webhook ───┤
Hevy   → Strava ─┘                         └─ this service
                                                  │
   1. fetch activity detail                       ▼
   2. is it WeightTraining?  (else ignore)
   3. identify source (Garmin vs Hevy)
   4. buffer it; look for the partner
      (same athlete, start within MATCH_WINDOW, other source)
   5. on a match: fetch Garmin HR stream, take Hevy sets
   6. build merged JSON, upload to Strava, poll until ready
   7. DELETE_ORIGINALS=true → delete both originals
      DELETE_ORIGINALS=false → leave them, log all three IDs
```

If only one half ever arrives, the buffered entry is evicted after
`PENDING_TTL_SECONDS` and the activity is left untouched on Strava.

> **Note on the Strava upload format.** This service follows the JSON
> upload shape described in the spec (`sets` + `streams` in the POST body).
> Strava's public upload API historically accepts file uploads (`.fit`/`.tcx`/
> `.gpx`); if the JSON upload is rejected, the `upload`-stage diagnostic log
> contains the full outbound payload and Strava's full response so the schema
> can be corrected from logs alone. See **Diagnostics** below.

---

## Project layout

```
app/
  main.py            FastAPI app, webhook endpoints, merge orchestration
  config.py          Settings (pydantic-settings, env vars)
  auth.py            OAuth2 token refresh
  strava.py          Strava API client (all HTTP calls)
  matcher.py         Pending buffer, match logic, TTL eviction
  merger.py          Source identification + merged-payload construction
  models.py          Pydantic models for Strava payloads
  logging_setup.py   Structured JSON logging + diagnostic helpers
k8s/                 Deployment, Service, Ingress, ConfigMap, Secret (templates)
tests/               Unit + end-to-end tests (Strava mocked)
scripts/             get_refresh_token.py — one-time OAuth bootstrap helper
.github/workflows/   build-and-publish.yml — CI: test, build, push to GHCR
Dockerfile
requirements.txt / requirements-dev.txt
```

---

## Prerequisites

1. A [Strava API application](https://www.strava.com/settings/api) — note its
   **Client ID** and **Client Secret**.
2. A **refresh token** for your athlete account with scope
   `activity:read_all,activity:write` — mint it once with the bundled helper
   (see [Getting a Strava refresh token](#getting-a-strava-refresh-token)).
3. A **publicly reachable HTTPS URL** for `/webhook` — Strava will not deliver
   to plain HTTP or unreachable hosts. In production this is your Ingress host;
   for local testing use a tunnel (e.g. `cloudflared tunnel` or `ngrok http
   8000`).

---

## Configuration

| Variable | Default | Description |
|---|---|---|
| `STRAVA_CLIENT_ID` | — | OAuth app client ID *(secret)* |
| `STRAVA_CLIENT_SECRET` | — | OAuth app client secret *(secret)* |
| `STRAVA_REFRESH_TOKEN` | — | Refresh token for your athlete *(secret)* |
| `STRAVA_WEBHOOK_VERIFY_TOKEN` | — | Arbitrary string echoed during webhook validation *(secret)* |
| `PUBLIC_BASE_URL` | — | Public base URL, e.g. `https://strava-merger.example.com` |
| `PENDING_TTL_SECONDS` | `600` | How long to wait for the matching activity |
| `MATCH_WINDOW_SECONDS` | `300` | Start-time tolerance when pairing |
| `MERGED_ACTIVITY_VISIBILITY` | `only_me` | `everyone` / `followers_only` / `only_me` |
| `DELETE_ORIGINALS` | `true` | Delete both source activities after a confirmed merge |
| `LOG_LEVEL` | `INFO` | `INFO` or `DEBUG` |
| `MANAGE_WEBHOOK_SUBSCRIPTION` | `true` | Verify/create the Strava push subscription on startup |
| `FALLBACK_EXERCISE_TYPE` | `WEIGHT_TRAINING_GENERIC` | `exercise_type` used when a Hevy exercise name can't be mapped to a Strava enum (a per-category generic is tried first) |
| `ADMIN_TOKEN` | — | Bearer token guarding `POST /merge`; leave unset to disable that endpoint *(secret)* |

`MERGED_ACTIVITY_VISIBILITY` defaults to `only_me` on purpose — a fresh or
misconfigured deploy will never publish an activity more widely than intended.
Verify a few merges look right, then switch to your preferred visibility.

Secrets belong in the Kubernetes Secret; everything else in the ConfigMap.

### Manual merge endpoint

`POST /merge` runs the merge on two given activity IDs, for testing the
build + upload without recording real workouts repeatedly. It is disabled
unless `ADMIN_TOKEN` is set (then returns `503`), and requires a matching
bearer token (`401` otherwise). Roles are resolved from the activities, so the
order of `garmin_id`/`hevy_id` doesn't matter.

```bash
# dry_run: build + return the payload, no upload or delete (safe to repeat)
curl -X POST https://strava-merger.example.com/merge \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"garmin_id": 18780904442, "hevy_id": 18780893099, "dry_run": true}'
```

Drop `dry_run` (or set it `false`) to run the full pipeline, including deleting
the originals when `DELETE_ORIGINALS=true`.

---

## Getting a Strava refresh token

The service authenticates as a single athlete using a long-lived **refresh
token**. Mint it once with the bundled helper (standard library only — nothing
to install):

1. **One-time Strava setup.** In your
   [API application settings](https://www.strava.com/settings/api) set the
   **Authorization Callback Domain** to `localhost`. (Strava only redirects to
   domains registered there; the helper uses a local callback.)

2. **Run the helper.** It opens the Strava consent screen, captures the
   redirect on a tiny local web server, exchanges the code, and prints the
   refresh token:

   ```bash
   python3 scripts/get_refresh_token.py \
     --client-id <CLIENT_ID> --client-secret <CLIENT_SECRET>
   ```

   Client id/secret are also read from `STRAVA_CLIENT_ID` / `STRAVA_CLIENT_SECRET`,
   from a local `.env`, or prompted for if omitted. Approve **all** permission
   boxes so the grant includes `activity:read_all` and `activity:write`.

3. **Store the printed token.** The helper prints a `STRAVA_REFRESH_TOKEN=...`
   line for your `.env` and a ready-to-run `kubectl create secret` command.

On a headless/SSH machine where the browser can't reach this host, add
`--manual` and paste the redirected URL by hand. Change the local callback port
with `--port` (default `8721`) if it's in use.

---

## Local development

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt

cp .env.example .env   # fill in STRAVA_* values
# Set MANAGE_WEBHOOK_SUBSCRIPTION=false locally unless PUBLIC_BASE_URL is a
# live tunnel, otherwise startup will keep retrying to register the webhook.

uvicorn app.main:app --reload --port 8000
```

Health check:

```bash
curl localhost:8000/health        # {"status":"ok"}
```

Run the tests (no network — Strava is mocked):

```bash
pip install -r requirements-dev.txt
pytest
```

To receive real events locally, point a tunnel at port 8000, set
`PUBLIC_BASE_URL` to the tunnel URL, set `MANAGE_WEBHOOK_SUBSCRIPTION=true`,
and restart — the service registers the push subscription itself.

---

## CI/CD — build & publish (GitHub Packages)

`.github/workflows/build-and-publish.yml` runs on every push to `main` (and via
**Run workflow** / `workflow_dispatch`). It:

1. runs the test suite on Python 3.12, then
2. builds the container image and pushes it to **GHCR** at
   `ghcr.io/ystxn/strava-garmin-hevy-merger`, tagged `latest` and
   `sha-<commit>`.

It authenticates with the built-in `GITHUB_TOKEN` (no extra secrets needed);
the job grants itself `packages: write`.

**Package visibility / cluster pull access.** GHCR packages are private by
default. Either:

- make the package public — GitHub → repo → *Packages* → the package →
  *Package settings* → *Change visibility → Public*; or
- keep it private and give the cluster a pull secret, then uncomment
  `imagePullSecrets` in `k8s/deployment.yaml`:

  ```bash
  kubectl create secret docker-registry ghcr-pull -n default \
    --docker-server=ghcr.io \
    --docker-username=<github-username> \
    --docker-password=<PAT with read:packages>
  ```

**Rolling out a new image.** The deployment tracks `:latest` with
`imagePullPolicy: Always`, so a `kubectl rollout restart deployment/strava-merger
-n default` pulls the newest build. For reproducible deploys, pin the
`sha-<commit>` tag in `k8s/deployment.yaml` instead.

---

## Build & deploy (Kubernetes)

```bash
# The image is published to GHCR by CI (see above). To build locally instead:
#   docker build -t ghcr.io/ystxn/strava-garmin-hevy-merger:latest . && docker push ...

# Create the secret (don't commit real values). The refresh-token helper
# prints this exact command pre-filled — see "Getting a Strava refresh token".
kubectl create secret generic strava-merger-secret -n default \
  --from-literal=STRAVA_CLIENT_ID=... \
  --from-literal=STRAVA_CLIENT_SECRET=... \
  --from-literal=STRAVA_REFRESH_TOKEN=... \
  --from-literal=STRAVA_WEBHOOK_VERIFY_TOKEN=...

# Edit k8s/configmap.yaml + k8s/ingress.yaml to use your real host, then:
kubectl apply -f k8s/configmap.yaml
kubectl apply -f k8s/deployment.yaml
kubectl apply -f k8s/service.yaml
kubectl apply -f k8s/ingress.yaml
```

`replicas` **must stay 1** — the pending buffer is in-memory and unshared. For
HA, swap the in-memory dict in `matcher.py` for a Redis-backed store first.

On startup the service verifies (and creates if missing) the Strava push
subscription pointing at `PUBLIC_BASE_URL/webhook`. Strava allows one
subscription per application; a stale one with a different callback is replaced.

---

## Diagnostics

Logs are structured JSON on stdout. Every line carries `timestamp`, `level`,
`stage`, `athlete_id`, `garmin_activity_id`, `hevy_activity_id`, and `message`;
errors add a full `traceback` and stage-specific fields. Response bodies are
logged in full (the first 10 KB plus a byte count if larger than 50 KB).

Pipeline stages: `webhook_ingest`, `activity_fetch`, `stream_fetch`,
`source_identification`, `payload_build`, `upload`, `delete`. On startup the
resolved configuration is logged (secrets excluded).

Retrieve logs from the cluster:

```bash
kubectl logs -n default deployment/strava-merger --follow
kubectl logs -n default deployment/strava-merger --since=1h | grep '"level": "ERROR"'
```

The single most likely first-deploy failure is a schema mismatch on the JSON
upload — its diagnostic log includes the **full outbound payload** and Strava's
**full response**.

---

## Token rotation

The access token is refreshed automatically (proactively, before its ~6-hour
expiry). Strava normally returns the same refresh token; if it ever rotates,
the new value is held in memory and a WARNING is logged telling you to update
`STRAVA_REFRESH_TOKEN` in the Secret so it survives a restart. (The service
cannot write the Secret back itself without cluster credentials.)

---

## Limitations / future work

- **Single athlete** — one refresh token, hard-coded via env.
- **Single replica** — in-memory buffer; needs Redis for HA/multi-replica.
- **Manual refresh-token rotation** — only required in the rare case Strava
  rotates it (logged when it happens).
- **One half only** — if just Garmin or just Hevy syncs, the entry is evicted
  after the TTL and that activity is left untouched (no data loss, no merge).
