# Skill Lab Top-50 Leaderboard (backend)

A small Python service that:

1. imports the Google Form export (`.xlsx`) of students and their **Google Skills** public profile links,
2. scrapes each public profile once a day (server-rendered HTML, no JavaScript),
3. scores students, or teams, on their badges using rules in `scoring.yaml`, and
4. serves the top 50 over a REST API.

Stack: FastAPI, SQLAlchemy 2 (SQLite by default, MySQL via `DATABASE_URL`), httpx, BeautifulSoup + lxml, APScheduler, openpyxl, PyYAML and pytest.

> **Student data must never be committed.** `.gitignore` excludes `data/`, `.env`, `*.xlsx` and `*.csv`.

---

## Setup

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env             # then edit ADMIN_TOKEN, CONTACT_EMAIL, ...
pytest                           # network is fully mocked
```

## Workflow: import → refresh → serve

```bash
# 1. Import the form export (repeat whenever new responses come in; it is idempotent)
python -m app.cli import "data/Skill Lab (Responses).xlsx" --teams data/teams.csv

# 2. Scrape every profile once, score, validate and publish
python -m app.cli refresh

# 3. Look at the result / chase broken links
python -m app.cli leaderboard --limit 50
python -m app.cli link-report -o data/link-report.csv

# 4. Serve the API (the scheduler refreshes daily at REFRESH_CRON)
python -m app.cli serve --port 8000
```

Debug a single profile without touching the database:

```bash
python -m app.cli scrape-one https://www.skills.google/public_profiles/<uuid>
```

### Trying it offline with synthetic data

```bash
python scripts/generate_sample_form.py data/sample_form.xlsx --teams data/sample_teams.csv
python -m app.cli import data/sample_form.xlsx
python scripts/mock_refresh.py      # refresh against a mocked site, then prints /api/leaderboard
```

## What the importer does

- Finds the sheet named **Form responses 1** (falls back to the first sheet). It matches headers case-insensitively by keyword, so trailing spaces and newlines don't matter.
- **USN** is the student key. It is trimmed, stripped of inner whitespace and uppercased. When a USN appears twice, the row with the latest `Timestamp` wins. If the two submissions used different profiles, the import report lists it as a *USN conflict*.
- **Year** values such as `2.0` are stored as `"2"`. An email without `@` is stored as typed and flagged `invalid_email`. A "No" registration answer is flagged `not_registered` but still processed.
- **Profile URL** values:
  - `https://(www.)skills.google/public_profiles/<uuid>` is accepted with or without a trailing slash or query string. It is stored as `https://www.skills.google/public_profiles/<uuid>`, and `profile_id` is the lowercase UUID.
  - The legacy `https://www.cloudskillsboost.google/public_profiles/<uuid>` is rewritten to the skills.google form.
  - `https://me.developers.google.com/u/<digits>` (and g.dev) gets `unsupported_platform` and is skipped. The URL is kept for a future collector.
  - Anything else gets `invalid_url`, and an empty cell gets `missing_url`.
  - When two USNs use the same profile, both are flagged `shared_profile`.
- **Re-imports** upsert by USN:
  - Students missing from the new file become `active=False`.
  - If a student's profile URL changes, their stored badges are deleted and the link goes back to `pending`.
- Emails, the college email and the USN are stored for organizers, but **no public endpoint returns them**.

## How teams are mapped

The form has no team column. Teams come from either:

1. an optional **`Team`** column in the same sheet, or
2. a mapping file passed with `--teams` (or as the `teams` field of `POST /api/admin/import`). It can be CSV or XLSX, with columns `USN, Team`. Where both exist, the file wins and the column fills the gaps.

If an import has no team information at all, existing team assignments are left untouched.

`LEADERBOARD_MODE` picks the leaderboard type:

| Value | Behaviour |
|---|---|
| `auto` (default) | Team leaderboard if any active member has a team, otherwise an individual (solo) leaderboard |
| `team` | Teams only. Students without a team aren't ranked; they are listed in `/api/leaderboard/status` → `unassigned_members` and in the import report. |
| `solo` | Individual students with a supported profile link |

## Scoring: `scoring.yaml`

Edit `scoring.yaml` (or point `SCORING_CONFIG` at another file); the next run uses it, with no code change or restart. Every run stores a SHA-256 hash and a full snapshot of the effective config (`/api/leaderboard/runs/{id}` → `scoring`), so any old leaderboard can be explained and reproduced.

```yaml
program_start: null        # "YYYY-MM-DD": badges earned before this date don't count
program_end: null          # optional, inclusive
points: {skill_badge: 10, course_badge: 3, game_badge: 5, other: 0}
count_deprecated: true      # false = "[Deprecated] ..." badges score 0
badge_overrides: {}         # exact badge title -> points; wins over category
max_points_per_member: null # optional cap
team_aggregation: sum       # sum | average (average over members with a working link)
min_team_members: 1         # teams with fewer active members are not ranked
```

- **Badge categories** come from the badge's "Learn more" dialog:
  - `/games/…` → `game_badge`
  - `/course_templates/…` with "skill badge" in the description → `skill_badge`
  - any other `/course_templates/…` → `course_badge`
  - everything else → `other`

  A title starting with `[Deprecated]` sets `deprecated` and keeps the category.
- **Dates:** the earned date is compared as the calendar date printed on the profile, which Google shows in US Eastern time (EDT/EST). It is not converted to IST. A badge showing "Earned Oct 31, 2025 EDT" counts for `program_end: 2025-10-31`, even though it was already Nov 1 in India. This keeps scoring matching what students see on their own profile. While a window is set, a badge without a readable date is not counted (`unknown_date`).
- Each `badge_id` counts once per student. A member's score is the sum of counted badge points, capped if a cap is set. A team's score is the `sum` or `average` of its member scores.
- **Tie-breakers**, in order:
  1. higher score
  2. more counted skill badges
  3. earlier *reached-final-score* date, meaning the earned date of the badge that brought the member (or, for teams, the last member) to their final score
  4. name, alphabetical
- When a badge doesn't count, the API gives the reason: `before_start`, `after_end`, `deprecated`, `zero_points` or `unknown_date`.

## The pipeline (one run)

1. **Lock.** The run takes an in-process asyncio lock plus a DB lock: it refuses to start while another run has been `running` for less than `RUN_LOCK_TIMEOUT_HOURS` (default 2). Older `running` rows are marked `failed` as abandoned.
2. **Create the run.** It creates a `runs` row and stores the scoring hash and snapshot.
3. **Fetch.** It fetches every active member with a supported link, with `FETCH_CONCURRENCY` parallel requests and a `FETCH_DELAY_SECONDS` pause after each one. It sends an honest `User-Agent` containing `CONTACT_EMAIL`. Failed requests are retried up to `FETCH_MAX_RETRIES` times, with exponential backoff and jitter, on timeouts, connection errors, 429 and 5xx; `Retry-After` is respected. A 404 means `not_found` and is not retried.
4. **Save results.**
   - **On success,** new badges are inserted, changed ones updated and vanished ones deleted. `first_seen_at` is preserved as evidence for disputes.
   - **On failure** (`fetch_error`, `not_found` or `parse_error`), the member's previous badges are kept and marked `stale`. A failure never counts as zero badges.
   - **Parse errors** come from two cases: the page has no `<h1>`, or it has no badges and also lacks the "…hasn't earned any badges yet" text. Zero badges is trusted only when the page says so, so a redesign of the badge section makes the run fail validation instead of silently zeroing everyone. An empty profile can still show a league and points, because points also come from labs.
5. **Score and rank,** then keep the top `LEADERBOARD_SIZE`.
6. **Validate.** The run is published only if `succeeded / attempted >= MIN_SUCCESS_RATIO`. Otherwise it is `rejected`, and the API keeps serving the previous published run. Standings are written in the same transaction that sets the status.

## API

Public endpoints never return emails or USNs.

| Method | Path | Description |
|---|---|---|
| GET | `/health` | `{"status":"ok","database":"ok"}` (503 if the DB is unreachable) |
| GET | `/api/leaderboard?limit=50` | Latest **published** run: `run_id`, `published_at`, `mode`, entries (rank, name, score, counted_badges, breakdown; for teams, each member's name, score and stale flag) |
| GET | `/api/leaderboard/history?limit=30` | Past runs with status and counts |
| GET | `/api/leaderboard/status` | Last run, last published run, next scheduled run, link-health counts, unassigned members (team mode) |
| GET | `/api/leaderboard/runs/{run_id}` | Standings and scoring snapshot of a past run |
| GET | `/api/teams/{team_id}` | Team, its members, and each member's counted and uncounted badges (with reason) |
| GET | `/api/members/{member_id}` | Name, department, year, display_name, league, platform_points, last_success_at, link_status, badges |

Admin endpoints require the header `X-Admin-Token: $ADMIN_TOKEN`. They return 401 when the header is missing, 403 when it is wrong, and 503 when no token is configured.

| Method | Path | Description |
|---|---|---|
| POST | `/api/admin/refresh` | Starts a run in the background → `202 {"run_id": …}`; `409` if one is running |
| POST | `/api/admin/import` | Multipart: `form` (.xlsx) and optional `teams` (.csv/.xlsx) → import report |
| GET | `/api/admin/link-report` | CSV: USN, name, email, profile_url, link_status, last_error, active, flags |

```bash
curl -X POST -H "X-Admin-Token: $ADMIN_TOKEN" -F form=@responses.xlsx -F teams=@teams.csv http://localhost:8000/api/admin/import
curl -X POST -H "X-Admin-Token: $ADMIN_TOKEN" http://localhost:8000/api/admin/refresh
```

Interactive docs are at `/docs`.

## Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `DATABASE_URL` | `sqlite:///data/leaderboard.db` | SQLAlchemy URL. MySQL: `mysql+pymysql://user:pass@host:3306/db` |
| `LEADERBOARD_MODE` | `auto` | `auto`, `team` or `solo` |
| `LEADERBOARD_SIZE` | `50` | Standings stored per run |
| `MIN_SUCCESS_RATIO` | `0.6` | Minimum successful/attempted fetches to publish |
| `SCORING_CONFIG` | `scoring.yaml` | Path to the scoring rules |
| `RUN_LOCK_TIMEOUT_HOURS` | `2` | After this, a `running` run is treated as abandoned |
| `CONTACT_EMAIL` | `organizers@example.edu` | Put into the User-Agent |
| `USER_AGENT` | `SkillLabLeaderboard/1.0 (+mailto:<CONTACT_EMAIL>)` | Full override |
| `FETCH_CONCURRENCY` | `3` | Parallel requests |
| `FETCH_DELAY_SECONDS` | `1.5` | Pause after each request (per slot) |
| `FETCH_TIMEOUT_SECONDS` | `20` | Per-request timeout |
| `FETCH_MAX_RETRIES` | `3` | Retries after the first attempt |
| `FETCH_BACKOFF_BASE` | `2.0` | Backoff base seconds (`base·2^n + jitter`) |
| `ADMIN_TOKEN` | *(empty: admin disabled)* | Shared secret for admin endpoints |
| `CORS_ORIGINS` | *(empty)* | Comma-separated allowed origins |
| `ENABLE_SCHEDULER` | `true` | Run the daily job in this process |
| `REFRESH_CRON` | `0 2 * * *` | Crontab for the refresh |
| `TIMEZONE` | `Asia/Kolkata` | Timezone of the cron expression |
| `LOG_LEVEL` | `INFO` | Python log level |

The variables are read from the environment, or from a `.env` file in the working directory.

### Scheduler: run exactly one

The daily job runs inside the API process: APScheduler's `AsyncIOScheduler`, with `max_instances=1`, `coalesce=True` and a one-hour misfire grace time. **Only one instance may run the scheduler.** If you run several API replicas, set `ENABLE_SCHEDULER=false` on all but one, or on all of them and trigger `POST /api/admin/refresh` from an external cron. The DB lock still stops overlapping runs across processes.

## Deployment

### Docker (any small VM)

```bash
docker build -t skill-leaderboard .
docker run -d --name leaderboard -p 8000:8000 \
  --env-file .env -v leaderboard-data:/app/data \
  --restart unless-stopped skill-leaderboard

# import / refresh inside the container
docker cp responses.xlsx leaderboard:/app/data/responses.xlsx
docker exec leaderboard python -m app.cli import data/responses.xlsx
docker exec leaderboard python -m app.cli refresh
```

The image is `python:3.11-slim` and runs as a non-root `app` user, with `/app/data` as a volume (SQLite lives there). Put Caddy or nginx in front for TLS.

### Render / Railway

- Deploy from the repo with the Dockerfile. Both platforms set `$PORT`, which the image honours.
- **Render:** attach a persistent disk mounted at `/app/data`, or use a managed MySQL and set `DATABASE_URL`. Run a single instance, or set `ENABLE_SCHEDULER=false` on extra ones.
- **Railway:** add a volume at `/app/data`, or a MySQL plugin, and set `DATABASE_URL=mysql+pymysql://…`.
- Set `ADMIN_TOKEN`, `CONTACT_EMAIL` and `CORS_ORIGINS` (your frontend's origin) in the dashboard. Upload responses with `POST /api/admin/import`.

## Backups

- **SQLite:** the whole state is `data/leaderboard.db`. Make a consistent copy while the app runs with `sqlite3 data/leaderboard.db ".backup data/backup-$(date +%F).db"`, or stop the app and copy the file. Keep the copies off the server.
- **MySQL:** use the provider's automated backups, or `mysqldump --single-transaction`.
- Old leaderboards stay reproducible from the DB alone: every run keeps its standings and the scoring snapshot.

## Project layout

```
app/
  config.py          settings from env
  db.py, models.py   engine/session, ORM (teams, members, badges, runs, standings)
  urls.py            profile-URL validation and normalisation
  importer.py        form + team import, idempotent upsert
  collectors/        base.py: Collector protocol (fetch(profile) -> ProfileResult)
                     skills_google.py + skills_parser.py: Google Skills collector
  scoring.py         scoring.yaml rules, badge verdicts, tie-break ranking
  leaderboard.py     builds entries from the DB
  pipeline.py        lock → collect → score → validate → publish
  scheduler.py       APScheduler daily job
  api.py, cli.py     FastAPI app and CLI
scripts/             synthetic sample data + offline mock refresh
tests/               pytest suite (network mocked)
```

### Adding the Google Developer Program (g.dev) later

1. Implement `Collector` in `app/collectors/` (`platform = "google_developers"`, `async fetch(ProfileRef) -> ProfileResult`).
2. Register it in `build_collectors`.
3. Add `"google_developers"` to `SUPPORTED_PLATFORMS` in `app/leaderboard.py`.
4. Have `urls.check_profile_url` return `pending` for those links.

The URLs are already stored.

Schema is created with `create_all` on startup. The models are plain declarative SQLAlchemy, so Alembic can be added later (`alembic init`, point `target_metadata` at `app.db.Base.metadata`).
