import { useEffect, useState } from 'react'

function Search({ connected, onTrackSelected }) {
  const [query, setQuery] = useState('')
  const [searchLogging, setSearchLogging] = useState(false)
  const [searchId, setSearchId] = useState(null)
  const [results, setResults] = useState([])
  const [placeId, setPlaceId] = useState('')
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState('')
  const [error, setError] = useState('')
  const [places, setPlaces] = useState([])

  useEffect(() => {
    if (!connected) return undefined
    let cancelled = false
    Promise.all([
      fetch('/api/recommender/settings').then(async (response) => {
        const result = await response.json()
        if (!response.ok) throw new Error(result.detail || 'Could not load search privacy settings.')
        return result
      }),
      fetch('/api/places').then(async (response) => {
        const result = await response.json()
        if (!response.ok) throw new Error(result.detail || 'Could not load saved places.')
        return result
      }),
    ])
      .then(([settings, savedPlaces]) => {
        if (cancelled) return
        setSearchLogging(Boolean(settings.settings.search_logging_enabled))
        setPlaces(savedPlaces.places)
      })
      .catch((loadError) => {
        if (!cancelled) setError(loadError.message)
      })
    return () => {
      cancelled = true
    }
  }, [connected])

  async function readJson(response, fallback) {
    const result = await response.json()
    if (!response.ok) throw new Error(result.detail || fallback)
    return result
  }

  async function updateSearchLogging(enabled) {
    setError('')
    try {
      const result = await readJson(await fetch('/api/recommender/settings', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ search_logging_enabled: enabled }),
      }), 'Could not update search privacy settings.')
      setSearchLogging(Boolean(result.settings.search_logging_enabled))
      if (!enabled) setSearchId(null)
      setMessage(enabled
        ? 'Search logging is on. New in-app searches will include their local time and selected saved place.'
        : 'Search logging is off. Previously collected searches are not deleted.')
    } catch (updateError) {
      setError(updateError.message)
    }
  }

  async function search(event) {
    event.preventDefault()
    if (!query.trim()) return
    setBusy(true)
    setError('')
    setMessage('')
    setSearchId(null)
    setResults([])
    try {
      const result = await readJson(await fetch('/api/spotify-search', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          query: query.trim(),
          place_id: searchLogging && placeId ? Number(placeId) : null,
        }),
      }), 'Spotify search failed.')
      setResults(result.tracks)
      setSearchId(result.search_id)
      setMessage(result.tracks.length
        ? `${result.tracks.length} tracks found${result.search_id ? '. Search saved for your opted-in learning data.' : '.'}`
        : 'No tracks found.')
    } catch (searchError) {
      setError(searchError.message)
    } finally {
      setBusy(false)
    }
  }

  async function selectTrack(track) {
    if (searchId) {
      try {
        await readJson(await fetch('/api/search-selection', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ search_id: searchId, track_id: track.id }),
        }), 'Could not record selected search result.')
      } catch (selectionError) {
        setError(selectionError.message)
        return
      }
    }
    onTrackSelected(track)
    setMessage(`Selected “${track.name}”.`)
  }

  async function deleteSearchHistory() {
    setError('')
    try {
      await readJson(await fetch('/api/search-history', { method: 'DELETE' }), 'Could not delete search history.')
      setSearchId(null)
      setMessage('Saved search history deleted.')
    } catch (deleteError) {
      setError(deleteError.message)
    }
  }

  if (!connected) {
    return (
      <section id="search">
        <h2>Spotify search</h2>
        <p>Connect Spotify to search for tracks and optionally record your searches.</p>
      </section>
    )
  }

  return (
    <section id="search">
      <h2>Spotify search</h2>
      <p>Search the Spotify catalog. Searches are private by default and are only logged when you opt in.</p>
      {error && <p className="status-message error-message" role="alert">{error}</p>}
      {message && <p className="status-message" role="status">{message}</p>}
      <form className="search-form" onSubmit={search}>
        <input
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          maxLength={200}
          placeholder="Search for a song or artist"
          aria-label="Search Spotify tracks"
        />
        <button className="connect-button" type="submit" disabled={busy}>
          {busy ? 'Searching…' : 'Search'}
        </button>
      </form>
      <label className="live-location-toggle">
        <input
          type="checkbox"
          checked={searchLogging}
          onChange={(event) => updateSearchLogging(event.target.checked)}
        />
        Opt in to log my Aftertone searches for recommendation research
      </label>
      {searchLogging && (
        <>
          <p className="location-privacy">
            Logs query text, local time, and an optional saved place—not precise GPS.
            Logging off stops new collection; use delete to clear saved searches.
          </p>
          <label className="field">
            Search context place
            <select value={placeId} onChange={(event) => setPlaceId(event.target.value)}>
              <option value="">No place label</option>
              {places.map((place) => (
                <option key={place.id} value={place.id}>{place.name}</option>
              ))}
            </select>
          </label>
          <button className="connect-button secondary" type="button" onClick={deleteSearchHistory}>
            Delete saved search history
          </button>
        </>
      )}
      {results.map((track) => (
        <article className="track-row" key={track.id}>
          {track.image ? <img src={track.image} alt="" /> : <div className="track-placeholder" aria-hidden="true" />}
          <div className="track-details">
            <p className="track-name">{track.name}</p>
            <p>{track.artists.join(', ')} · {track.album}</p>
          </div>
          <button className="connect-button secondary" type="button" onClick={() => selectTrack(track)}>
            Select
          </button>
          {track.url && <a href={track.url} target="_blank" rel="noreferrer">Open</a>}
        </article>
      ))}
    </section>
  )
}

export default Search
