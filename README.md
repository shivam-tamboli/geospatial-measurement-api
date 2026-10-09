# Geospatial Measurement API

This service takes a Shapefile (a `.zip`) or a KML file, reads every feature in it, and tells you the area of each polygon in square metres and the length of each line in metres. Measurements are always made after reprojecting to a metric CRS, never in degrees. It is a FastAPI backend with PostgreSQL, plus a small React frontend (upload, status, results table, map) that the same server serves.

Repository: <https://github.com/shivam-tamboli/geospatial-measurement-api>

## Live URL

<https://geospatial-measurement-api-zthu.onrender.com>

The web UI is at that address, and the interactive API documentation is at `/docs` on the same host. `render.yaml` configures it for Render's free plan, where a service spins down when idle, so the first request after a pause can be slow.

## Running it locally

### With Docker

```bash
docker compose up --build
```

That starts PostgreSQL 16 and the app, and opens everything on <http://localhost:8000>: the web UI at `/`, Swagger at `/docs`, a health probe at `/health`. The Dockerfile has two stages. Node builds the frontend, and the Python image copies the result in, so the app is a single container (next to the database) and there is nothing else to build. Postgres data and uploaded files live in named volumes; `docker compose down -v` deletes both.

Tables are created with `create_all` at startup, which never alters an existing table. If you pull a version that adds a column, recreate the volumes with `docker compose down -v` first.

### Without Docker

You need Python 3.12 and a running PostgreSQL. Node 22 is only needed if you want the UI.

1. Create the database and a virtualenv.
   ```bash
   createdb geospatial
   python3.12 -m venv .venv && source .venv/bin/activate
   pip install -r requirements-dev.txt     # requirements.txt if you don't need the tests
   ```
2. Copy the environment template and point `DATABASE_URL` at your database.
   ```bash
   cp .env.example .env
   ```
3. Start the API. Tables are created on startup.
   ```bash
   uvicorn app.main:app --reload
   ```
4. Optional, to serve the UI from the API: `cd frontend && npm ci && npm run build`. The API serves `frontend/dist` at `/` whenever that directory exists, and runs API-only when it doesn't.

To work on the frontend with hot reload, leave the API running on port 8000 and start Vite next to it:

```bash
cd frontend
npm ci
npm run dev        # http://localhost:5173, calls http://localhost:8000 by default
```

CORS is open (`*`) when `ENVIRONMENT=development`, so this works without configuration. In any other environment, set `ALLOWED_ORIGINS=http://localhost:5173`.

### Tests

```bash
pytest                      # 157 backend tests, no Postgres needed
cd frontend && npm run build && npm run lint
```

The backend tests run on SQLite, which is fast but means they cannot catch Postgres-specific problems. I found that out the hard way; see "What I learned". There are no automated frontend tests.

## Configuration

Everything comes from environment variables, loaded from `.env` by python-dotenv. Only `DATABASE_URL` is required. `postgres://` and `postgresql://` URLs are rewritten to the asyncpg driver, because that is what Render hands out.

- `DATABASE_URL`: the PostgreSQL connection string.
- `APP_NAME` (default `Geospatial Measurement API`): the title shown in the Swagger UI.
- `ENVIRONMENT` (default `development`): `development` turns on permissive CORS by default.
- `LOG_LEVEL` (default `INFO`) and `LOG_FORMAT` (default `json`; use `console` for readable local output).
- `DB_POOL_SIZE` (default 5) and `DB_MAX_OVERFLOW` (default 10): connection pool sizing.
- `UPLOAD_DIR` (default `uploads`): where raw uploads are stored, under a UUID filename.
- `MAX_UPLOAD_SIZE_MB` (default 50): the upload limit. A request whose `Content-Length` is over it (plus a 64 KB allowance for multipart framing) is refused before the body is read; chunked requests are checked while streaming.
- `MAX_EXTRACTED_SIZE_MB` (default 500) and `MAX_ZIP_MEMBERS` (default 200): zip-bomb limits applied while extracting a Shapefile archive.
- `DEFAULT_PAGE_SIZE` (default 20) and `MAX_PAGE_SIZE` (default 100): pagination for measurements. A `page_size` above the maximum is rejected with `400`, not capped.
- `PROCESSING_BATCH_SIZE` (default 50): how many features are measured and written to the database at a time. It bounds the memory the measuring step adds; see "Background tasks".
- `LIST_DEFAULT_PAGE_SIZE` (default 20) and `LIST_MAX_PAGE_SIZE` (default 100): pagination for the file listing.
- `ALLOWED_ORIGINS`: comma-separated CORS origins. If unset, it is `*` in development and CORS is off otherwise.
- `FRONTEND_DIST_DIR` (default `frontend/dist`): the built frontend to serve at `/`.
- `PORT`: read by the Docker command; Render sets it. Defaults to 8000.
- `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`: used only by `docker-compose.yml` for the database container. The defaults are `postgres`, `postgres` and `geospatial`, so change them for anything shared.
- `VITE_API_BASE_URL`: read at frontend build time. Unset means `http://localhost:8000` under `npm run dev` and the same origin in a production build.

## API

All endpoints are under `/api/files/`. The trailing slashes matter. Without one the request matches no route. Any instance that serves the frontend (the Docker image and the Render deployment) answers a GET with `404 NOT_FOUND` and a POST with `405 METHOD_NOT_ALLOWED`; only an API-only instance run without `frontend/dist` answers with a 307 redirect. I did not add a redirect, because some HTTP clients drop the body of a POST when they follow one. The examples below are captured from a running instance.

Every error, from any endpoint, has the same shape: `{"detail": "...", "code": "..."}`. `detail` is for people; branch on `code`.

### POST /api/files/

Uploads a file. The request is `multipart/form-data` with one field, `file`, which is either a `.zip` containing a Shapefile or a `.kml`. The response is `202 Accepted`, not 200, because nothing has been measured yet; parsing happens in the background.

```bash
curl -F "file=@survey.kml" http://localhost:8000/api/files/
```
```json
{"id": "8e4bcba8-dfcc-4609-b116-718620bb14b3", "filename": "survey.kml", "status": "PENDING"}
```

Problems that can be seen without a full parse are rejected here: `400 INVALID_FILE_TYPE` (wrong extension, or content that doesn't match it), `400 EMPTY_FILE`, `413 FILE_TOO_LARGE`, and `422 CORRUPT_FILE` (not a valid ZIP, no `.shp` inside, no `<kml>` element). Anything that needs a real parse fails later, as a `FAILED` status.

### GET /api/files/{id}/

Returns the file's metadata and status. This is the endpoint to poll after an upload.

```bash
curl http://localhost:8000/api/files/8e4bcba8-dfcc-4609-b116-718620bb14b3/
```
```json
{
  "id": "8e4bcba8-dfcc-4609-b116-718620bb14b3",
  "filename": "survey.kml",
  "feature_count": 3,
  "crs": "EPSG:4326",
  "status": "COMPLETED",
  "error": null,
  "warnings": [],
  "file_size": 557,
  "created_at": "2026-10-08T23:25:24.191828Z",
  "updated_at": "2026-10-08T23:25:24Z"
}
```

`status` moves `PENDING`, `PROCESSING`, then `COMPLETED` or `FAILED`. `feature_count` and `crs` are null until it completes. A failed file explains itself:

```json
{"status": "FAILED", "feature_count": null, "crs": null, "error": "'sf.shp' does not declare a coordinate reference system (missing .prj). Include the .prj file in the ZIP.", "warnings": []}
```

`warnings` is for partial reads. If one folder of a multi-folder KML can't be parsed, the file still completes, and the skipped folder is named here, for example `["KML layer 'Roads' could not be read and was skipped; its features are missing."]`. A KML point whose coordinates are empty or unparseable is kept as a feature with a null geometry, and the file gets a warning that says how many: `["2 features had empty or unparseable coordinates; the geometry of each was set to null."]`. GDAL would otherwise turn such a point into `POINT (0 0)` without saying anything. A geometry that can't be built at all, such as a LineString with a single point, is treated the same way: the feature is kept with a null geometry and a warning names it, for example `["Feature 3: LineString has fewer than 2 points — skipped (geometry set to null)"]`, and the rest of the layer still processes. Those per-feature geometry warnings are capped at 10 per file; anything beyond that is summarised in one line (`"...and 15 more features had geometries that could not be built."`). An unknown id is `404 FILE_NOT_FOUND`; a malformed id is `422 VALIDATION_ERROR`.

### GET /api/files/

Lists uploads, newest first. Query parameters: `page` (default 1) and `page_size` (default 20, maximum 100).

```bash
curl "http://localhost:8000/api/files/?page=1&page_size=2"
```
```json
{
  "page": 1,
  "page_size": 2,
  "total": 1,
  "total_pages": 1,
  "items": [
    {
      "id": "8e4bcba8-dfcc-4609-b116-718620bb14b3",
      "filename": "survey.kml",
      "status": "COMPLETED",
      "feature_count": 3,
      "crs": "EPSG:4326",
      "created_at": "2026-10-08T23:25:24.191828Z"
    }
  ]
}
```

### GET /api/files/{id}/measurements/

Returns the features of a completed file, ordered by `feature_id`, one page at a time. Query parameters: `page` (default 1) and `page_size` (default 20, maximum 100). A larger `page_size` is rejected with `400 PAGE_SIZE_TOO_LARGE` rather than silently capped, so a client never receives fewer rows than it asked for without knowing.

```bash
curl "http://localhost:8000/api/files/8e4bcba8-dfcc-4609-b116-718620bb14b3/measurements/?page_size=3"
```
```json
{
  "file_id": "8e4bcba8-dfcc-4609-b116-718620bb14b3",
  "page": 1,
  "page_size": 3,
  "total": 3,
  "total_pages": 1,
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
      "properties": {"Name": "Poly", "Description": ""},
      "measurement": {"type": "area", "value": 1090165.1617245723, "unit": "m²", "projected_crs": "EPSG:32643"}
    },
    {
      "feature_id": 1,
      "geometry_type": "LineString",
      "geometry": "LINESTRING (77 28, 77.01 28)",
      "geometry_geojson": {"type": "LineString", "coordinates": [[77.0, 28.0], [77.01, 28.0]]},
      "crs": "EPSG:4326",
      "properties": {"Name": "Line", "Description": ""},
      "measurement": {"type": "length", "value": 983.6971724179657, "unit": "m", "projected_crs": "EPSG:32643"}
    },
    {
      "feature_id": 2,
      "geometry_type": "Point",
      "geometry": "POINT (77 28)",
      "geometry_geojson": {"type": "Point", "coordinates": [77.0, 28.0]},
      "crs": "EPSG:4326",
      "properties": {"Name": "Pt", "Description": ""},
      "measurement": {"type": null, "value": null, "unit": null, "projected_crs": null}
    }
  ]
}
```

`geometry` is the WKT exactly as read from the file, in the file's own CRS. `geometry_geojson` is the same geometry reprojected to WGS84 as 2D GeoJSON, rounded to six decimals (about 10 cm), because a web map needs lon/lat and the original CRS could be anything. It is null only when the feature has no geometry or it could not be reprojected to valid WGS84 coordinates. `measurement.projected_crs` records which UTM zone the value was computed in. A point, or a geometry type I don't measure, gets a measurement whose fields are all null rather than an error.

Calling this before processing finishes is `409 FILE_NOT_READY`. For a failed file it is `422 FILE_PROCESSING_FAILED`, with the reason in `detail`.

### GET /health

Runs `SELECT 1`. It returns `{"status": "ok"}`, or `503` with `{"detail": "Database unavailable", "code": "DB_UNAVAILABLE"}` when the database can't be reached. I made it 503 rather than 500 because that is what Render's health check and load balancers treat as "not ready".

A Postman collection is included at postman_collection.json — import it, set base_url if needed, and all endpoints are ready to test.

## Architecture

### Structure

The rule I held to is that route handlers contain no logic. `app/api/routes/files.py` declares parameters and OpenAPI metadata and calls into `app/services/file_service.py`; the only thing a route does besides that is hand the upload's id to `BackgroundTasks`. `file_service` is the only place that touches both the database and the geospatial code. It stores uploads, runs the background job, and builds the response schemas.

The geospatial code sits below it in three modules that never touch the database or a request object, which is what makes them easy to test with plain Shapely objects. (`file_parser` does raise the shared error classes, which carry an HTTP status; that is the one place the layers meet.) `file_parser.py` turns a ZIP or KML into feature records. `crs_handler.py` owns every CRS decision: labels, UTM zone selection, reprojection. `measurement.py` computes areas and lengths and only ever gets geometries through `crs_handler`, so there is no code path that measures in degrees by accident.

Cross-cutting pieces live in `app/core/`: settings (`config.py`), JSON logging with a per-request id (`logging.py`), the error classes and handlers that produce the `{"detail", "code"}` shape (`exceptions.py`), the Content-Length guard (`middleware.py`), and the static-file mount that serves the frontend (`static.py`). The frontend is in `frontend/`: a typed fetch client, one hook per API call, and small components.

### From upload to stored measurements

```
POST /api/files/
  Content-Length over the limit?  -> 413, body never read
  validate name + magic bytes; stream to uploads/<uuid>.<ext> under the size cap
  cheap structure check (ZIP has a .shp / KML has <kml>)  -> 400/413/422, file deleted
  INSERT uploaded_files (PENDING); commit; schedule background task; return 202

background task process_file(id)            (runs after the response is sent)
  status -> PROCESSING (own commit, so pollers see it)
  in a worker thread:
    ZIP: extract safely, read each .shp  |  KML: read each layer
    in batches of PROCESSING_BATCH_SIZE (default 50): group features by source CRS
      one call: all geometries -> WGS84; centroids and UTM zones in numpy
      group by UTM zone; one reprojection call per group; vectorised area/length
      build GeoJSON from the WGS84 geometries
  one transaction: INSERT features (batches of 1000) + status COMPLETED + counts + warnings
  any exception: rollback, status FAILED + a user-safe message (traceback only in logs)
```

### Background tasks

Processing uses FastAPI's `BackgroundTasks`, which runs the job in the same process after the response is sent. The job opens its own database session, because the request's session is gone by then. GeoPandas, GDAL and PROJ are synchronous and CPU-heavy, so the parse and measure step runs in `asyncio.to_thread`; run inline, it would freeze every other request for the length of a big file.

Measuring and storing are done in batches of `PROCESSING_BATCH_SIZE` features (50 by default). Each batch is reprojected, measured, inserted, and dropped before the next is built, inside the one transaction that is committed together with the `COMPLETED` status. I added this after measuring a 20,000-polygon file: building every row at once pushed the process to 913 MB, while batching kept it at 321 MB, which is just the cost of parsing. With a 512 MB container limit the batched run completed (about 253 MiB sampled) and the all-at-once run was killed and restarted. The trade-off is speed on files scattered over many UTM zones, because features are only grouped by zone within a batch: on 30,000 features spread over about 50 zones, batch 50 ran at 18,700 features/s against 51,000 for one big batch, while batch 500 matched it (47,000/s) with the same memory profile. If throughput matters more than headroom, raise `PROCESSING_BATCH_SIZE` to 500.

The job never raises. Domain errors become a `FAILED` status with their own message; anything unexpected is logged with a traceback and stored as a generic message, so a client never sees an internal error. The features and the `COMPLETED` status are written in one transaction, which means a poller can't see a half-ingested file.

The cost of in-process tasks is that a job dies with the process. To keep that from leaving a file in `PROCESSING` forever, startup marks every `PENDING` or `PROCESSING` file as `FAILED` with "Service restarted before processing completed". There is no age threshold, since any such job at startup can only belong to the previous process. That is only correct with one running instance; see the limitations.

### How a geometry is measured

`measure_geometries` in `measurement.py` decides per feature, by Shapely geometry type:

- `Polygon` and `MultiPolygon` get an **area** in m². Holes are subtracted and the parts of a multipolygon are summed.
- `LineString` and `MultiLineString` get a **length** in m.
- `Point` and `MultiPoint` get no measurement, silently, because that is expected. The measurement object is returned with every field null.
- Anything else (`GeometryCollection`, `LinearRing`), and any feature with a missing or empty geometry, also gets null, but it is logged as a warning with the feature index and type, so you can find it.
- A feature that can't be measured for another reason (it can't be reprojected, or the result isn't a finite number) gets null and an error log. The rest of the file is unaffected.
- An invalid polygon, for example a self-intersecting ring, is still measured, with a warning in the log.

For the features that are measured, the steps are: reproject every geometry to WGS84 in one call, take each centroid and work out its UTM zone, group the features by zone, reproject each group from its original CRS into that zone in one call, then read `area` or `length` off the projected geometries. The stored measurement is the type (`area` or `length`), the value, the unit and the UTM CRS that was used, next to the feature's original CRS. On the 30,000-feature benchmark described below, this took 0.49 s, against 2.43 s for the first version that reprojected feature by feature.

## CRS handling

Geographic coordinates are angles. A degree of longitude is about 111 km at the equator and shrinks to nothing at the poles, so a polygon's area in "square degrees" is not a measurement of anything. Measuring means projecting first, so that coordinates are in metres on a flat plane.

For each polygon or line I take the centroid of the feature in WGS84, work out its UTM zone (`floor((lon + 180) / 6) + 1`, EPSG 326xx in the northern hemisphere and 327xx in the southern), reproject the geometry to that zone, and measure there. Both CRSs are stored: `crs` is the original, `measurement.projected_crs` is the zone used. Beyond 84°N and 80°S, UTM isn't defined, so those features use the UPS polar projections (EPSG:32661 and 32761). Longitudes are wrapped, so ±180° land in zones 1 and 60 as they should.

Zones are chosen per feature, not per file, so a file that spans several zones is measured near each feature's own central meridian. I reproject even when the file is already projected, because a projected CRS can be in feet (US State Plane, for example) and measuring "as is" would give the wrong unit with no error. A Shapefile with no `.prj` is accepted at upload but ends as `FAILED` with a message asking for the `.prj`, rather than getting an assumed CRS: a wrong guess produces plausible numbers that are simply wrong. The cost is that a genuinely WGS84 Shapefile that someone exported without its `.prj` is refused too. KML is defined to be WGS84, so that is assumed when the driver reports nothing.

The `crs` label is never a guess. It is `EPSG:<code>` only when PROJ identifies the CRS with 100% confidence and the registry definition equals the file's CRS. Otherwise it is `CUSTOM:` followed by the first 100 characters of the CRS's WKT. A `CUSTOM:` file is still measured correctly, because measurement uses the CRS object, not the label. PROJ's default behaviour is to accept a 70% match: a Transverse Mercator I wrote by hand, with a central meridian of 75° and the UTM scale factor, came back as `EPSG:32643`, which it is not. The label is what users see, so I would rather say "custom" than name the wrong CRS.

The limitation, which I would rather state than hide: UTM preserves shape, not area. Each zone is accurate close to its central meridian, with scale error up to about 0.1% inside the zone, and the error grows with distance from it. Every feature is measured in the single zone of its centroid, so a feature that spans several zones (a country, a large state) is measured with a projection that gets worse toward its edges, and the API doesn't flag it. For plots, roads and buildings this is negligible. For continental polygons it is not.

## Design decisions

**FastAPI instead of Django with DRF.** The assignment allowed either. I picked FastAPI because the whole request path can be async end to end, with async SQLAlchemy and asyncpg, and because Pydantic models give me the OpenAPI document for free. Django would have been the better choice if I had wanted GeoDjango and PostGIS, but I didn't need spatial queries, only measurements.

**`BackgroundTasks` instead of Celery or ARQ.** A queue is the right answer once you need retries, several workers or progress reporting. It also means running Redis and a worker process, and for a service this size that operational weight buys nothing yet. The price is the single-instance restriction above, which I accepted and wrote down. The status field is persisted, so moving to a queue later changes how the job is started, not the API.

**UTM per feature instead of one projection per file, an equal-area projection, or geodesic math.** One projection per file is wrong as soon as a file spans zones. An equal-area projection such as Lambert azimuthal centred on each feature would give better areas for large features, but it means building a custom CRS per feature, and the result is harder to explain and to store as a label. `pyproj.Geod` would be the most accurate and avoids projection entirely, but the assignment asked for a projected CRS, and geodesic area on complex or invalid polygons has its own edge cases. UTM is the standard answer at survey scale, and I documented where it stops working.

**Compute once at ingest and store, instead of computing on read.** Measurements, WKT and the WGS84 GeoJSON are all computed during processing and stored, so the read endpoints are plain paginated queries. The cost is extra storage, since every feature carries two copies of its geometry. I didn't use PostGIS geometry columns, because nothing here needs a spatial query, and PostGIS would complicate every local setup.

**Grouping features by UTM zone and reprojecting each group in one call.** My first version transformed each feature on its own, twice: once to WGS84 to find its zone, then again into UTM. The current version reprojects every geometry to WGS84 in one GeoPandas call, computes all the centroids and zones with numpy, groups features by zone, and does one reprojection per group. Results are identical, and a test asserts that, plus a spy that asserts the number of PROJ calls equals the number of zones. On 30,000 synthetic features spread over many zones, on my laptop, the file-to-rows step dropped from 2.43 s to 0.49 s; 100,000 features take 1.64 s. Those figures were measured with the whole file in one batch; the default batch of 50 is slower on data spread over many zones (see "Background tasks"). If a whole group fails, that group is retried feature by feature, so one bad geometry still can't take its neighbours down.

**Cheap validation at upload, real parsing in the background.** Wrong extension, mismatched content, empty file, oversize, and a ZIP with no `.shp` all fail immediately with a 4xx, so the common mistakes get fast feedback. Everything that needs GDAL happens in the background and surfaces as `FAILED`. The alternative, parsing synchronously, gives a simpler contract but ties up a request for however long a large file takes and invites proxy timeouts.

**A partly readable KML completes with warnings.** A KML with several folders where one is broken is still mostly useful. Failing the whole file throws away good data, and silently skipping the folder hides the loss, so I take the middle path: complete, and list exactly what was skipped in `warnings`. A Shapefile ZIP is different: an unreadable `.shp` fails the file, because there is no equivalent of "the rest of it".

**The frontend is served by the API.** One origin means no CORS in production and one service to deploy on Render. A separate static host would cache better, but needs CORS and two deployments for a project of this size. In development I run Vite separately and rely on the permissive development CORS setting.

**SQLite for the test suite.** It makes tests run in a second with nothing to install, and the JSON column falls back from JSONB. It also cost me a bug: see below. The right fix is running a subset against Postgres in CI, which I list under future scope.

## Known limitations

- Processing is in-process, so the service must run as one instance with one Uvicorn worker. Several instances would fail each other's live jobs at startup, and several workers would need a real queue. A job in flight when the process dies is lost and shows up as `FAILED`.
- Uploaded files are never deleted. The ZIP or KML stays on disk after processing, even though everything useful is in Postgres, and `FAILED` uploads leave theirs behind too. On Render's free tier the filesystem is wiped on every redeploy, which hides the problem there. Anywhere with a persistent disk, usage grows with every upload.
- Features that span several UTM zones are measured less accurately, as described above. Nothing in the response says so.
- There is no authentication or rate limiting. Anyone who can reach the API can upload.
- The Content-Length guard can't see the size of chunked uploads. Those are streamed to disk until the limit is hit, then cleaned up. In production I would also set a body-size limit at the proxy.
- Parsing reads the whole file into memory (GeoPandas builds every geometry up front), so very large files still need a lot of RAM: a 17.6 MB zip of 20,000 polygons with 101 vertices each peaked at about 320 MB during parsing alone. Only the measuring step is batched, so it adds almost nothing on top of that instead of adding another 560 MB.
- Self-intersecting (bow-tie) polygons return an incorrect area. Shapely does not validate geometry before measuring, and the service does not reject such polygons. Real-world files rarely contain them, but when one does the number is wrong and the only signal is a log line: a 0.01° bow-tie measured 2.6 m² where its true area is about 540,000 m².
- Coordinates outside valid ranges (longitude beyond ±180°, latitude beyond ±90°) are accepted. They may produce a null measurement or GeoJSON with invalid coordinates, and nothing in the response says so. Validation at the geometry level is not implemented.
- Polygons that cross the antimeridian (±180° longitude) are not split in the GeoJSON output, so map libraries may render them incorrectly.
- `POST /api/files` without a trailing slash returns 405. The correct path is `POST /api/files/`.
- Schema changes need a manual database reset, because there are no migrations.
- A ZIP containing Shapefiles in different CRSs reports a comma-joined string in the file-level `crs`. Each feature still has its correct `crs`.
- KMZ and UTF-16 KML are not supported, and the Norway/Svalbard UTM zone exceptions are ignored.
- The map draws at most 5,000 features and says so when a file has more. The table is paginated server-side and has no such limit.
- Map tiles come from OpenStreetMap, so the map needs internet access.
- On Render's free tier the service idles out, and the free Postgres instance expires after a fixed period, so it is not suitable for anything you need to keep.

## Future scope

- Replace `BackgroundTasks` with ARQ and Redis: retries, a heartbeat per job instead of "fail everything at startup", progress reporting, and a webhook or server-sent event to replace polling.
- Delete the stored upload right after successful processing, and add a periodic job that removes the files of `FAILED` uploads after a configurable TTL, something like `FAILED_UPLOAD_TTL_HOURS`.
- Detect features whose bounds cross a UTM zone boundary and flag them in the response. For those, compute area and length with `pyproj.Geod` or a local equal-area projection.
- Add Alembic migrations in place of `create_all`.
- Run the test suite against Postgres in CI with Testcontainers, so column-length and JSONB problems are caught before deploy.
- Add Playwright tests for upload, polling and the map, and Vitest for the hooks.
- Add PostGIS, so I can answer bounding-box and intersects queries, and a GeoJSON export endpoint.
- Support KMZ and GeoPackage, and report invalid polygons as a field in the measurement instead of a log line.
- Move uploads to object storage, and add authentication with per-user file ownership.
- Serve very large files to the map as vector tiles or clustered points rather than raw GeoJSON.

## What I learned

The most useful lesson was how much a passing test suite can hide. My tests ran on SQLite, which ignores `VARCHAR(255)`. A Shapefile with a custom projection produces a CRS label of several hundred characters, which PostgreSQL would have rejected, turning a valid upload into a `FAILED` file with a generic error. Every test was green. I found it by reviewing the code rather than by running it, changed the columns to `TEXT`, then confirmed on a real Postgres container that the same file now completes. I also added a test that asserts the column types directly, since a test of behaviour can't see this class of bug on SQLite.

The same review caught the CRS label problem. I had trusted `crs.to_epsg()`, whose default accepts a 70% match, so a lookalike projection was being reported as a registered EPSG code. It never raised an error and the measurements were still right; only the label lied. That one only shows up if you go looking for it.

My first startup recovery only failed jobs older than 30 minutes. A job that died two minutes before a restart would stay `PROCESSING` forever, and my README claimed otherwise. The age threshold made sense in my head and was wrong for a single instance.

I learned that Starlette reads and spools the entire multipart body before your handler runs, so a size check inside the handler is too late: a 5 GB upload has already consumed the disk. The check has to look at the `Content-Length` header in middleware. I tested that by giving the middleware a `receive` function that fails the test if it is called, and then with curl against a real server: a 300 MB upload against a 1 MB limit was refused in 2 ms with no bytes sent.

Packaging produced two surprises. Fiona publishes no Linux arm64 wheels, so my Docker build failed on an Apple Silicon laptop and needed GDAL compiled from source there. Then the first Render deploy failed with `ImportError: libexpat.so.1`: the x86_64 wheel bundles GDAL, but `python:3.12-slim` doesn't ship libexpat, and my Dockerfile only ran `apt-get` on the arm64 path. Running `ldd` over the wheel's shared objects showed the full picture: the bundled libraries have hashed names, and `libexpat` is the single unhashed one it expects from the system. I reproduced the failure in a `linux/amd64` container before changing the Dockerfile, and found that nothing else was missing.

Two performance facts are worth keeping. `pyproj.CRS.__hash__` is `hash(self.to_wkt())` (I read the source), so using a CRS as a dictionary key or `lru_cache` argument once per feature serialises it to WKT every time; I now group by object identity and compute the label once per CRS. And the 5x speed-up in the benchmark above came from two changes together: one PROJ call per UTM zone instead of two Python-level transforms per feature, and no per-feature CRS handling.

Smaller things I would now do from the start: SQLite timestamps default to one-second resolution, so files uploaded in the same second tied on `created_at` and came back in random order, and I now set the timestamp in Python. A Shapefile can only hold one geometry type, which my first test fixture tried to violate. And Tailwind's reset constrains `<img>` width, which quietly breaks Leaflet's map tiles until you override it.
