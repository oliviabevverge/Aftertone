# Aftertone

[![CI](https://github.com/oliviabevverge/Aftertone/actions/workflows/ci.yml/badge.svg)](https://github.com/oliviabevverge/Aftertone/actions/workflows/ci.yml)

Aftertone turns your Spotify listening history into personal listening
recommendations. It combines recurring song sequences, artist frequency,
time-of-day patterns, saved places, and your editable Office/Gym/Car preferences
to rank tracks from your own history.

## What it does

- Syncs recently played tracks from Spotify and explores listening patterns.
- Ranks recommendations for different times, saved places, and listening modes.
- Lets you save recommendation feedback and optionally log private searches.
- Saves account data in an isolated local SQLite database or a per-account
  PostgreSQL schema, so it can follow your Spotify account across devices.
- Works as an installable mobile web app when hosted over HTTPS.

Recommendations are generated from synced listening history, not a hardcoded
playlist. Spotify's recently-played API is not a complete listening archive;
recommendations and stats only include history Aftertone can sync.

## Stack

- **Frontend:** React 19, Vite, and Oxlint
- **API:** FastAPI and Python
- **Storage:** SQLite for local development; PostgreSQL for persistent hosting
- **Music data and sign-in:** Spotify Web API and OAuth
- **Hosting:** Docker on Render, with Supabase Postgres

## Run locally

You need Python, Node.js, and a Spotify Developer app. Add this redirect URI to
the Spotify app:

```text
http://127.0.0.1:8888/callback
```

Create a root `.env` file with your Spotify app credentials:

```dotenv
SPOTIFY_CLIENT_ID=your_client_id
SPOTIFY_CLIENT_SECRET=your_client_secret
SPOTIFY_REDIRECT_URI=http://127.0.0.1:8888/callback
FRONTEND_URL=http://127.0.0.1:5173
```

Keep `.env` private; never commit credentials or add them to frontend code.
Start the backend and frontend in separate terminals:

```sh
cd backend
python -m pip install -r requirements.txt
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8888
```

```sh
cd frontend
npm ci
npm run dev -- --host 127.0.0.1
```

Open <http://127.0.0.1:5173> and choose **Connect Spotify**. Local development
stores data in SQLite under `backend/data/`; do not commit that data.

## Checks

Run the backend tests and frontend quality checks:

```sh
cd backend
python -m unittest discover -s tests -q
```

```sh
cd frontend
npm ci
npm run lint
npm run build
```

GitHub Actions runs the same checks for pushes and pull requests.

## Deploy

The root `Dockerfile` builds the frontend and runs the API on a single origin.
Connect this repository to Render using the included `render.yaml` Blueprint.
The deployment expects a Supabase PostgreSQL database and Spotify credentials;
Render prompts for `DATABASE_URL`, `SPOTIFY_CLIENT_ID`,
`SPOTIFY_CLIENT_SECRET`, and `AFTERTONE_ALLOWED_SPOTIFY_USERS`. Use Supabase's
Session Pooler connection string and allow only your own Spotify account.

For the complete setup:

1. Create a Supabase project and copy its Session Pooler URL. Percent-encode
   reserved characters in the database password.
2. Add `https://aftertone.onrender.com/callback` as a Spotify Redirect URI.
3. Create a private GitHub repository and deploy its Render Blueprint. Set the
   Render secrets and Spotify account ID when prompted.
4. If you have local data to keep, set `DATABASE_URL` in the root `.env` and
   run this once from `backend/`, before the first sign-in to the deployed app:

   ```sh
   python migrate_sqlite_to_postgres.py \
     --source data/aftertone.db \
     --spotify-user-id YOUR_SPOTIFY_ACCOUNT_ID
   ```

   To import an account-specific SQLite database, use
   `--source data/accounts`; the importer selects the file for that Spotify
   account. The import is safe to retry before the cloud app starts collecting
   data, but do not rerun it over an active cloud database.
5. After Render is healthy, open the HTTPS app, sign in, and save your listening
   profiles. On iPhone, open the app in Safari and select **Share → Add to Home
   Screen**. Sign in with the same Spotify account on each device.

Render's free service may sleep and Supabase's free database may pause after
inactivity. OAuth sessions are held in memory and may require reconnecting after
a service restart. Listening data persists in Postgres, but free hosting is not
a backup; export important data regularly.

## Privacy and limitations

Spotify access and refresh tokens stay on the backend and are held in memory.
Listening data and preferences are isolated by Spotify account. Search text is
not stored unless optional search logging is enabled. Location is only used
while the app is open; Aftertone does not support background location tracking.

Spotify does not attach locations or actual play duration to listening events.
Past plays need manual place tags, while work hours can be inferred from their
timestamps. Recommendation profiles are ranking signals only and can only boost
tracks already present in synced listening history.
