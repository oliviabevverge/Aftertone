import './App.css'
import { useEffect, useMemo, useState } from 'react'
import { Vibrant } from 'node-vibrant/browser'
import Recommendations from './Recommendations.jsx'
import Search from './Search.jsx'
import Stats from './Stats.jsx'

const SECTIONS = [
  { id: 'overview', label: 'Overview' },
  { id: 'listening', label: 'Listening' },
  { id: 'stats', label: 'Stats' },
  { id: 'favorites', label: 'Favorites' },
  { id: 'discover', label: 'For You' },
]

function readableInk(hex) {
  const channels = hex.match(/[a-f\d]{2}/gi)?.map((channel) => parseInt(channel, 16) / 255) || [0, 0, 0]
  const luminance = channels
    .map((channel) => (channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4))
    .reduce((sum, channel, index) => sum + channel * [0.2126, 0.7152, 0.0722][index], 0)
  return luminance > 0.42 ? '#171717' : '#ffffff'
}

function formatArtists(track) {
  return track.artists?.join(', ') || 'Unknown artist'
}

function TrackRows({ tracks, emptyMessage, saved = false }) {
  if (!tracks.length) return <p className="status-message">{emptyMessage}</p>
  return (
    <div className="recently-played">
      {tracks.map((track, index) => (
        <article className="track-row" key={`${track.id}-${track.played_at || track.saved_at || index}`}>
          {track.image
            ? <img src={track.image} alt="" />
            : <div className="track-placeholder" aria-hidden="true" />}
          <div className="track-details">
            <p className="track-name">{track.name}</p>
            <p>{formatArtists(track)}{track.album ? ` · ${track.album}` : ''}</p>
          </div>
          {track.play_count > 0 ? (
            <span className="track-play-count">{track.play_count} {track.play_count === 1 ? 'play' : 'plays'}</span>
          ) : (track.played_at || track.saved_at) && (
            <time dateTime={track.played_at || track.saved_at}>
              {new Date(track.played_at || track.saved_at).toLocaleString()}
            </time>
          )}
          {track.url && (
            <a href={track.url} target="_blank" rel="noreferrer">
              {saved ? 'Open' : 'Open in Spotify'}
            </a>
          )}
        </article>
      ))}
    </div>
  )
}

function App() {
  const [tracks, setTracks] = useState([])
  const [plays, setPlays] = useState([])
  const [favorites, setFavorites] = useState(null)
  const [favoriteCount, setFavoriteCount] = useState(0)
  const [favoritePeriod, setFavoritePeriod] = useState(7)
  const [loading, setLoading] = useState(false)
  const [syncMessage, setSyncMessage] = useState('')
  const [lastSync, setLastSync] = useState(null)
  const [savedPlayCount, setSavedPlayCount] = useState(0)
  const [historyVersion, setHistoryVersion] = useState(0)
  const [connected, setConnected] = useState(false)
  const [spotifyAccountId, setSpotifyAccountId] = useState('')
  const [section, setSection] = useState(() => {
    const hash = window.location.hash.slice(1)
    return SECTIONS.some((item) => item.id === hash) ? hash : 'overview'
  })
  const [selectedSearchTrack, setSelectedSearchTrack] = useState('')
  const [error, setError] = useState(() => (
    new URLSearchParams(window.location.search).has('spotify_error')
      ? 'Spotify authorization was cancelled. You can try connecting again.'
      : ''
  ))

  const latestTrack = tracks[0] || plays[0]
  const weeklyPlays = useMemo(() => {
    const cutoff = Date.now() - 7 * 24 * 60 * 60 * 1000
    return plays.filter((play) => new Date(play.played_at).getTime() >= cutoff)
  }, [plays])
  const topArtist = useMemo(() => {
    const counts = new Map()
    for (const play of weeklyPlays) {
      for (const artist of play.artists || []) counts.set(artist, (counts.get(artist) || 0) + 1)
    }
    return [...counts.entries()].sort((a, b) => b[1] - a[1])[0]?.[0] || 'Not enough data yet'
  }, [weeklyPlays])

  useEffect(() => {
    const params = new URLSearchParams(window.location.search)
    if (params.has('spotify_connected') || params.has('spotify_error')) {
      window.history.replaceState({}, '', window.location.pathname)
    }
    void syncHistory()
  }, [])

  useEffect(() => {
    const imageUrl = latestTrack?.image
    const setFallback = () => {
      document.documentElement.style.setProperty('--accent1', '#d81e5b')
      document.documentElement.style.setProperty('--accent2', '#ef8f58')
      document.documentElement.style.setProperty('--accent-ink', '#ffffff')
    }
    if (!imageUrl) {
      setFallback()
      return undefined
    }
    let cancelled = false
    Vibrant.from(imageUrl).getPalette()
      .then((palette) => {
        if (cancelled) return
        const primary = palette.Vibrant?.hex || palette.DarkVibrant?.hex || palette.Muted?.hex || '#d81e5b'
        const secondary = palette.LightVibrant?.hex || palette.LightMuted?.hex || palette.DarkMuted?.hex || '#ef8f58'
        document.documentElement.style.setProperty('--accent1', primary)
        document.documentElement.style.setProperty('--accent2', secondary)
        document.documentElement.style.setProperty('--accent-ink', readableInk(primary))
      })
      .catch(() => {
        if (!cancelled) setFallback()
      })
    return () => {
      cancelled = true
    }
  }, [latestTrack?.image])

  useEffect(() => {
    if (!connected || section !== 'favorites') return undefined
    let cancelled = false
    fetch('/api/favorites?limit=50')
      .then(async (response) => {
        const result = await response.json()
        if (!response.ok) throw new Error(result.detail || 'Could not load your listening-frequency ranking.')
        if (!cancelled) {
          setFavorites(result.tracks)
          setFavoriteCount(result.total_plays)
          setFavoritePeriod(result.period_days)
          setError('')
        }
      })
      .catch((loadError) => {
        if (!cancelled) {
          setFavorites([])
          setError(loadError.message)
        }
      })
    return () => {
      cancelled = true
    }
  }, [connected, historyVersion, section])

  async function syncHistory() {
    setLoading(true)
    setError('')
    setSyncMessage('')
    try {
      const response = await fetch('/api/recently-played?days=30')
      if (response.status === 401) {
        setConnected(false)
        return
      }
      const result = await response.json()
      if (!response.ok) throw new Error(result.detail || 'Could not sync your Spotify listening history.')
      const historyResponse = await fetch('/api/history?limit=500')
      const historyResult = await historyResponse.json()
      if (!historyResponse.ok) throw new Error(historyResult.detail || 'Could not load saved listening history.')
      const accountResponse = await fetch('/api/account')
      const accountResult = await accountResponse.json()
      if (!accountResponse.ok) throw new Error(accountResult.detail || 'Could not load Spotify account information.')
      setTracks(result.tracks)
      setPlays(historyResult.plays)
      setSpotifyAccountId(accountResult.spotify_user_id)
      setConnected(true)
      setLastSync(result.last_sync_at)
      setSavedPlayCount(historyResult.stats?.play_count ?? result.play_count)
      setSyncMessage(
        result.page_limit_reached
          ? `Imported ${result.newly_saved} new plays from ${result.pages_fetched} pages; Spotify history import reached its safety limit.`
          : `Imported up to 30 days of Spotify history (${result.newly_saved} new plays across ${result.pages_fetched} pages).`,
      )
      setHistoryVersion((version) => version + 1)
    } catch (syncError) {
      setError(syncError.message)
    } finally {
      setLoading(false)
    }
  }

  function navigate(nextSection) {
    setSection(nextSection)
    if (nextSection === 'favorites') setFavorites(null)
    window.history.replaceState({}, '', `#${nextSection}`)
  }

  async function disconnect() {
    await fetch('/logout', { method: 'POST' })
    setConnected(false)
    setSpotifyAccountId('')
    setTracks([])
    setPlays([])
    setFavorites([])
    setSavedPlayCount(0)
    setLastSync(null)
    navigate('overview')
  }

  return (
    <div className="app-shell">
      <header className="app-header">
        <div>
          <h1>Aftertone</h1>
          <p>Your music, your patterns, your next favourite.</p>
        </div>
        {connected ? (
          <button className="connect-button secondary" type="button" onClick={disconnect}>Disconnect Spotify</button>
        ) : (
          <a className="connect-button" href="/login">Connect Spotify</a>
        )}
      </header>

      <div className="app-layout">
        <nav className="sidebar" aria-label="Main navigation">
          {SECTIONS.map((item) => (
            <button
              className={section === item.id ? 'active' : ''}
              key={item.id}
              type="button"
              aria-current={section === item.id ? 'page' : undefined}
              onClick={() => navigate(item.id)}
            >
              {item.label}
            </button>
          ))}
        </nav>

        <main className="page-content">
          {error && <p className="status-message error-message" role="alert">{error}</p>}
          {syncMessage && <p className="status-message" role="status">{syncMessage}</p>}
          {!connected ? (
            <section>
              <h2>Connect your Spotify</h2>
              <p>Aftertone only shows your music after you connect the Spotify account you listen with.</p>
              <p>After connecting, sync recent plays to populate Listening, Stats, Favorites, and automatic For You rankings.</p>
              {loading && <p className="status-message">Checking Spotify connection…</p>}
              <a className="connect-button" href="/login">Connect Spotify</a>
            </section>
          ) : (
            <>
              {section === 'overview' && (
                <section>
                  <h2>Your week in music</h2>
                  <p>A summary of the Spotify listening history you have synced with Aftertone.</p>
                  {spotifyAccountId && <p className="stats-caption">Spotify account ID: <code>{spotifyAccountId}</code></p>}
                  <div className="history-sync">
                    <span>{savedPlayCount} plays saved · Last sync {lastSync ? new Date(lastSync).toLocaleString() : 'not yet'}</span>
                    <button className="connect-button secondary" type="button" onClick={syncHistory} disabled={loading}>
                      {loading ? 'Importing recent history…' : 'Import last 30 days'}
                    </button>
                  </div>
                  <div className="stats-grid">
                    <article className="stat-card">
                      <h3>Recent plays</h3>
                      <p className="stat-value">{weeklyPlays.length}</p>
                      <p>Saved plays in the last 7 days</p>
                    </article>
                    <article className="stat-card">
                      <h3>Top artist</h3>
                      <p className="stat-value">{topArtist}</p>
                      <p>By recent play count</p>
                    </article>
                  </div>
                  {latestTrack && (
                    <div className="featured-listening">
                      <div className="album-art">
                        {latestTrack.image && <img src={latestTrack.image} alt={`${latestTrack.album} album cover`} />}
                      </div>
                      <div className="featured-text">
                        <p className="eyebrow">YOUR LATEST PLAY</p>
                        <h2>{latestTrack.name}</h2>
                        <p>{formatArtists(latestTrack)} · {latestTrack.album}</p>
                        {latestTrack.played_at && <p className="featured-note">{new Date(latestTrack.played_at).toLocaleString()}</p>}
                      </div>
                    </div>
                  )}
                  <TrackRows tracks={tracks.slice(0, 8)} emptyMessage="No recent tracks returned. Choose Sync Spotify history to try again." />
                </section>
              )}

              {section === 'listening' && (
                <section>
                  <h2>Listening history</h2>
                  <p>Plays saved locally from Spotify’s recently played feed. Spotify limits this feed; it is not your complete all-time history.</p>
                  <div className="history-sync">
                    <span>{plays.length} plays stored in Aftertone</span>
                    <button className="connect-button secondary" type="button" onClick={syncHistory} disabled={loading}>
                      {loading ? 'Importing recent history…' : 'Import last 30 days'}
                    </button>
                  </div>
                  <TrackRows tracks={plays} emptyMessage="No plays have been synced yet. Select Import last 30 days." />
                </section>
              )}

              {section === 'favorites' && (
                <section>
                  <h2>Your most-listened tracks</h2>
                  <p>{favoriteCount} synced plays in the past {favoritePeriod} days, ranked by listens. Aftertone only counts plays it has synced.</p>
                  {favorites === null
                    ? <p className="status-message">Counting plays from your listening history…</p>
                    : <TrackRows tracks={favorites} saved emptyMessage="No synced plays from the past 7 days yet. Go to Listening and sync your Spotify history." />}
                </section>
              )}

              {section === 'stats' && <Stats connected={connected} historyVersion={historyVersion} />}

              {section === 'discover' && (
                <section>
                  <Recommendations connected={connected} historyVersion={historyVersion} />
                  <details className="manual-search">
                    <summary>Search Spotify manually</summary>
                    <Search connected={connected} onTrackSelected={(track) => setSelectedSearchTrack(`${track.name} · ${formatArtists(track)}`)} />
                    {selectedSearchTrack && <p className="status-message">Selected: {selectedSearchTrack}</p>}
                  </details>
                </section>
              )}
            </>
          )}
        </main>
      </div>
    </div>
  )
}

export default App
