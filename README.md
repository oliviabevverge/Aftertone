# Aftertone

Aftertone is a local Spotify listening-history dashboard. After you connect
Spotify, it stores your plays and can recommend what to play next from
**song sequences**, **artist frequency**, **time of day**, and **places you tag**.
Recommendations are automatically ranked; you do not need to rate tracks or
select a seed sequence.

## Spotify setup

1. Create or open an app in the [Spotify Developer Dashboard](https://developer.spotify.com/dashboard).
2. Add `http://127.0.0.1:8888/callback` as a Redirect URI in the app settings.
3. Create `Aftertone/.env` with the app's credentials:

   ```dotenv
   SPOTIFY_CLIENT_ID=your_client_id
   SPOTIFY_CLIENT_SECRET=your_client_secret
   SPOTIFY_REDIRECT_URI=http://127.0.0.1:8888/callback
   FRONTEND_URL=http://127.0.0.1:5173
   ```

   Keep the client secret private. Never put it in frontend code or commit `.env`.

## Run locally

In one terminal, install backend dependencies and start the API:

```sh
cd backend
python -m pip install -r requirements.txt
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8888
```

In another terminal, start the frontend:

```sh
cd frontend
npm install
npm run dev -- --host 127.0.0.1
```

Open <http://127.0.0.1:5173>, choose **Connect Spotify**, and approve access.
Use the React frontend URL, not the workspace-root `index.html` prototype.
To install Aftertone on iPhone, deploy the frontend and API to HTTPS, open the
site in Safari, tap **Share**, then **Add to Home Screen**. The installed app
shell can load offline, but Spotify, location lookups, and saved data still need
the backend and an internet connection. The local development server is not
reachable from an iPhone using `127.0.0.1`; deployment or a phone-accessible
HTTPS development tunnel is required.

## Free Render preview

The root `Dockerfile` and `render.yaml` configure a single Render web service
that builds and serves the frontend and API on the same HTTPS origin. To deploy,
push the project to a private GitHub repository, create a new Render Blueprint
from that repository, and provide `SPOTIFY_CLIENT_ID` and
`SPOTIFY_CLIENT_SECRET` when prompted. In the Spotify Developer Dashboard, add
`https://aftertone.onrender.com/callback` as a Redirect URI before connecting
Spotify. Keep the client secret in Render's environment settings, not in source.

This free preview uses temporary storage at `/tmp/aftertone.db`, may sleep while
idle, and keeps Spotify login sessions in memory. Its listening history,
feedback, and login can be lost when the service restarts; do not use it as the
only copy of data you want to keep. Persistent data and reliable availability
require a later hosting upgrade.
After Spotify redirects back, Aftertone paginates Spotify's recently-played endpoint
to import up to the last 30 days and saves those plays locally. Spotify must make
those events available through its API; this is not a guaranteed complete account
archive. **Import last 30 days** retries the import and reports how many pages and
new plays it saved. **Favorites** ranks synced play events from the last seven days
by listen count; it is not Spotify's saved-tracks library.

Open **Stats** to choose a date range (7/30/90 days or all synced history) and
filter All listening, Work hours, Outside work hours, or a saved place. The selected
view shows plays by weekday and hour, top artists, genres, tracks, runtime estimate,
and how many plays have a place tag. Configure workdays and same-day work hours
there too. Untagged plays in that local window are classified as Work hours; saved
place labels (for example Desk or Gym) take precedence over the time rule. Add
places from **For You** using their coordinates, then
enable optional live location while the page is open to tag new plays near those
places. Tracking pauses while the page is hidden and does not save raw GPS
coordinates. Use the recent-play controls on **Stats** to correct old location
tags manually; the list is paginated so you can work back through the imported
history. Use **Review and edit artist genres** to correct missing or inaccurate
labels. Genres are descriptive, editable tags, not objective classifications.
On **For You**, use **Use my current location** to match a nearby saved place
and rank for that context. Rate recommendations as **Played**, **Like**, or
**Not for me**; those labels are saved alongside the recommendation's time and
selected place for future training. Current-location matching is a one-time
foreground lookup; exact coordinates are not sent or retained unless you save
a new place. iPhone background location tracking is not supported by this web app.
Use **Import last 30 days** to bring new events into the local stats.
Spotify does not attach location to past plays: only work-hours are inferred from
timestamps; saved-place history must be tagged manually or captured by live tracking.
Minutes are estimated from each track's full catalog duration and can overstate
listening when you skip or stop early; Spotify's recently-played API does not report
actual play length or skips. Artist genres are fetched from Spotify's deprecated
artist endpoint when available, so genre metadata may not be returned.
Mood is not inferred or collected; comparing it requires separate self-reported
mood data.

**For You** automatically ranks tracks from your synced listening history using
recurring song sequences, artist frequency, and time patterns. Select a saved
place to see the ranking for that context. The playback feed does not provide
reliable genre labels or connected Bluetooth device names, so those are not used
yet; car listening is left unclassified. Recommendations are limited to tracks
already present in the synced history.

Each generated recommendation list is saved with its context and candidate scores.
Search in Aftertone is private by default; opt in to store query text, local time,
an optional saved-place label, and which search result you selected.
You can turn logging off and delete stored search history from the Search section.

## Train and evaluate the personal ranker

An optional logistic regression can be trained with gradient descent in
`backend/train_model.py`; it learns from legacy explicit feedback labels and
opted-in search affinity, includes artist frequency features, keeps a
time-ordered session holdout, and reports accuracy, log loss, and NDCG@5 against
the automatic rule-based ranker. The automatic ranker works without feedback.
Collect at least 20 labeled recommendations across five sessions, including both
positive and negative feedback in the older training and newer test sessions
before attempting supervised training.

From the project root, run:

```sh
cd backend
../.venv/bin/python train_model.py
```

The script writes `backend/data/recommender_model.json`. The API automatically
uses that model to rerank candidates; if no model exists, it keeps using the
rule-based baseline. Compare holdout metrics and listen to the recommendations
before trusting the model. Retrain after collecting more feedback; the JSON model
is a local artifact, not source code.

The backend uses an HttpOnly cookie for the session; access and refresh tokens
are not sent to frontend JavaScript. Saved plays live in `backend/data/aftertone.db`.

The current backend stores sessions in memory, so restarting it disconnects
existing sessions. Use **Disconnect Spotify** to clear your local session.
