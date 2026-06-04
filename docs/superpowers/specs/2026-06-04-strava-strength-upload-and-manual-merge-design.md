# Strava-accurate strength upload + manual merge trigger

Date: 2026-06-04
Branch: `feat/parse-hevy-sets-from-description`

## Background

Confirmed (byte-exact, via the `raw_payload` instrumentation): Strava's
`GET /activities/{id}` returns no structured `sets` array for strength
workouts. Hevy writes the workout into the free-text `description`; the only
accessible source of set data through the public API is that text.

Separately, research into Strava's `/uploads` docs revealed the merger's
existing upload path is wrong on two counts:

1. **Set shape:** the documented strength JSON uses
   `{exercise_type (enum, required), repetitions, weight (kg), duration,
   start_time}` — not the `{exercise:{name}, reps, weight, weight_units}` the
   code currently emits.
2. **Transport:** the strength JSON is a **multipart file upload** to
   `POST /uploads` (`file` + `data_type="json"` + `sport_type`), not a raw JSON
   request body.

So a merge that resolves the pair correctly would still produce a merged
activity with an empty/incorrect exercises section. This work fixes the
end-to-end path and adds a manual trigger to test it without doing real
workouts repeatedly.

## Goals

- Parse Hevy sets from `description` and emit a Strava-accurate strength upload.
- Map Hevy free-text exercise names to Strava `exercise_type` enums.
- Upload via the correct multipart mechanism.
- Add a token-guarded `POST /merge` endpoint to run the merge on two given
  Strava activity IDs, with a dry-run mode.

## Components

### 1. Exercise mapping — `app/exercises.py` (new, pure)

`to_exercise_type(name: str, *, fallback: str) -> tuple[str, bool]` returning
`(exercise_type, matched)` (matched=False means fallback was used, so the
caller can warn). Three tiers:

1. **Curated dict** — known names → exact enum. Seed:
   - `"Deadlift (Barbell)"` → `BARBELL_DEADLIFT`
   - `"Shoulder Press (Dumbbell)"` → `OVERHEAD_DUMBBELL_PRESS`
   - `"Seated Cable Row - V Grip (Cable)"` → `SEATED_CABLE_ROW`
   - `"Hip Thrust (Machine)"` → `HIP_THRUST`
   - `"Sit Up (Weighted)"` → `SIT_UP_GENERIC`
   - `"Hip Abduction (Machine)"` → `MACHINE_HIP_ABDUCTION`
2. **Keyword → category `_GENERIC`** — match a movement keyword in the name:
   deadlift→`DEADLIFT_GENERIC`, row→`ROW_GENERIC`, press/shoulder→
   `SHOULDER_PRESS_GENERIC`, squat→`SQUAT_GENERIC`, curl→`CURL_GENERIC`,
   lunge→`LUNGE_GENERIC`, "hip thrust"/"hip raise"→`HIP_RAISE_GENERIC`,
   "sit up"/situp/crunch→`SIT_UP_GENERIC`, "hip abduction"→`HIP_STABILITY_GENERIC`.
   (Curated dict wins over keyword.)
3. **Fallback** — `fallback` arg (from `settings.fallback_exercise_type`,
   default `"WEIGHT_TRAINING_GENERIC"`); caller logs a WARNING naming the
   unmapped exercise.

Matching is case-insensitive and tolerant of surrounding text.

### 2. Parser — `app/hevy.py` (revised)

Keep description parsing. Each set dict keeps `name`, `reps`, `weight`,
`weight_units` (so the merger can map + convert). Unchanged otherwise.

### 3. Merged payload — `app/merger.py` (revised)

`build_merged_payload(...)` emits the documented JSON strength shape:

- Top level: `version: "1.0"`, `start_time` (Garmin `start_date`),
  `utc_offset` (int, from Garmin), `elapsed_time` (Garmin), `active_time`
  (Garmin `moving_time`), `streams: {"time": [...], "heartrate": [...]}` (bare
  arrays), `sets: [...]`, `description` (our summary line).
- Per set (`_extract_set` rewritten): `{exercise_type, repetitions, weight}`
  with **weight converted to kilograms** (pounds→kg ×0.45359237); include
  `duration` only when the source set has one. Drop `exercise.name`, `reps`,
  `weight_units`, `id`, `start_index`/`end_index`.
- `sport_type` ("WeightTraining") returned alongside the payload (it is a
  multipart form field, not a JSON body field). `build_merged_payload` returns
  the JSON dict; the caller passes `sport_type` to the upload separately.
- `required_payload_problems(payload)` updated: require `version`, `start_time`,
  `elapsed_time`, ≥1 set each with a non-empty `exercise_type`, and a non-empty
  `heartrate` stream.

### 4. Upload transport — `app/strava.py` (revised)

`create_upload` switches from `json=payload` to multipart/form-data:
`files={"file": ("merged.json", json_bytes, "application/json")}`,
`data={"data_type": "json", "sport_type": sport_type}`. Same `UploadStatus`
return and polling. Signature gains `sport_type`.

### 5. Config — `app/config.py`

- `admin_token: Optional[str] = None` (`ADMIN_TOKEN`) — guards `/merge`;
  when unset the endpoint is disabled (503).
- `fallback_exercise_type: str = "WEIGHT_TRAINING_GENERIC"`.

### 6. Manual trigger — `app/main.py`

`POST /merge`, body `{garmin_id: int, hevy_id: int, dry_run: bool = false}`,
header `Authorization: Bearer <ADMIN_TOKEN>`.

- `admin_token` unset → 503; missing/incorrect token → 401.
- Fetch both activities by ID (raw_payload-logged like the webhook path).
- Resolve roles (validate they form a Garmin+Hevy pair); if not → 422 with the
  reason.
- Build the payload (parse → map → kg).
- `dry_run=true` → log + **return the payload** in the response; no upload, no
  delete.
- `dry_run=false` → upload (multipart) + poll, then honor `DELETE_ORIGINALS`
  exactly like the automatic flow.
- Runs synchronously (awaited) so the response carries the outcome:
  `{status, merged_activity_id?, payload? (dry-run), problems?}`.

## Data flow (manual)

POST /merge → auth → fetch garmin + hevy → resolve roles → parse Hevy sets →
map exercise_type + convert kg → build JSON strength payload →
(dry_run ? return payload : multipart upload → poll → honor DELETE_ORIGINALS)
→ JSON response.

## Error handling

Reuse the stage logging. Unmapped exercise → WARNING + fallback enum. Fetch
failure / bad ID → error response with the upstream status. Unidentifiable pair
→ 422. Disabled/unauthorized → 503/401.

## Testing

- `app/exercises.py`: curated hit, keyword→generic, fallback+`matched=False`.
- `app/hevy.py`: existing parser tests stand.
- `app/merger.py`: new payload shape (`version`, `utc_offset` int, bare-array
  streams, per-set `exercise_type`/`repetitions`/kg `weight`), lbs→kg
  conversion, `required_payload_problems`.
- `app/strava.py`: `create_upload` sends multipart with the JSON file +
  `data_type=json` + `sport_type` (respx assertion).
- `POST /merge`: 503 when disabled, 401 on bad token, dry-run returns payload
  without calling upload, real run uploads (respx), 422 on a non-pair.

## Out of scope (YAGNI)

- Parsing timed-exercise durations from Hevy text (no such data exists yet and
  Hevy's text format for it is unknown). `duration` is passed through if a
  source set already has it.
- Mapping all ~400 `exercise_type` values; curated + keyword-generic + fallback
  is sufficient.

## Assumptions to verify empirically (the endpoint exists to do this)

- Validity of `WEIGHT_TRAINING_GENERIC` as a fallback enum.
- Whether the JSON upload accepts `description` and the `{time, heartrate}`
  streams shape.

Use `POST /merge` with `dry_run` to inspect the payload, then a real run to
confirm Strava accepts it and the exercises section populates.
