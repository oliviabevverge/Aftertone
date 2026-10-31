# Copilot instructions for Aftertone

## Project structure

- `backend/app/main.py`: FastAPI routes, Spotify OAuth, sessions, and account
  resolution.
- `backend/app/store.py`: SQLite/PostgreSQL persistence and schema management.
- `backend/app/recommender.py`: rule-based ranking and listening-profile boosts.
- `backend/app/learned_ranker.py`: optional learned reranking.
- `backend/tests/`: backend unit and API tests.
- `frontend/src/`: React app, feature views, and styles.
- `Dockerfile` and `render.yaml`: single-origin Render deployment.

## Development and validation

Run the backend from `backend/`:

```sh
python -m pip install -r requirements.txt
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8888
python -m unittest discover -s tests -q
```

Run the frontend from `frontend/`:

```sh
npm ci
npm run dev -- --host 127.0.0.1
npm run lint
npm run build
```

GitHub Actions runs the backend tests and frontend lint/build checks on pushes
and pull requests.

## Architecture and conventions

- Local development uses a SQLite database under `backend/data/`. Hosted mode
  uses `DATABASE_URL` and a separate PostgreSQL schema per Spotify account.
- Spotify OAuth access and refresh tokens are held in backend memory. The
  frontend uses the HttpOnly session cookie and must not read tokens.
- Keep secrets in the root `.env` for local work and in the hosting provider's
  secret settings for deployment. Never commit credentials or user data.
- Search logging is opt-in. Preserve that privacy behavior when changing search
  features.
- Timestamp and timezone handling affect stats and recommendations; keep
  datetimes timezone-aware and preserve the existing user timezone behavior.
- Recommendations work without the optional learned model. Keep the
  rule-based ranking path available if the model artifact is missing.
- Keep changes within the existing feature boundaries and run the narrowest
  relevant test/check before broader validation.
