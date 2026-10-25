import { useEffect, useState } from 'react'

const DEFAULT_SETTINGS = {
  work_start_hour: 8,
  work_end_hour: 16,
  work_weekdays: [0, 1, 2, 3, 4],
}

const WEEKDAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
const DATE_RANGES = [
  { days: 7, label: 'Last 7 days' },
  { days: 30, label: 'Last 30 days' },
  { days: 90, label: 'Last 90 days' },
  { days: 0, label: 'All synced history' },
]

function BarList({ items, labelKey, valueKey, emptyMessage }) {
  const maximum = Math.max(...items.map((item) => item[valueKey]), 1)
  if (!items.length) return <p className="status-message">{emptyMessage}</p>
  return (
    <div className="stats-bars">
      {items.map((item) => (
        <div className="stats-bar-row" key={item[labelKey]}>
          <span>{item[labelKey]}</span>
          <div className="stats-bar-track">
            <div className="stats-bar-fill" style={{ width: `${(item[valueKey] / maximum) * 100}%` }} />
          </div>
          <strong>{item[valueKey]}</strong>
        </div>
      ))}
    </div>
  )
}

function Stats({ connected, historyVersion }) {
  const [stats, setStats] = useState(null)
  const [places, setPlaces] = useState([])
  const [plays, setPlays] = useState([])
  const [storedPlayCount, setStoredPlayCount] = useState(0)
  const [context, setContext] = useState('all')
  const [days, setDays] = useState(30)
  const [historyOffset, setHistoryOffset] = useState(0)
  const [settings, setSettings] = useState(DEFAULT_SETTINGS)
  const [error, setError] = useState('')
  const [status, setStatus] = useState('')
  const [refreshVersion, setRefreshVersion] = useState(0)
  const [genreDrafts, setGenreDrafts] = useState({})
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    if (!connected) return undefined
    let cancelled = false
    const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'
    Promise.all([
      fetch(`/api/stats?timezone=${encodeURIComponent(timezone)}&context=${encodeURIComponent(context)}&days=${days}`),
      fetch('/api/places'),
      fetch(`/api/history?limit=100&offset=${historyOffset}`),
      fetch('/api/stats/settings'),
    ]).then(async ([statsResponse, placesResponse, historyResponse, settingsResponse]) => {
      const [statsResult, placesResult, historyResult, settingsResult] = await Promise.all([
        statsResponse.json(),
        placesResponse.json(),
        historyResponse.json(),
        settingsResponse.json(),
      ])
      if (!statsResponse.ok) throw new Error(statsResult.detail || 'Could not load listening stats.')
      if (!placesResponse.ok) throw new Error(placesResult.detail || 'Could not load saved places.')
      if (!historyResponse.ok) throw new Error(historyResult.detail || 'Could not load listening history.')
      if (!settingsResponse.ok) throw new Error(settingsResult.detail || 'Could not load work-hour settings.')
      if (!cancelled) {
        setStats(statsResult)
        setPlaces(placesResult.places)
        setPlays(historyResult.plays)
        setStoredPlayCount(historyResult.stats.play_count)
        setSettings(settingsResult.settings)
        setError('')
      }
    }).catch((loadError) => {
      if (!cancelled) setError(loadError.message)
    })
    return () => { cancelled = true }
  }, [connected, context, days, historyOffset, historyVersion, refreshVersion])

  async function tagPlay(playId, nextPlaceId) {
    try {
      const response = await fetch(`/api/plays/${playId}/location`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ place_id: nextPlaceId ? Number(nextPlaceId) : null }),
      })
      const result = await response.json()
      if (!response.ok) throw new Error(result.detail || 'Could not update the place for that play.')
      setStatus('Location updated. Stats refreshed.')
      setRefreshVersion((version) => version + 1)
      setError('')
    } catch (tagError) {
      setError(tagError.message)
    }
  }

  async function saveWorkSchedule(event) {
    event.preventDefault()
    setSaving(true)
    setError('')
    setStatus('')
    try {
      const response = await fetch('/api/stats/settings', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(settings),
      })
      const result = await response.json()
      if (!response.ok) throw new Error(result.detail || 'Could not save work hours.')
      setSettings(result.settings)
      setStatus('Work schedule saved. Stats refreshed.')
      setRefreshVersion((version) => version + 1)
    } catch (saveError) {
      setError(saveError.message)
    } finally {
      setSaving(false)
    }
  }

  async function saveGenres(artist) {
    const genres = (genreDrafts[artist.id] ?? artist.genres.join(','))
      .split(',')
      .map((genre) => genre.trim())
      .filter(Boolean)
    setError('')
    setStatus('')
    try {
      const response = await fetch('/api/artist-genres', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          artist_id: artist.id,
          artist_name: artist.name,
          genres,
        }),
      })
      const result = await response.json()
      if (!response.ok) throw new Error(result.detail || `Could not save genres for ${artist.name}.`)
      setStatus(`Genres saved for ${artist.name}.`)
      setRefreshVersion((version) => version + 1)
    } catch (saveError) {
      setError(saveError.message)
    }
  }

  if (!connected) {
    return <section><h2>Stats</h2><p>Connect Spotify to see listening patterns from your synced history.</p></section>
  }

  const maxHourlyCount = Math.max(...(stats?.hours || []).map((item) => item.play_count), 1)
  const tagged = stats?.location_coverage.tagged_plays || 0
  const untagged = stats?.location_coverage.untagged_plays || 0
  return (
    <section className="stats-page">
      <h2>Listening stats</h2>
      <p>{stats ? `Showing ${stats.context.name.toLowerCase()} from synced history in your local time zone (${stats.timezone}).` : 'Patterns from your synced listening history.'}</p>
      {error && <p className="status-message error-message" role="alert">{error}</p>}
      {status && <p className="status-message" role="status">{status}</p>}
      {!stats && !error && <p className="status-message">Loading your listening history…</p>}
      {stats && (
        <>
          <div className="stats-filter-row">
            <label className="field context-place stats-context">
              Location / work context
              <select value={context} onChange={(event) => setContext(event.target.value)}>
                <option value="all">All listening</option>
                <option value="work-hours">Work hours</option>
                <option value="outside-work-hours">Outside work hours</option>
                {places.map((place) => <option key={place.id} value={`place:${place.id}`}>{place.name}</option>)}
              </select>
            </label>
            <label className="field context-place stats-context">
              Date range
                <select value={days} onChange={(event) => {
                  setDays(Number(event.target.value))
                  setHistoryOffset(0)
                }}>
                {DATE_RANGES.map((range) => <option key={range.days} value={range.days}>{range.label}</option>)}
              </select>
            </label>
          </div>
          <div className="stats-overview">
            <article className="stat-card"><h3>Synced plays</h3><p className="stat-value">{stats.play_count}</p></article>
            <article className="stat-card"><h3>Full-track runtime estimate</h3><p className="stat-value">{stats.estimated_full_track_minutes} min</p></article>
            <article className="stat-card"><h3>Top artist</h3><p className="stat-value">{stats.top_artists[0]?.name || 'Not enough data'}</p></article>
            <article className="stat-card"><h3>History range</h3><p className="stat-value stats-range">{stats.first_played_at ? `${new Date(stats.first_played_at).toLocaleDateString()} – ${new Date(stats.last_played_at).toLocaleDateString()}` : 'No plays yet'}</p></article>
          </div>
          <p className="stats-caption">Runtime is catalog track length multiplied by play count ({stats.duration_metadata_play_count} of {stats.play_count} plays have track-length metadata)—not measured minutes listened. Skips and early stops aren’t available from this Spotify history feed.</p>
          <article className="stat-card location-coverage">
            <h3>Location coverage in this date range</h3>
            <p><strong>{tagged}</strong> tagged plays · <strong>{untagged}</strong> untagged plays · {stats.location_coverage.tagged_percent}% tagged</p>
            <p className="stats-caption">Only tagged plays can be attributed to a specific saved place. Work hours are a time-based estimate, not GPS proof.</p>
          </article>
          <div className="stats-grid">
            <article className="stat-card">
              <h3>{context === 'all' ? 'Where / when' : 'Selected context'}</h3>
              <p className="stats-caption">Saved places take precedence. Other plays use your configured workdays and hours.</p>
              <BarList items={stats.contexts} labelKey="name" valueKey="play_count" emptyMessage="No plays in this selection." />
            </article>
            <article className="stat-card">
              <h3>By weekday</h3>
              <BarList items={stats.weekdays} labelKey="day" valueKey="play_count" emptyMessage="No weekday data in this selection." />
            </article>
            <article className="stat-card stats-hour-card">
              <h3>By hour</h3>
              <div className="hour-chart" role="img" aria-label="Listening plays by local hour">
                {stats.hours.map((item) => (
                  <div className="hour-column" key={item.hour} title={`${item.hour}:00 — ${item.play_count} plays`}>
                    <div className="hour-bar-track">
                      <div className="hour-bar-fill" style={{ height: `${(item.play_count / maxHourlyCount) * 100}%` }} />
                    </div>
                    {item.hour % 3 === 0 && <span>{String(item.hour).padStart(2, '0')}</span>}
                  </div>
                ))}
              </div>
            </article>
            <article className="stat-card">
              <h3>Top artists</h3>
              <BarList items={stats.top_artists} labelKey="name" valueKey="play_count" emptyMessage="No artists in this selection." />
            </article>
            <article className="stat-card">
              <h3>Genres</h3>
              <BarList items={stats.top_genres} labelKey="name" valueKey="play_count" emptyMessage="No genre data available yet. Add genre labels to artists below." />
              <p className="stats-caption">Genres are descriptive labels, not definitive classifications. A play can count toward several genres.</p>
            </article>
          </div>
          <article className="stat-card stats-top-tracks">
            <h3>Most played tracks</h3>
            {stats.top_tracks.length ? (
              <div className="recently-played">
                {stats.top_tracks.map((track) => (
                  <article className="track-row" key={track.id}>
                    {track.image ? <img src={track.image} alt="" /> : <div className="track-placeholder" aria-hidden="true" />}
                    <div className="track-details"><p className="track-name">{track.name}</p><p>{track.artists.join(', ')}</p></div>
                    <span className="track-play-count">{track.play_count} {track.play_count === 1 ? 'play' : 'plays'}</span>
                  </article>
                ))}
              </div>
            ) : <p className="status-message">No plays in this selection.</p>}
          </article>

          <details className="place-setup">
            <summary>Set your workdays and work hours</summary>
            <p>Untagged plays in this local time window count as Work hours. A saved place tag always takes priority.</p>
            <form className="work-schedule-form" onSubmit={saveWorkSchedule}>
              <fieldset className="weekday-picker">
                <legend>Workdays</legend>
                {WEEKDAYS.map((day, index) => (
                  <label key={day}>
                    <input
                      type="checkbox"
                      checked={settings.work_weekdays.includes(index)}
                      onChange={(event) => setSettings((current) => ({
                        ...current,
                        work_weekdays: event.target.checked
                          ? [...current.work_weekdays, index].sort()
                          : current.work_weekdays.filter((weekday) => weekday !== index),
                      }))}
                    />
                    {day}
                  </label>
                ))}
              </fieldset>
              <label className="field">Start hour
                <select value={settings.work_start_hour} onChange={(event) => setSettings((current) => ({ ...current, work_start_hour: Number(event.target.value) }))}>
                  {Array.from({ length: 24 }, (_, hour) => <option key={hour} value={hour}>{String(hour).padStart(2, '0')}:00</option>)}
                </select>
              </label>
              <label className="field">End hour
                <select value={settings.work_end_hour} onChange={(event) => setSettings((current) => ({ ...current, work_end_hour: Number(event.target.value) }))}>
                  {Array.from({ length: 24 }, (_, index) => index + 1).map((hour) => <option key={hour} value={hour}>{String(hour % 24).padStart(2, '0')}:00</option>)}
                </select>
              </label>
              <button className="connect-button" type="submit" disabled={saving || !settings.work_weekdays.length || settings.work_start_hour >= settings.work_end_hour}>
                {saving ? 'Saving…' : 'Save work schedule'}
              </button>
            </form>
          </details>

          <details className="place-setup">
            <summary>Tag or correct locations in recent plays</summary>
            <p>Untagged means Aftertone has no saved place for that play. Select a place where you know you were.</p>
            <div className="recently-played">
              {plays.slice(0, 100).map((play) => (
                <article className="track-row" key={play.id}>
                  {play.image ? <img src={play.image} alt="" /> : <div className="track-placeholder" aria-hidden="true" />}
                  <div className="track-details">
                    <p className="track-name">{play.name}</p>
                    <p>{play.artists.join(', ')} · {new Date(play.played_at).toLocaleString()}</p>
                  </div>
                  <label className="place-select">
                    <span className="sr-only">Location for {play.name}</span>
                    <select value={play.place_id || ''} onChange={(event) => void tagPlay(play.id, event.target.value)}>
                      <option value="">Untagged / use work-hours rule</option>
                      {places.map((place) => <option key={place.id} value={place.id}>{place.name}</option>)}
                    </select>
                  </label>
                </article>
              ))}
            </div>
            <div className="history-pagination">
              <button className="connect-button secondary" type="button" disabled={historyOffset === 0} onClick={() => setHistoryOffset((offset) => Math.max(offset - 100, 0))}>Newer plays</button>
              <span>{plays.length ? `Showing ${historyOffset + 1}–${historyOffset + plays.length}` : 'No more plays'} of {storedPlayCount} stored plays.</span>
              <button className="connect-button secondary" type="button" disabled={plays.length < 100} onClick={() => setHistoryOffset((offset) => offset + 100)}>Older plays</button>
            </div>
          </details>

          <details className="place-setup">
            <summary>Review and edit artist genres</summary>
            <p>{stats.artists_without_genres} artists in this selection have no genre labels. Spotify metadata can be missing; comma-separate your own labels.</p>
            {stats.artists.map((artist) => (
              <form
                className="genre-edit-row"
                key={artist.id}
                onSubmit={(event) => {
                  event.preventDefault()
                  void saveGenres(artist)
                }}
              >
                <label>{artist.name} · {artist.play_count} plays
                  <input
                    value={genreDrafts[artist.id] ?? artist.genres.join(', ')}
                    onChange={(event) => setGenreDrafts((drafts) => ({ ...drafts, [artist.id]: event.target.value }))}
                    placeholder="e.g. indie rock, dream pop"
                  />
                </label>
                <button className="connect-button secondary" type="submit">Save genres</button>
              </form>
            ))}
          </details>
          <p className="stats-caption">Mood is not collected or inferred. If you decide to track it later, a private, optional self-reported check-in is more meaningful than guessing mood from music.</p>
        </>
      )}
    </section>
  )
}

export default Stats
