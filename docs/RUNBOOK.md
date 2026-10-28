# Operations runbook

A step-by-step guide for whoever runs the Skill Lab leaderboard day to day. You don't need to have seen the project before. The [README](../README.md) is the technical reference. This page tells you what to *do*.

> **Two rules before anything else**
> 1. **Never share or commit student data**: the form export (`.xlsx`), the team sheet, the link report, database backups, or anything containing names, emails or USNs. Keep these files in `data/` (git ignores that folder), and don't paste them into chats or issues.
> 2. **Never share secrets**: `ADMIN_TOKEN` and `DATABASE_URL` (it contains the database password). If you think one has leaked, rotate it (section 5).

Contents:
1. [What the system does](#1-what-the-system-does)
2. [Environment variables](#2-environment-variables)
3. [Local setup](#3-local-setup)
4. [Deploying to Render with Supabase Postgres](#4-deploying-to-render-with-supabase-postgres)
5. [Updating environment variables and rotating ADMIN_TOKEN](#5-updating-environment-variables-and-rotating-admin_token)
6. [Daily refresh](#6-daily-refresh)
7. [Routine tasks (copy-paste commands)](#7-routine-tasks-copy-paste-commands)
8. [Changing scoring](#8-changing-scoring)
9. [Troubleshooting](#9-troubleshooting)
10. [Backups](#10-backups)
11. [Pre-launch checklist](#11-pre-launch-checklist)

---

## 1. What the system does

- Students submit their **Google Skills public profile link** through a Google Form. We download the responses as `.xlsx` and import them.
- Once a day the server visits every public profile, reads the badges, and scores them using the rules in `scoring.yaml`.
- It ranks students (or teams, if a team sheet was uploaded) and publishes the top 50.
- The website at `/` shows the latest published leaderboard. The API under `/api/...` serves the same data and never shows emails or USNs.
- It runs as one Docker container on Render, with its data in a Supabase Postgres database.

### How a daily run works

1. **Import** (only when new form responses come in): the `.xlsx` is uploaded and each student is added or updated by USN. Students missing from the new file are deactivated.
2. **Fetch:** a refresh is triggered, usually by cron-job.org at 02:00 IST. The server fetches each active student's profile, three at a time, with a pause between requests. If a profile fails (network error, 404, page it can't read), that student's badges from the last good fetch are kept and marked **stale**. A failure never counts as zero badges.
3. **Score:** badges are scored using `scoring.yaml`, then students/teams are ranked and the top `LEADERBOARD_SIZE` kept.
4. **Validate and publish:** the run is **published** only if
   - at least `MIN_SUCCESS_RATIO` (60%) of profiles were fetched successfully, **and**
   - no more than `MAX_OTHER_BADGE_RATIO` (50%) of the badges seen were classified `other`.

   Otherwise the run is **rejected**. A rejected run is kept for inspection (its `notes` say why), but **the website keeps showing the last published leaderboard**. A bad day never wipes the board. A flood of `other` badges or a low success ratio usually means Google changed the profile page (see section 9).

Each run has a status: `running`, `published`, `rejected` or `failed` (crashed; the notes have the error).

---

## 2. Environment variables

All settings are environment variables, read by `app/config.py`. Locally they come from a `.env` file. On Render you set them in the **Environment** tab. 🔒 = secret: never share it, never commit it.

| Variable | What it does | Default | Example | Required in production? |
|---|---|---|---|---|
| `DATABASE_URL` 🔒 | Where the data lives (SQLAlchemy URL). Contains the DB password. `postgres://` is accepted and rewritten. | `sqlite:///data/leaderboard.db` | `postgresql://postgres.abcd1234:PASSWORD@aws-0-ap-south-1.pooler.supabase.com:5432/postgres?sslmode=require` | **Yes**: Supabase session-pooler URI with `sslmode=require`. The SQLite default is wiped on every Render deploy. |
| `ADMIN_TOKEN` 🔒 | Password for the admin endpoints (`/api/admin/*`), sent as the `X-Admin-Token` header. Empty = admin endpoints disabled (503). | *(empty)* | output of `python -c "import secrets; print(secrets.token_urlsafe(32))"` | **Yes**: at least 32 random characters |
| `CONTACT_EMAIL` | Put into the User-Agent so Google can contact us about our scraping. Not secret, but must be real. | `organizers@example.edu` | `gdg-skilllab@yourcollege.edu` | **Yes**: a real, monitored address |
| `USER_AGENT` | Full override of the User-Agent header. Normally leave unset. | `SkillLabLeaderboard/1.0 (+mailto:<CONTACT_EMAIL>)` | | No |
| `LEADERBOARD_MODE` | `auto` = teams if any student has a team, otherwise individuals; `team`; or `solo`. | `auto` | `team` | No |
| `LEADERBOARD_SIZE` | How many entries each run stores and shows. | `50` | `50` | No |
| `MIN_SUCCESS_RATIO` | Reject a run if fewer than this share of profiles fetched OK. | `0.6` | `0.6` | No |
| `MAX_OTHER_BADGE_RATIO` | Reject a run if more than this share of badges is category `other`. | `0.5` | `0.5` | No |
| `SCORING_CONFIG` | Path to the scoring rules file. | `scoring.yaml` | `scoring.yaml` | No |
| `RUN_LOCK_TIMEOUT_HOURS` | A run still `running` after this long is treated as abandoned. | `2` | `2` | No |
| `FETCH_CONCURRENCY` | Profiles fetched in parallel. Keep it low; be polite to Google. | `3` | `3` | No |
| `FETCH_DELAY_SECONDS` | Pause after each request. | `1.5` | `1.5` | No |
| `FETCH_TIMEOUT_SECONDS` | Per-request timeout. | `20` | `20` | No |
| `FETCH_MAX_RETRIES` | Retries on timeouts, 429 and 5xx. | `3` | `3` | No |
| `FETCH_BACKOFF_BASE` | Base seconds for exponential backoff between retries. | `2.0` | `2.0` | No |
| `CORS_ORIGINS` | Comma-separated origins allowed to call the API from a browser. **Leave empty** when the site is served by this app at `/` (our setup). Only set it if a frontend is hosted elsewhere. | *(empty)* | `https://leaderboard.yourclub.dev` | No: leave empty on Render |
| `ENABLE_SCHEDULER` | Run the 02:00 job inside the server process. | `true` | `false` | **Set to `false` on Render's free plan** (the service sleeps, so the job would be missed; use cron-job.org instead) |
| `REFRESH_CRON` | When the in-process scheduler runs (crontab syntax). Only used if `ENABLE_SCHEDULER=true`. | `0 2 * * *` | `0 2 * * *` | No |
| `TIMEZONE` | Timezone for `REFRESH_CRON`. | `Asia/Kolkata` | `Asia/Kolkata` | No |
| `LOG_LEVEL` | `DEBUG`, `INFO`, `WARNING`, `ERROR`. | `INFO` | `INFO` | No |
| `PORT` | Port the container listens on. **Render sets this itself**; don't add it. | `8000` | | No |

Rules for the `.env` file: one `KEY=value` per line, no quotes needed, no spaces around `=`, comments on their own line starting with `#`. Don't paste commands or notes into it. Lines like that break `docker run --env-file`.

---

## 3. Local setup

Use local setup to try things out or to debug a profile. **Locally, always use SQLite** (the default `DATABASE_URL`). If your `.env` points at the Supabase URL, every local `import` or `refresh` writes straight to the live database.

You need Python 3.11+ and git.

### Windows (PowerShell)

```powershell
git clone https://github.com/Berserker-GM/GDG.git
cd GDG
py -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt -r requirements-dev.txt
Copy-Item .env.example .env
notepad .env
```

If `Activate.ps1` is blocked by the execution policy, skip activation and prefix every command with `.venv\Scripts\` instead, e.g. `.venv\Scripts\python -m app.cli refresh`.

In PowerShell, `curl` is *not* real curl. Type **`curl.exe`** for the commands in section 7.

### Mac / Linux

```bash
git clone https://github.com/Berserker-GM/GDG.git
cd GDG
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env
nano .env
```

### Edit `.env`

For local use, change only these:

- `ADMIN_TOKEN`: generate one with `python -c "import secrets; print(secrets.token_urlsafe(32))"`. Use a *different* token locally than in production.
- `CONTACT_EMAIL`: your organizer email.
- `ENABLE_SCHEDULER=false`: you don't want a 02:00 job on your laptop.
- Leave `DATABASE_URL=sqlite:///data/leaderboard.db`.

### Run it

```bash
pytest                                   # all tests should pass; no network needed

mkdir data                               # the form export goes here (git ignores it)
# put the downloaded form export at data/responses.xlsx (and the optional team sheet at data/teams.csv)

python -m app.cli import data/responses.xlsx --teams data/teams.csv   # omit --teams if you have none
python -m app.cli refresh                # fetches every profile; takes a minute or two
python -m app.cli leaderboard --limit 10
python -m app.cli serve --port 8000
```

Open <http://localhost:8000> for the site, and <http://localhost:8000/docs> for the API explorer. Stop the server with Ctrl+C.

Test one profile without touching the database:

```bash
python -m app.cli scrape-one https://www.skills.google/public_profiles/<uuid>
```

---

## 4. Deploying to Render with Supabase Postgres

### 4a. Create the database (Supabase)

1. Sign in at <https://supabase.com> → **New project**.
2. Pick the organization, name it (e.g. `skill-leaderboard`), set a **strong database password** and save it in your password manager, and pick a region near Render's (e.g. *South Asia (Mumbai)* or *Southeast Asia (Singapore)*). Click **Create new project** and wait about 2 minutes.
3. Click **Connect** at the top of the project page.
4. Under **Connection string**, choose **Session pooler**. Don't use *Direct connection*: it is IPv6-only and Render can't reach it. Don't use the *Transaction pooler* (port 6543) either.
5. Copy the URI. It looks like
   `postgresql://postgres.<project-ref>:[YOUR-PASSWORD]@aws-0-<region>.pooler.supabase.com:5432/postgres`
6. Replace `[YOUR-PASSWORD]` with the password, and **add `?sslmode=require` at the end**:
   `postgresql://postgres.<project-ref>:<password>@aws-0-<region>.pooler.supabase.com:5432/postgres?sslmode=require`

   If the password contains `@ : / ? # [ ]` or spaces, percent-encode them (`@` → `%40`, `#` → `%23`, `/` → `%2F`, `:` → `%3A`), or pick a password without them.

The app creates its tables on first start and turns on Row Level Security for them. Use the `postgres.<project-ref>` user from the pooler URI, which owns the tables.

### 4b. Create the web service (Render)

1. Sign in at <https://dashboard.render.com> with GitHub → **New +** → **Web Service**.
2. **Source Code:** pick the GitHub repo `Berserker-GM/GDG` (click *Configure account* to give Render access if it isn't listed) → **Connect**.
3. Fill in:
   - **Name:** e.g. `skill-leaderboard` (gives `https://skill-leaderboard.onrender.com`)
   - **Language:** `Docker`
   - **Branch:** `main`
   - **Region:** Singapore (closest to India)
   - **Root Directory:** leave empty
   - **Instance Type:** `Free`
4. **Environment Variables** → *Add Environment Variable* for each:

   | Key | Value |
   |---|---|
   | `DATABASE_URL` | the session-pooler URI from 4a, ending in `?sslmode=require` |
   | `ADMIN_TOKEN` | a fresh `secrets.token_urlsafe(32)` value (not one you've used anywhere else) |
   | `CONTACT_EMAIL` | the real organizer email |
   | `ENABLE_SCHEDULER` | `false` |
   | `LEADERBOARD_MODE` | `auto` (or `team` / `solo`) |

   Don't add `PORT` (Render sets it) or `CORS_ORIGINS` (leave unset).
5. Open **Advanced**:
   - **Health Check Path:** `/health`
   - **Dockerfile Path:** `./Dockerfile` (default)
   - **Auto-Deploy:** `On Commit` (every merge to `main` redeploys)
6. Click **Deploy Web Service**. Watch the **Logs** tab. The deploy is done when you see `Uvicorn running on http://0.0.0.0:...` and the status turns **Live**.
7. Check it:

   ```bash
   curl https://skill-leaderboard.onrender.com/health
   # {"status":"ok","database":"ok"}
   ```

   `"database":"unreachable"` (HTTP 503) means `DATABASE_URL` is wrong. See section 9.
8. Import the form (section 7a), trigger a refresh (7c), and open the site.

**Free plan facts:** the service sleeps after about 15 minutes without requests. The first request afterwards takes about a minute while it wakes, and the in-process scheduler can't be trusted. That's why `ENABLE_SCHEDULER=false` and the refresh comes from outside (section 6). Run only **one** instance.

---

## 5. Updating environment variables and rotating ADMIN_TOKEN

### Locally

1. Edit `.env` in a text editor and save.
2. Restart: stop `python -m app.cli serve` with Ctrl+C and start it again. CLI commands (`import`, `refresh`, …) read `.env` each time they start, so they need no restart.

### On Render

1. Dashboard → your service → **Environment** tab.
2. Click **Edit**, change or add the variable.
3. Click **Save, rebuild, and deploy** (or **Save and deploy**). Render restarts the service with the new values. Watch **Events/Logs** until it's **Live** again (1–3 minutes).
4. Re-check `/health`.

If you choose *Save only*, the new value takes effect at the next deploy, not now.

### Rotating `ADMIN_TOKEN` safely

Rotate it **before launch** if the current one was ever pasted in a chat, email, screenshot, issue, or committed anywhere. Rotate it again whenever someone with access leaves the team.

1. Generate a new token: `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
2. **Render first:** Environment → set `ADMIN_TOKEN` to the new value → Save and deploy. Wait until **Live**. From now on the old token gets `403`.
3. **cron-job.org:** edit the refresh job → *Advanced* → header `X-Admin-Token` → paste the new value → Save. Click **Test run** and confirm `202` (or `409` if a run is going).
4. **GitHub Actions** (if used): repo → Settings → Secrets and variables → Actions → `ADMIN_TOKEN` → Update.
5. **Scripts and shells:** anywhere you stored it (password manager, `scripts/run_pipeline.py --admin-token`, your local `.env` if you used the production token there, which you shouldn't).
6. Next morning, check that the 02:00 run happened (section 7d).

Between steps 2 and 3 the cron job would fail with 403. Do steps 2–4 together, not at 01:59.

---

## 6. Daily refresh

Use **one** of these: cron-job.org (recommended) or GitHub Actions. If both are active, the second call gets a harmless `409`.

### Option A: cron-job.org (recommended)

1. Sign up at <https://cron-job.org> → **Settings** → set your account timezone to **Asia/Kolkata**.
2. **Cronjobs** → **Create cronjob**:
   - **Title:** `Leaderboard daily refresh`
   - **URL:** `https://skill-leaderboard.onrender.com/api/admin/refresh`
   - **Execution schedule:** *Every day* at **02:00**
3. **Advanced** tab:
   - **Time zone:** `Asia/Kolkata`
   - **Request method:** `POST`
   - **Headers:** *Add header* → key `X-Admin-Token`, value = the production `ADMIN_TOKEN`
   - **Timeout:** set it to 60 seconds or more, or to the maximum your account allows, because a sleeping Render instance takes up to about a minute to answer.
   - **Treat redirects as success:** off
4. **Notifications:** turn on *notify on failure* to your organizer email.
5. **Create**, then click **Test run**. You should get **HTTP 202** with a body like `{"run_id": 12, "status": "running"}`.

**Recommended:** create a second job, **Leaderboard wake-up**, that does `GET https://skill-leaderboard.onrender.com/health` every day at **01:55** Asia/Kolkata with no headers. The service is then awake when the 02:00 job fires, so a short timeout can't cause a miss. It also keeps the Supabase free project from pausing for inactivity.

The refresh answers `202` straight away and runs in the background for a minute or two. Requests keep Render awake for about 15 minutes, which is enough.

### Option B: GitHub Actions

The workflow is already in the repo: [`.github/workflows/daily-refresh.yml`](../.github/workflows/daily-refresh.yml). It is **disabled by default**: it only runs when started by hand.

1. Repo → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**:
   - `APP_URL` = `https://skill-leaderboard.onrender.com`
   - `ADMIN_TOKEN` = the production token
2. **Actions** tab → *Daily leaderboard refresh* → **Run workflow** → check it goes green and the log shows `HTTP 202`.
3. To run it daily, edit the workflow and uncomment the two `schedule` lines (`cron: "30 20 * * *"`, which is 20:30 UTC = 02:00 IST), then merge to `main`. Turn off the cron-job.org job if you switch.

GitHub may start scheduled runs 5–30 minutes late. It also disables scheduled workflows in a repo with no commits for 60 days, so re-enable it from the Actions tab if that happens.

---

## 7. Routine tasks (copy-paste commands)

Set these once per terminal session. Typing the token at a prompt keeps it out of your shell history.

**Mac/Linux (bash/zsh):**

```bash
export APP_URL=https://skill-leaderboard.onrender.com
read -rs ADMIN_TOKEN && export ADMIN_TOKEN     # paste the token, press Enter
```

**Windows (PowerShell):** use `curl.exe`, not `curl`:

```powershell
$env:APP_URL = "https://skill-leaderboard.onrender.com"
$env:ADMIN_TOKEN = Read-Host "Admin token"
```

The commands below are in bash form. In PowerShell, replace `curl` with `curl.exe`, `$APP_URL` with `$env:APP_URL`, `$ADMIN_TOKEN` with `$env:ADMIN_TOKEN`, and the `\` line endings with `` ` ``.

If the service was asleep, the first command can take about a minute. Add `--max-time 120` if it times out.

### 7a. Import new form responses

1. Google Form → **Responses** → **Link to Sheets** (or **View in Sheets** if already linked) → in the sheet, **File → Download → Microsoft Excel (.xlsx)**. Save it as `data/responses.xlsx`. The form's own "Download responses" gives `.csv`, which the importer does not accept.
2. Upload the **whole** export, not just the new rows. Students missing from the file are **deactivated**. Re-importing the same file is safe.

```bash
curl -fsS -X POST -H "X-Admin-Token: $ADMIN_TOKEN" \
  -F form=@data/responses.xlsx \
  "$APP_URL/api/admin/import"
```

The response is an import report: counts, invalid/unsupported links, duplicate USNs, students without a team. It contains names and USNs, so don't paste it anywhere public. New students are picked up by the next refresh. Trigger one (7c) if you want them on the board now.

### 7b. Upload the team sheet

The team sheet is a `.csv` (or `.xlsx`) with exactly these two columns and a header row:

```csv
USN,Team
1XX23CS001,Cloud Ninjas
1XX23CS002,Cloud Ninjas
1XX23CS003,Byte Busters
```

USNs are matched case-insensitively, ignoring spaces. Team names are matched case-insensitively. The API only accepts the team sheet **together with the form**, so always send both:

```bash
curl -fsS -X POST -H "X-Admin-Token: $ADMIN_TOKEN" \
  -F form=@data/responses.xlsx \
  -F teams=@data/teams.csv \
  "$APP_URL/api/admin/import"
```

With `LEADERBOARD_MODE=auto` the board switches to teams as soon as any student has a team. Students without a team are listed under `unassigned_members` in the status (7d) and aren't ranked in team mode. An import with no team information leaves existing team assignments unchanged.

### 7c. Trigger a manual refresh

```bash
curl -fsS -X POST -H "X-Admin-Token: $ADMIN_TOKEN" "$APP_URL/api/admin/refresh"
# {"run_id": 13, "status": "running"}
```

`409` means a run is already in progress. Wait for it to finish (section 9).

### 7d. Check status

```bash
curl -fsS "$APP_URL/api/leaderboard/status" | python -m json.tool
curl -fsS "$APP_URL/api/leaderboard/history?limit=5" | python -m json.tool
```

In the status, check:

- `running`: whether a run is going right now.
- `last_run.status` and `last_run.notes`: `published`, or `rejected`/`failed` and why.
- `last_published_run.published_at`: when the board last changed (UTC).
- `last_run.success_ratio`: share of profiles fetched OK.
- `link_health`: how many students are `ok`, `not_found`, `fetch_error`, `invalid_url`, `unsupported_platform`, `missing_url`, `pending`.
- `stale_members`: students whose last fetch failed and who are showing older badges.

No admin token is needed. Status never shows emails or USNs.

### 7e. Download the link report

A CSV of every student with USN, name, email, profile URL, link status, last error, active and flags. Use it to chase broken links. **It contains personal data:** keep it in `data/` and delete it when you're done.

```bash
curl -fsS -H "X-Admin-Token: $ADMIN_TOKEN" -o data/link-report.csv "$APP_URL/api/admin/link-report"
```

Open it in a spreadsheet and filter `link_status` for anything other than `ok`.

---

## 8. Changing scoring

All scoring rules are in [`scoring.yaml`](../scoring.yaml) at the repo root:

```yaml
program_start: "2026-10-01"  # badges earned before this date don't count ("YYYY-MM-DD", in quotes)
program_end: null            # optional last day, inclusive
points:
  skill_badge: 10
  course_badge: 3
  game_badge: 5
  other: 0
count_deprecated: true        # false = "[Deprecated] ..." badges score 0
badge_overrides:              # exact badge title -> points; wins over the category
  "Build a Secure Google Cloud Network": 15
max_points_per_member: null   # optional cap per student
team_aggregation: sum         # sum | average
min_team_members: 1           # teams with fewer active members are not ranked
```

- **`program_start` / `program_end`** are compared with the date printed on the profile, which Google shows in US Eastern time. A badge shown as "Earned Oct 31, 2026 EDT" counts for `program_end: "2026-10-31"`. While a window is set, badges without a readable date don't count.
- **`points`** are per badge category. Categories come from the badge's "Learn more" link: skill badge, course badge, game badge, or `other`.
- **`badge_overrides`** keys must match the badge title *exactly*, as shown in `/api/members/{id}`.

### How to change it

1. Edit `scoring.yaml` on a branch, open a PR, get it reviewed, merge to `main`. The file is built into the Docker image, so changing it means a deploy. Render auto-deploys on merge.
2. Changes apply **from the next run**. Trigger one (7c) if you don't want to wait until 02:00.

### What happens to old runs

- Each run stores a **SHA-256 hash** and a **full snapshot** of the scoring rules it used (`scoring_hash`; the full rules are at `/api/leaderboard/runs/{run_id}` → `scoring`). Old runs are never rescored, so any past leaderboard can be explained.
- The published board doesn't change until the next run publishes. Meanwhile, the team and student detail pages show a `live_score` computed with the new rules next to the `published_score`. The two differing after a scoring change is expected.
- If a YAML mistake breaks the file, the next run fails (`status: failed`, the notes give the error) and the old board stays up. Fix the file and redeploy.

---

## 9. Troubleshooting

### A run was rejected

Look at `last_run.notes` in the status (7d).

- **"Success ratio 0.xx below MIN_SUCCESS_RATIO"**: too many profiles failed to fetch.
  - Check `link_health` and the link report's `last_error` column.
  - Mostly `fetch_error` with timeouts or 429: Google was slow or rate-limiting. Wait and trigger another refresh later. Don't raise `FETCH_CONCURRENCY`.
  - Mostly `parse_error`: **Google changed the profile page.** See the next item.
  - Mostly `not_found`: profile links went private or were deleted. Chase those students.
- **"N of M badges (xx%) were classified 'other'"**: badges are still found but can't be classified, which means **Google changed the profile page markup**.
  - Confirm with `python -m app.cli scrape-one <a profile you know has badges>` (section 3). Look at each badge's `category`, and at `status` (`parse_error`).
  - Fixing it is a code change in `app/collectors/skills_parser.py`. Open an issue for a developer, and attach the `scrape-one` output only for your *own* profile.
  - Don't lower `MIN_SUCCESS_RATIO` or raise `MAX_OTHER_BADGE_RATIO` to force a publish. That would publish wrong scores.

The site keeps showing the last published board the whole time, so nothing is broken for students.

### A student says badges are missing

1. Find them on the site and open their card, or `GET /api/members/{member_id}`. Each badge is listed under `counted_badges` or `uncounted_badges`, with a reason:
   - `before_start` / `after_end`: earned outside `program_start`–`program_end`.
   - `deprecated`: deprecated badge and `count_deprecated: false`.
   - `zero_points`: the category scores 0 (usually `other`).
   - `unknown_date`: no readable date while a program window is set.
2. **Stale flag** (`stale: true`): the last fetch of their profile failed, so they're showing badges from the last good fetch. Check `link_status` and `last_error` in the link report (7e).
3. **Badge not listed at all:**
   - It was earned after the last run. It shows after the next one, or trigger a refresh.
   - Their profile is **private** or the link is wrong (`not_found` = 404). Ask them to make their Google Skills profile public, open the link in a private/incognito window to check it shows their badges, and resubmit the form with the `https://www.skills.google/public_profiles/...` link. Then re-import (7a).
   - They submitted the form twice with different links. The latest submission (by timestamp) wins, and the import report lists it as a *USN conflict*.
4. **Not on the board at all:** only the top `LEADERBOARD_SIZE` are shown. In team mode, students without a team aren't ranked.

### Unsupported g.dev links

Links like `https://g.dev/...` or `https://me.developers.google.com/u/...` are Google Developer Program profiles, not Google Skills profiles. They're stored with `link_status: unsupported_platform` and skipped. Ask the student to resubmit with their **Google Skills public profile** link (`https://www.skills.google/public_profiles/<id>`), then re-import. `invalid_url` and `missing_url` are handled the same way.

### Render is sleeping / the first request is slow

On the free plan the service sleeps after about 15 minutes idle, and the next request takes up to about a minute. This is normal. The site loads after the wait. Mitigations: the 01:55 wake-up job (section 6), a timeout of 60 seconds or more on the refresh job, or a paid instance.

### Database connection errors

Symptoms: `/health` returns `503` with `"database":"unreachable"`, or the deploy logs show `OperationalError`, `could not translate host name`, `password authentication failed`, `SSL`, or `Network is unreachable`.

- **Wrong host / IPv6:** use the **Session pooler** URI (`...pooler.supabase.com:5432`), not `db.<ref>.supabase.co`.
- **Password:** re-paste it. Percent-encode special characters (section 4a), or reset the database password in Supabase (*Project Settings → Database*) and update `DATABASE_URL` on Render.
- **SSL:** the URL must end in `?sslmode=require`.
- **User name:** the pooler user is `postgres.<project-ref>`, not plain `postgres`.
- **Project paused:** free Supabase projects pause after a period of inactivity. Open the project in the Supabase dashboard and click **Restore**.
- **`must be owner of table`:** the role in `DATABASE_URL` doesn't own the tables. Use the same `postgres.<project-ref>` user that created them.

After fixing `DATABASE_URL`, save and deploy (section 5) and re-check `/health`.

### 409 "run already in progress"

A refresh is already running. Normal runs take a minute or two. Check `running` in the status (7d) and try again once it's `false`.

If it stays stuck (the service restarted or slept mid-run), the old run stays `running` in the database. It blocks new runs until `RUN_LOCK_TIMEOUT_HOURS` (2 hours) after it started. The next refresh after that marks it `failed` ("Abandoned") and runs normally. Just wait and retry. Don't edit the database by hand.

---

## 10. Backups

Backups contain all student data. Store them encrypted or access-controlled (e.g. a private organizer Drive folder), **never** in the repo or a group chat.

### Supabase backups

- Dashboard → **Database** → **Backups** shows what your plan includes. Paid plans have automatic daily backups you can restore from there. **On the free plan, don't rely on platform backups**: take your own with `pg_dump`.
- Every run keeps its standings and scoring snapshot, so the database alone can reproduce any past leaderboard.

### `pg_dump`

Install the PostgreSQL client tools: [postgresql.org/download](https://www.postgresql.org/download/) (Windows installer: tick *Command Line Tools* only), `brew install libpq` (Mac), or `sudo apt install postgresql-client` (Linux). The `pg_dump` major version must be at least the server's; check the server's with `select version();` in the Supabase SQL editor.

Use the same session-pooler URI, but it must start with `postgresql://`. If yours says `postgresql+psycopg2://`, drop the `+psycopg2` for these tools.

```bash
# Mac/Linux
pg_dump "postgresql://postgres.<project-ref>:<password>@aws-0-<region>.pooler.supabase.com:5432/postgres?sslmode=require" \
  --format=custom --no-owner --no-privileges --schema=public \
  --file="data/leaderboard-$(date +%F).dump"
```

```powershell
# Windows PowerShell
pg_dump "postgresql://postgres.<project-ref>:<password>@aws-0-<region>.pooler.supabase.com:5432/postgres?sslmode=require" `
  --format=custom --no-owner --no-privileges --schema=public `
  --file="data/leaderboard-$(Get-Date -Format yyyy-MM-dd).dump"
```

Take one before launch, weekly during the program, and before any risky change (scoring edits, big re-imports).

Restore into an **empty** database (a new Supabase project), then point `DATABASE_URL` at it:

```bash
pg_restore --no-owner --no-privileges --dbname "postgresql://...new project...?sslmode=require" data/leaderboard-YYYY-MM-DD.dump
```

---

## 11. Pre-launch checklist

- [ ] **`program_start` set** in `scoring.yaml` (quoted `"YYYY-MM-DD"`), merged to `main`, and deployed. Also `program_end` if the program has an end date.
- [ ] **Render env vars set:** `DATABASE_URL` (session pooler, `?sslmode=require`), `ADMIN_TOKEN`, a real `CONTACT_EMAIL`, `ENABLE_SCHEDULER=false`. `/health` returns `{"status":"ok","database":"ok"}`.
- [ ] **`ADMIN_TOKEN` rotated** (section 5): the production token is fresh, at least 32 characters, never pasted in a chat/issue/screenshot/commit, and different from any local or test token.
- [ ] **Form imported** from the full, latest export. The import report has been read, and invalid, unsupported and missing links followed up.
- [ ] **Team sheet uploaded** (`USN,Team`) if running in team mode. `unassigned_members` in the status is empty, or each one is known.
- [ ] **Cron job active** on cron-job.org (02:00 Asia/Kolkata, POST, `X-Admin-Token` header, timeout of 60 seconds or more, failure notifications on), plus the 01:55 wake-up job. A **Test run** returned `202`. If you use GitHub Actions instead, its schedule is uncommented and the cron-job.org job is off.
- [ ] **One successful published run:** the status shows `last_published_run` with a reasonable `success_ratio`, and the board looks right for students you can check by hand.
- [ ] **Frontend checked on mobile:** open the site on a phone (portrait and landscape). The board loads, names and scores are readable, and team/member cards open.
- [ ] **Backup taken** with `pg_dump` (section 10) and stored safely.
- [ ] Someone other than you has read this runbook and knows where the token is kept.
