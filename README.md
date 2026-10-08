# Geospatial Measurement API

A FastAPI service that accepts a **Shapefile (`.zip`)** or **KML (`.kml`)**, extracts every feature, and returns
**area (m²) for polygons** and **length (m) for lines** — always computed in a projected CRS, never in degrees.

- **Backend:** FastAPI + Uvicorn · PostgreSQL + async SQLAlchemy (asyncpg) · GeoPandas / Shapely / PyProj / Fiona · Pydantic v2
- **Frontend:** React + TypeScript (Vite) · Tailwind CSS · TanStack Query · Leaflet — served by the API at `/`
- **Processing:** upload returns immediately; parsing + measuring runs as a background task
- **Deploy:** Docker (multi-stage, builds the frontend), Docker Compose, Render (`render.yaml`)

---

## Table of contents

1. [Local setup](#local-setup)
2. [API](#api)
3. [Architecture](#architecture)
4. [CRS handling strategy](#crs-handling-strategy)
5. [Frontend](#frontend)
6. [Design decisions & alternatives](#design-decisions--alternatives)
7. [Error handling](#error-handling)
8. [Testing](#testing)
9. [Deployment (Render)](#deployment-render)
10. [Learnings & future scope](#learnings--future-scope)

---

## Local setup

### Option A — Docker Compose (recommended)

```bash
cp .env.example .env        # optional; sensible defaults are built in
docker compose up --build
```

Web app: <http://localhost:8000> · Interactive API docs: <http://localhost:8000/docs> · Health: <http://localhost:8000/health>

This starts PostgreSQL 16 and the API. The image is multi-stage: Node builds the React frontend, and the Python image
serves it at `/` next to the API, so one container is the whole application. The API waits for the database healthcheck.
Data persists in the `pgdata` and `uploads` volumes (`docker compose down -v` wipes them).

> **Schema changes:** tables are created with `create_all`, which never alters existing tables (there are no migrations
> yet). After pulling a version that adds columns, recreate the database: `docker compose down -v && docker compose up --build`.

> On Apple Silicon the image compiles Fiona against system GDAL (Fiona publishes no Linux arm64 wheels), so the
> first build takes a couple of minutes. On x86_64 everything installs from wheels.

### Option B — Without Docker

Requires Python 3.12 and a running PostgreSQL.

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt      # use requirements.txt for runtime only

createdb geospatial                      # or any database you like
cp .env.example .env                     # then edit DATABASE_URL if needed

uvicorn app.main:app --reload
```

Tables are created automatically on startup. The API is at <http://localhost:8000>. If `frontend/dist` exists (see
[Frontend](#frontend)) it is served at `/`; otherwise the app runs API-only.

### Configuration

All settings come from environment variables (`.env` is loaded via `python-dotenv`). See [`.env.example`](.env.example).

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | *(required)* | `postgresql+asyncpg://user:pass@host:5432/db`. `postgres://` / `postgresql://` are rewritten to the asyncpg driver automatically. |
| `UPLOAD_DIR` | `uploads` | Where raw uploads are stored (as `<uuid>.<ext>`). |
| `MAX_UPLOAD_SIZE_MB` | `50` | Upload limit (→ `413`). Rejected immediately from the `Content-Length` header, before the body is read; requests without that header (chunked) are checked while streaming. |
| `MAX_EXTRACTED_SIZE_MB` / `MAX_ZIP_MEMBERS` | `500` / `200` | Zip-bomb guards. |
| `DEFAULT_PAGE_SIZE` / `MAX_PAGE_SIZE` | `50` / `500` | Measurement pagination. |
| `LIST_DEFAULT_PAGE_SIZE` / `LIST_MAX_PAGE_SIZE` | `20` / `100` | File-listing pagination. |
| `ALLOWED_ORIGINS` | `*` in development, none otherwise | Comma-separated CORS origins, e.g. `http://localhost:5173,https://app.example.com`. |
| `FRONTEND_DIST_DIR` | `frontend/dist` | Built frontend to serve at `/` (skipped if absent). |
| `LOG_LEVEL` / `LOG_FORMAT` | `INFO` / `json` | `json` for production, `console` for humans. |
| `DB_POOL_SIZE` / `DB_MAX_OVERFLOW` | `5` / `10` | Connection pool. |

---

## API

Base path: `/api/files/`. Interactive OpenAPI docs are served at `/docs`.

### `POST /api/files/` — upload

Multipart upload, field name `file`. Accepts `.zip` (containing a Shapefile with `.shp/.shx/.dbf/.prj`) or `.kml`.
Returns **202 Accepted** immediately; processing continues in the background.

```bash
curl -X POST http://localhost:8000/api/files/ -F "file=@survey.kml"
```

```json
{
  "id": "d518ba8f-66b0-491e-8c45-86af0a5acb53",
  "filename": "survey.kml",
  "status": "PENDING"
}
```

### `GET /api/files/` — list uploads

Newest first. Query params: `page` (default `1`), `page_size` (default `20`, max `100`).

```bash
curl "http://localhost:8000/api/files/?page=1&page_size=2"
```

```json
{
  "page": 1,
  "page_size": 2,
  "total": 2,
  "total_pages": 1,
  "items": [
    {
      "id": "212fd68c-6eee-4a1d-aa0e-50f03015646b",
      "filename": "t.kml",
      "status": "COMPLETED",
      "feature_count": 3,
      "crs": "EPSG:4326",
      "created_at": "2026-10-08T22:39:27.425613Z"
    },
    {
      "id": "0a70dc75-23a3-4f6f-a2cb-d6a67a8641ea",
      "filename": "custom.zip",
      "status": "COMPLETED",
      "feature_count": 1,
      "crs": "CUSTOM:PROJCRS[\"unknown\",BASEGEOGCRS[\"unknown\",DATUM[\"D_Unknown_based_on_WGS_84_ellipsoid\",ELLIPSOID[\"WGS 8",
      "created_at": "2026-10-08T22:39:25.810029Z"
    }
  ]
}
```

### `GET /api/files/{id}/` — file metadata

```bash
curl http://localhost:8000/api/files/d518ba8f-66b0-491e-8c45-86af0a5acb53/
```

```json
{
  "id": "d518ba8f-66b0-491e-8c45-86af0a5acb53",
  "filename": "survey.kml",
  "feature_count": 3,
  "crs": "EPSG:4326",
  "status": "COMPLETED",
  "error": null,
  "warnings": [],
  "file_size": 557,
  "created_at": "2026-10-08T22:21:08.192249Z",
  "updated_at": "2026-10-08T22:21:08.213687Z"
}
```

`status` is one of `PENDING → PROCESSING → COMPLETED | FAILED`. While not `COMPLETED`, `feature_count` and `crs`
are `null`; when `FAILED`, `error` explains why. Poll this endpoint after uploading.

`warnings` lists non-fatal problems. If one folder (layer) of a multi-folder KML cannot be read, it is skipped, the file
is still `COMPLETED`, and the skipped layer is reported instead of being dropped silently:

```json
{ "status": "COMPLETED", "feature_count": 1, "warnings": ["KML layer 'Roads' could not be read and was skipped; its features are missing."] }
```

`crs` is `EPSG:<code>` only when the file's CRS exactly matches that EPSG definition; otherwise it is
`CUSTOM:<first 100 characters of its WKT>` (see [CRS labelling](#crs-labelling)).

### `GET /api/files/{id}/measurements/` — per-feature measurements

Query params: `page` (default `1`), `page_size` (default `50`, max `500`).

```bash
curl "http://localhost:8000/api/files/d518ba8f-66b0-491e-8c45-86af0a5acb53/measurements/?page=1&page_size=2"
```

```json
{
  "file_id": "d518ba8f-66b0-491e-8c45-86af0a5acb53",
  "page": 1,
  "page_size": 2,
  "total": 3,
  "total_pages": 2,
  "items": [
    {
      "feature_id": 0,
      "geometry_type": "Polygon",
      "geometry": "POLYGON Z ((77 28 0, 77.01 28 0, 77.01 28.01 0, 77 28.01 0, 77 28 0))",
      "geometry_geojson": {
        "type": "Polygon",
        "coordinates": [[[77.0, 28.0], [77.01, 28.0], [77.01, 28.01], [77.0, 28.01], [77.0, 28.0]]]
      },
      "crs": "EPSG:4326",
      "properties": { "Name": "Poly", "Description": "" },
      "measurement": { "type": "area", "value": 1090165.1617245723, "unit": "m²", "projected_crs": "EPSG:32643" }
    },
    {
      "feature_id": 1,
      "geometry_type": "LineString",
      "geometry": "LINESTRING (77 28, 77.01 28)",
      "geometry_geojson": { "type": "LineString", "coordinates": [[77.0, 28.0], [77.01, 28.0]] },
      "crs": "EPSG:4326",
      "properties": { "Name": "Line", "Description": "" },
      "measurement": { "type": "length", "value": 983.6971724179657, "unit": "m", "projected_crs": "EPSG:32643" }
    }
  ]
}
```

- `feature_id` is the zero-based index of the feature within the file (numbered consecutively if a ZIP contains several Shapefiles).
- `geometry` is WKT in the **original** CRS (`crs`), unchanged from the file. `geometry_geojson` is the same geometry
  **reprojected to WGS84** as 2D GeoJSON (Z dropped, 6 decimal places ≈ 0.1 m) — what web maps need. It is `null` only
  for features without usable geometry.
- `measurement.projected_crs` is the UTM CRS actually used for the measurement.
- Points (and any unsupported geometry) return `"measurement": {"type": null, "value": null, "unit": null, "projected_crs": null}`.

### `GET /health`

Returns `{"status": "ok"}` when the database is reachable (used by Docker and Render). If it is not, it returns
**503** `{"detail": "Database unavailable", "code": "DB_UNAVAILABLE"}`.

### Errors

Every error uses the same body: `{"detail": "<human-readable message>", "code": "<MACHINE_CODE>"}`.

| Status | `code` | When |
|---|---|---|
| 400 | `INVALID_FILE_TYPE` | Extension not `.zip`/`.kml`, or content doesn't match the extension |
| 400 | `EMPTY_FILE` | Zero-byte upload |
| 404 | `FILE_NOT_FOUND` | Unknown file id |
| 409 | `FILE_NOT_READY` | Measurements requested while `PENDING`/`PROCESSING` |
| 413 | `FILE_TOO_LARGE` | Exceeds `MAX_UPLOAD_SIZE_MB` |
| 422 | `CORRUPT_FILE` | Not a valid ZIP / no `.shp` inside / not KML — detected at upload |
| 422 | `FILE_PROCESSING_FAILED` | Measurements requested for a file whose background parse failed (`detail` has the reason) |
| 422 | `VALIDATION_ERROR` | Bad request params (e.g. malformed UUID, `page=0`, missing `file` field) |
| 503 | `DB_UNAVAILABLE` | `/health` only: database unreachable |
| 500 | `INTERNAL_ERROR` | Anything unexpected — logged server-side, never leaked to the client |

```bash
curl -s -F "file=@notes.txt" http://localhost:8000/api/files/
# {"detail":"Invalid file type. Upload a .zip (Shapefile) or a .kml file.","code":"INVALID_FILE_TYPE"}
```

---

## Architecture

### Layout

```
app/
├── main.py                  # app factory, lifespan (create tables, fail interrupted jobs), CORS, request-id/access-log middleware, /health, frontend mount
├── api/routes/files.py      # thin HTTP layer: validate params → call service → shape response
├── services/
│   ├── file_service.py      # orchestration: store upload, run pipeline, DB queries
│   ├── file_parser.py       # ZIP/KML → plain feature records (safe extraction, JSON-safe properties)
│   ├── crs_handler.py       # CRS labels, UTM zone detection, reprojection
│   └── measurement.py       # area / length / graceful no-op, always via crs_handler
├── models/models.py         # UploadedFile, Feature (SQLAlchemy 2.0)
├── schemas/schemas.py       # Pydantic v2 request/response models
├── db/{base,session}.py     # declarative base, async engine + session dependency
└── core/{config,logging,exceptions,middleware,static}.py   # middleware.py: Content-Length upload guard; static.py: SPA files + index.html fallback

frontend/                    # React + TypeScript SPA (built into frontend/dist, served by the API at "/")
├── src/api/                 # typed fetch client (ApiError) and response interfaces
├── src/hooks/               # useFileUpload, useFileStatus, useFileList, useFileMeasurements, useMapFeatures
├── src/components/          # small single-purpose components (UploadZone, ResultsMap, MeasurementsTable, …)
└── src/utils/format.ts      # formatting, client-side file validation, geometry colours
```

Dependencies point one way: `routes → file_service → {file_parser, measurement → crs_handler}`. Route handlers
contain no business logic. The geospatial modules are pure and synchronous (no DB, no HTTP), so they're trivial to test.

### File-processing flow

```
POST /api/files/
   │  0. middleware: declared Content-Length over the limit → 413 immediately, body never read
   │  1. validate filename/extension, stream to disk with size cap, sniff magic bytes
   │  2. cheap structural check (valid ZIP w/ .shp, KML root element)  ──fail──▶ 400/413/422, file deleted
   │  3. INSERT uploaded_files (status=PENDING), commit
   │  4. schedule background task, return 202 {id, status}
   ▼
Background task: process_file(id)
   │  (at app startup, any job still PENDING/PROCESSING is marked FAILED — see "Startup recovery")
   │  status → PROCESSING (committed, visible to pollers)
   │  worker thread (asyncio.to_thread, keeps the event loop free):
   │     ZIP: safe-extract to temp dir (zip-slip / zip-bomb / encryption guards) → read each .shp with GeoPandas
   │     KML: read every layer with GeoPandas (Fiona KML driver); an unreadable layer is skipped and
   │          recorded as a warning (file still COMPLETED), never dropped silently
   │     per feature: index, geometry type, WKT, CRS, JSON-safe properties
   │     per source CRS (batch): bulk reproject → WGS84, group by UTM zone, measure   ← see below
   │     per batch: GeoJSON in WGS84 (2D, rounded) for map display
   │  bulk INSERT features (batches of 1000) + status=COMPLETED + feature_count + crs + warnings  — ONE transaction
   │  any failure → rollback, status=FAILED + safe error message (full traceback only in logs)
   ▼
GET /api/files/{id}/  and  /measurements/   (poll until COMPLETED)
```

### Measurement flow

```
geometry (original CRS)
   ├─ None / empty ................ warn, measurement = null
   ├─ Point / MultiPoint .......... measurement = null (by design)
   ├─ other types (GeometryCollection, …) ... warn, measurement = null
   └─ Polygon / MultiPolygon / LineString / MultiLineString
         1. reproject ALL geometries → WGS84 in one call; centroids + UTM zones via numpy   (crs_handler)
         2. GROUP features by target UTM CRS; one reprojection call per group                (crs_handler)
         3. polygon → .area (m²) · line → .length (m), vectorized per group                  (measurement)
         4. store value, unit, and projected CRS alongside the original CRS
```

Files are processed in bulk by `measure_geometries`: instead of two Python-level PROJ transforms *per feature*, every
geometry goes to WGS84 in one `GeoSeries.to_crs` call, and each UTM group goes to its zone in one more call (at most
~60 per source CRS). Each feature still gets its own projected geometry, zone and measurement; results are identical to
the single-feature `measure_geometry` (a test asserts this). On a 30,000-feature benchmark spread over many zones the
file-to-rows step went from 2.4 s to 0.5 s (≈ 5× faster, same output).

Neither function raises: a failure on one feature is logged and yields a null measurement, and if a whole zone group
fails the bulk path retries that group feature by feature, so one bad geometry can't fail its neighbours or the file.

### Data model

- `uploaded_files` — id (UUID), filename, stored_path, file_size, status, feature_count, crs, error_message,
  warnings (JSONB list), timestamps.
- `features` — file_id (FK, cascade), feature_index, geometry_type, geometry_wkt, geometry_geojson (**JSONB**, WGS84),
  crs, properties (**JSONB**), measurement_{type,value,unit,crs}. All CRS columns are `TEXT`: a CRS's WKT can be hundreds
  of characters, and a `VARCHAR(255)` would make PostgreSQL reject the insert (SQLite would not, so tests alone can't catch it). Unique index on `(file_id, feature_index)` serves the paginated query.

Measurements and GeoJSON are computed once at processing time and stored, so reads are cheap pagination queries.

### Startup recovery

Processing runs as in-process background tasks, so a job that is `PENDING` or `PROCESSING` when the service *starts* can
only be an orphan of the previous process. On startup **every** such job is marked `FAILED` with the reason
`"Service restarted before processing completed"` (there is no age threshold — a crash two minutes ago must be recovered
too). Clients polling that file see `FAILED` and the reason, and can re-upload. This assumes a **single running
instance**: with several instances, one starting would fail another's live jobs (a real queue is the fix — see future scope).

---

## CRS handling strategy

**Rule: area and length are never computed in degrees.** For every measurable feature:

1. Take the feature's centroid, expressed in WGS84 (transforming first if the file uses another CRS).
2. Pick the UTM zone: `zone = floor((lon + 180) / 6) + 1`; EPSG `326xx` for the northern hemisphere, `327xx` for the southern.
3. Reproject the geometry to that zone and measure there (units are metres).
4. Persist both the **original CRS** (`crs`) and the **projected CRS used** (`measurement.projected_crs`).

Details worth knowing:

- **Zone is chosen per feature**, not per file, so a file spanning several zones is still measured near each feature's own central meridian.
- **Already-projected inputs are still reprojected**, because a projected CRS may use feet (e.g. US State Plane) — measuring "as is" would silently return the wrong unit.
- **Polar regions:** UTM is undefined beyond 84°N / 80°S, so those features use UPS (EPSG:32661 / 32761).
- **Antimeridian:** longitude is wrapped, so `±180` maps to zone 1/60 correctly.
- **KML** is defined to be WGS84, so it's assumed `EPSG:4326` if the driver reports no CRS. **Shapefiles with no `.prj`** are rejected with a clear message rather than guessing a CRS — a wrong guess would produce plausible-looking but wrong numbers.
- Transformers are cached (`lru_cache`) since constructing them is the expensive part, and use `always_xy=True` to avoid axis-order bugs. The bulk path relies on GeoPandas, which also uses `always_xy=True`.

### Accuracy limits: features spanning several UTM zones

UTM is a **conformal** projection, not an **equal-area** one: it preserves local shape and angles, not area. Each zone
is only accurate close to its central meridian (scale error ≲ 0.1 % within the zone), and the area error grows as you move
away from it. Because every feature is measured in exactly **one** zone — the zone of its centroid — a feature that
extends across several zones is measured with a projection that is increasingly wrong towards its far edges.

- For ordinary survey-scale features (plots, roads, buildings, farms — well inside one 6°-wide zone) this is negligible.
- For **large polygons** such as state or country boundaries, area accuracy **degrades**, and the error **increases with the
  feature's geographic extent**. Line lengths are affected the same way. The API does not currently flag these features;
  the returned value looks as precise as any other.
- **Future improvement:** detect multi-zone features (bounds spanning more than one zone) and either switch them to a
  local equal-area projection (e.g. Lambert azimuthal equal-area centred on the feature) or compute **geodesic** area and
  length with `pyproj.Geod`, which has no projection distortion at all. Until then, treat measurements of very large
  features as approximate.

### CRS labelling

The stored/returned `crs` is never a guess. `crs_label()` reports `EPSG:<code>` only if PROJ identifies the CRS with
**100 % confidence** *and* `CRS.from_epsg(code) == crs`. Anything else — no match, a fuzzy match, or a definition that
merely resembles a registry entry — is labelled `CUSTOM:<first 100 characters of its WKT>`. (PROJ's default 70 %-confidence
matching would label a lookalike Transverse Mercator as `EPSG:32643`; that is exactly what this prevents.) Measurement
always uses the real CRS object, never the label, so a `CUSTOM` file is measured correctly.

---

## Frontend

A single-page React app (no router) with three states, driven by the file's live status:

1. **Upload** — drag-and-drop / click zone (`.zip` and `.kml` enforced client-side), file name + size, an Upload button, and
   a list of recent uploads (status badge, feature count, **View**) refreshed every 5 s.
2. **Processing** — spinner and status while `PENDING`/`PROCESSING`; polls every 3 s and switches to results automatically.
   `FAILED` shows the server's error message.
3. **Results** — left (40 %): file card, a yellow banner if the file has `warnings`, and a paginated (20/page) measurements
   table with collapsible properties. Right (60 %): a Leaflet map drawing each feature's `geometry_geojson`, coloured by type
   (Polygon blue, LineString orange, Point red), fitted to all features. Clicking a feature (map or table row) highlights it
   and shows its measurement in a popup; clicking on the map also jumps the table to that feature's page.

Notes: the map fetches features page by page (200 at a time) up to a cap of 5,000 and says so if a file is larger; the
table is server-paginated and has no such limit. Map tiles come from OpenStreetMap, so the map needs internet access.

### Run the frontend in dev mode (separately from the API)

```bash
# terminal 1 — API (Docker or local), on http://localhost:8000
docker compose up --build        # or: uvicorn app.main:app --reload

# terminal 2 — Vite dev server with hot reload, on http://localhost:5173
cd frontend
cp .env.example .env             # optional; VITE_API_BASE_URL defaults to http://localhost:8000 in dev
npm install
npm run dev
```

CORS allows this out of the box when `ENVIRONMENT=development` (`ALLOWED_ORIGINS` defaults to `*`). For any other
environment set `ALLOWED_ORIGINS=http://localhost:5173`.

Other commands: `npm run build` (type-check + production build into `frontend/dist`), `npm run lint`.
A production build with `VITE_API_BASE_URL` unset calls the API on the **same origin**, which is how the Docker image and
Render deployment work (no CORS needed). The code is TypeScript `strict` with no `any`; API calls live in typed hooks
(`useFileUpload`, `useFileStatus`, `useFileList`, `useFileMeasurements`, `useMapFeatures`) and every query/mutation error is
rendered, never thrown.

---

## Design decisions & alternatives

| Decision | Why | Alternatives considered |
|---|---|---|
| **FastAPI** (async) | Native async fits async SQLAlchemy; typed schemas give OpenAPI for free. | Django + DRF: heavier, sync-first; its ORM/GeoDjango would be a good fit if PostGIS were the goal. |
| **`BackgroundTasks` for processing** | Zero extra infrastructure; sufficient for this scope. Status is persisted, so clients poll. Jobs orphaned by a crash are marked `FAILED` on startup. | Celery/RQ/ARQ + Redis: right answer for scale/retries (see future scope) but adds a broker and worker to operate. |
| **Blocking geo work in `asyncio.to_thread`** | GeoPandas/GDAL/PyProj are synchronous and CPU-bound; running them inline would stall every request. | `ProcessPoolExecutor` for true parallelism (GIL is largely released in GDAL/GEOS, so a thread is enough for now). |
| **UTM auto-detection** | One global rule, metre units, scale error of at most ~0.1 % inside a zone — accurate for survey-scale features. | Local equal-area CRS (e.g. LAEA at centroid): better for area, but zone-less CRSs are harder to reason about/report. Geodesic calculation (`pyproj.Geod`): most accurate and projection-free, but the brief asks for projection-based measurement, and it makes area of invalid/complex polygons trickier. |
| **Store results in Postgres (JSONB properties)** | Compute once, serve many paginated reads; attributes are schemaless across files. | Recompute on every GET (wasteful); PostGIS geometry columns (enables spatial queries, but not required by the brief and complicates local setup — see future scope). |
| **Geometry stored as WKT text + GeoJSON (WGS84)** | WKT is the faithful original; GeoJSON is precomputed once at ingest so reads never re-project and the map gets what it needs. Costs extra storage per feature. | Convert on every read (CPU per request); WKB / PostGIS `geometry` (see future scope). |
| **Fail all in-flight jobs on startup** | Correct for a single instance, simple, and recovers crashes immediately; an age threshold would leave a job that died 2 minutes ago `PROCESSING` forever. | Heartbeat/lease per job (right for multiple instances). |
| **Partial KML read ⇒ `COMPLETED` + `warnings`** | A multi-folder KML with one broken folder is still mostly useful; the user is told exactly what is missing instead of getting a silently smaller file. | Fail the whole file (loses good data); skip silently (hides data loss). |
| **Frontend served by FastAPI at `/`** | One deployable, same origin (no CORS in production), one Render service. | Separate static host/CDN (better caching, needs CORS + two deploys). |
| **Map loads all features, table is server-paginated** | The map must show every feature and fit to them; the table stays fast for any size. | Client-side pagination (simpler but loads everything up front); vector tiles for very large files. |
| **Whole file processed in one transaction** | A file is never observed half-ingested; failure leaves no partial rows. | Streaming per-feature commits: better memory profile for huge files, worse atomicity. |
| **`create_all` on startup** | Simplest for a two-table service. | Alembic migrations — needed as soon as the schema evolves (future scope). |
| **`202 Accepted` on upload** | Accurately signals "accepted, not yet processed". | `201 Created` (resource exists, but the result doesn't yet). |
| **Cheap validation at upload, full parse in background** | Obvious bad input gets an immediate 4xx; the expensive parse never blocks a request. Deep parse failures surface as `FAILED` status + `422` on the measurements endpoint. | Full synchronous parse: simplest contract, but slow requests and timeouts on big files. |
| **Settings via `os.environ` + `python-dotenv`** | As specified; a frozen dataclass is typed and fails fast on bad values. | `pydantic-settings` (equivalent, extra dependency). |
| **Tests on SQLite, app on Postgres** | Fast, zero-setup test runs; the JSON column falls back from JSONB. | Testcontainers/Postgres in CI for full fidelity (listed under future scope). |

**Security hardening included:** server-generated storage names (client filename never touches the filesystem path),
zip-slip rejection, uncompressed-size and member-count limits (enforced on actual bytes written, since headers can lie),
encrypted-ZIP rejection, upload size cap enforced up front from `Content-Length` (and while streaming as a fallback), magic-byte sniffing, non-root container user.

**Known limitations:** processing uses in-process tasks, so run exactly **one** Uvicorn worker and one instance (as the
Dockerfile does). An in-flight job is lost if the process dies and is marked `FAILED` on the next start; multi-worker or
multi-instance deployments need a real queue (and would otherwise fail each other's jobs at startup). Very large files are
loaded into memory by GeoPandas. Area is measured in a single UTM zone per feature, so very large multi-zone features are
less accurate (see [Accuracy limits](#accuracy-limits-features-spanning-several-utm-zones)). There is no authentication;
anyone who can reach the API can upload. The `Content-Length` guard cannot see the size of chunked uploads, which are
streamed until the limit is hit; also enforce a body-size limit at your reverse proxy in production.

**Uploaded files are not cleaned up.** Uploaded files (ZIP and KML) are stored on disk permanently after processing.
The parsed data lives in PostgreSQL, so the raw files are **not needed after processing — but they are not currently
deleted**, including the files of `FAILED` uploads. Disk usage therefore grows with every upload. On Render's free tier
the filesystem is ephemeral anyway, so files are lost on every redeploy or restart (which is harmless here, since the
results are in Postgres); on a persistent disk or a local Docker volume they accumulate indefinitely.
**Future improvement:** delete each file immediately after successful processing, and run a periodic cleanup job that
removes the files of `FAILED` uploads after a configurable TTL.

---

## Error handling

- **Service layer owns exceptions.** Domain errors (`AppError` subclasses) carry an HTTP status and a stable `code`; handlers render them as `{"detail", "code"}`.
- **Catch-all handler** converts any unexpected exception to a generic `500 INTERNAL_ERROR` and logs the traceback; clients never see one.
- **Background task never raises**: it catches everything and records `FAILED` with a safe message.
- **Per-feature isolation**: unsupported/empty/unprojectable geometries are logged and returned with a `null` measurement.
- **JSON safety**: attribute values from pandas/numpy (`NaN`, `NaT`, `numpy` scalars, timestamps, bytes) are coerced to valid JSON so they can't break the JSONB insert.
- **Observability**: structured (JSON) logs with a per-request `x-request-id`, plus `file_id`, `feature_index`, etc. as fields.

---

## Testing

```bash
pip install -r requirements-dev.txt
pytest
```

87 backend tests. `tests/test_files.py` covers UTM zone selection (hemispheres, antimeridian, polar), numerically verified
area/length (a 1 km × 1 km square must measure ≈ 1,000,000 m², not ~1e-4 "degrees²"), non-WGS84 sources, unsupported
geometries, and the full API: KML and Shapefile uploads, multi-Shapefile ZIPs, pagination, every error code, ZIP-slip,
missing `.prj`, and filename traversal. `tests/test_hardening.py` covers CRS labelling (exact vs lookalike vs custom, column
types), startup recovery, partial-KML warnings, `/health` 503, CORS preflight, the file listing, WGS84 GeoJSON (projected
sources, Z dropped), and SPA static serving (fallback never masks API 404s). `tests/test_middleware.py` proves the
`Content-Length` guard rejects without reading the body (its `receive` fails the test if called), passes chunked requests
through to the service-level fallback, and keeps CORS headers on the 413. `tests/test_bulk_measurement.py` asserts the bulk path
matches the per-feature path across zones, types and CRSs, makes exactly one projection call per UTM group, and isolates a
failing group or an unprojectable feature.

Tests run against SQLite (no Postgres needed), so they cannot catch PostgreSQL-specific problems such as column-length
limits (hence the explicit column-type test). The full stack was also verified by hand against PostgreSQL 16 via Docker
Compose, and the UI was exercised end-to-end in headless Chrome (upload, validation, polling transition, map, popups,
warnings banner). **There are no automated frontend tests yet** (see future scope).

---

## Deployment (Render)

1. Push the repo to GitHub.
2. In Render: **New → Blueprint**, select the repo. [`render.yaml`](render.yaml) provisions a Docker web service and a
   managed PostgreSQL, and wires `DATABASE_URL` between them. The Docker build compiles the frontend, so the web app and
   API are served from the same URL.
3. Health check path is `/health`.

Notes: the free plan has an **ephemeral filesystem**, which is fine here because results are stored in Postgres and the
raw upload is only needed during processing. To retain raw files across deploys, upgrade the plan and uncomment the
`disk:` block in `render.yaml`. Free Render Postgres instances expire after a limited period — use a paid plan for anything long-lived.

---

## Learnings & future scope

**Learnings**

- Reprojection is where geospatial code silently goes wrong: the bug isn't a crash, it's a plausible number in the wrong unit. Making "never measure in degrees" a structural property of the code (measurement *only* goes through `crs_handler`) is more robust than remembering to do it.
- Real-world files are messy: NaN attributes, missing `.prj`, Z coordinates in KML, multiple KML folders, mixed Shapefile layers, hostile ZIPs. Most of the production-hardening effort was in the parsing boundary, not the maths.
- Background work in an async server needs deliberate thought: blocking GDAL calls belong off the event loop, and "the process died mid-job" needs an explicit recovery story.
- Packaging matters: Fiona has no Linux arm64 wheels, which only showed up when building the image on Apple Silicon.

**Future scope**

- **Job queue** (ARQ/Celery + Redis) with retries, progress reporting, and horizontal scaling; webhook or SSE on completion instead of polling.
- **Alembic migrations** instead of `create_all`.
- **PostGIS**: store native geometries, add spatial queries (`bbox`, `intersects`), and a GeoJSON output option.
- **Geodesic measurements** (`pyproj.Geod`) as an optional method/cross-check, and a configurable target CRS per request.
- **More formats**: GeoJSON, GeoPackage, KMZ; formal handling of `LinearRing`/`GeometryCollection` (measure members) and perimeter for polygons.
- **Streaming/chunked ingestion** for very large files; object storage (S3) for uploads.
- **Upload file cleanup**: delete the stored ZIP/KML immediately after successful processing, and run a periodic cleanup job that removes files of `FAILED` uploads after a configurable TTL (e.g. `FAILED_UPLOAD_TTL_HOURS`).
- **Auth & rate limiting**, per-user file ownership.
- **CI**: lint (ruff), type-check (mypy), tests against Postgres via Testcontainers, image scanning.
- **Frontend tests** (Vitest + Testing Library, Playwright for the upload → results flow), and vector tiles / clustering for files with very many features.
- **Multi-zone area accuracy**: UTM is conformal, not equal-area, so large features spanning several zones are measured less accurately. Detect multi-zone features (and flag them in the response), then switch to a local equal-area projection or compute geodesic area/length with `pyproj.Geod`.
