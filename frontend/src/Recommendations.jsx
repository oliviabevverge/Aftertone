import { useCallback, useEffect, useRef, useState } from 'react'

function distanceKm(latitude1, longitude1, latitude2, longitude2) {
  const radians = (degrees) => (degrees * Math.PI) / 180
  const latitudeDelta = radians(latitude2 - latitude1)
  const longitudeDelta = radians(longitude2 - longitude1)
  const chord = Math.sin(latitudeDelta / 2) ** 2
    + Math.cos(radians(latitude1)) * Math.cos(radians(latitude2))
      * Math.sin(longitudeDelta / 2) ** 2
  return 6371 * 2 * Math.atan2(Math.sqrt(chord), Math.sqrt(1 - chord))
}

function currentContext() {
  const now = new Date()
  return {
    hour: now.getHours() + now.getMinutes() / 60,
    weekday: (now.getDay() + 6) % 7,
    timezone_offset_minutes: now.getTimezoneOffset(),
  }
}

function Recommendations({ connected, historyVersion }) {
  const [places, setPlaces] = useState([])
  const [placeId, setPlaceId] = useState('')
  const [recommendations, setRecommendations] = useState([])
  const [sessionId, setSessionId] = useState('')
  const [feedback, setFeedback] = useState({})
  const [feedbackBusy, setFeedbackBusy] = useState({})
  const [learningStatus, setLearningStatus] = useState(null)
  const [status, setStatus] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  const [locating, setLocating] = useState(false)
  const [liveTracking, setLiveTracking] = useState(false)
  const [locationStatus, setLocationStatus] = useState('')
  const [placeName, setPlaceName] = useState('')
  const [latitude, setLatitude] = useState('')
  const [longitude, setLongitude] = useState('')
  const selectedPlaceId = useRef('')

  useEffect(() => {
    if (!connected) return
    let cancelled = false
    fetch('/api/learning-status')
      .then(async (response) => {
        const result = await response.json()
        if (!response.ok) throw new Error(result.detail || 'Could not load learning progress.')
        if (!cancelled) setLearningStatus(result)
      })
      .catch((loadError) => {
        if (!cancelled) setError(loadError.message)
      })
    return () => { cancelled = true }
  }, [connected, feedback])

  async function readJson(response, fallback) {
    const result = await response.json().catch(() => ({}))
    if (!response.ok) throw new Error(result.detail || fallback)
    return result
  }

  const requestRecommendations = useCallback(async (nextPlaceId = selectedPlaceId.current) => {
    if (!connected) return
    setSessionId('')
    setLoading(true)
    setStatus('Ranking from your listening patterns…')
    setError('')
    try {
      const response = await fetch('/api/recommendations', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          ...currentContext(),
          place_id: nextPlaceId ? Number(nextPlaceId) : null,
          limit: 12,
        }),
      })
      const result = await readJson(response, 'Could not rank recommendations.')
      setRecommendations(result.recommendations)
      setSessionId(result.session_id)
      setFeedback({})
      setStatus(result.recommendations.length
        ? `Ranked automatically using song sequences, artist listening frequency, and time patterns (${result.ranking_model}).`
        : 'Sync more listening history to reveal recurring sequences and patterns.')
    } catch (requestError) {
      setError(requestError.message)
      setStatus('')
    } finally {
      setLoading(false)
    }
  }, [connected])

  function useCurrentLocation() {
    if (!navigator.geolocation) {
      setError('Location is not available in this browser.')
      return
    }
    setLocating(true)
    setError('')
    navigator.geolocation.getCurrentPosition(
      async ({ coords }) => {
        const nearest = places
          .map((place) => ({
            place,
            distance: distanceKm(coords.latitude, coords.longitude, place.latitude, place.longitude),
          }))
          .filter(({ place, distance }) => distance <= (place.radius_km ?? 0.4))
          .sort((a, b) => a.distance - b.distance)[0]?.place
        setLocating(false)
        if (nearest) {
          selectedPlaceId.current = String(nearest.id)
          setPlaceId(String(nearest.id))
          setLocationStatus(`Using saved place: ${nearest.name}. Precise coordinates were not saved.`)
          await requestRecommendations(String(nearest.id))
          return
        }
        setLatitude(coords.latitude.toFixed(6))
        setLongitude(coords.longitude.toFixed(6))
        setLocationStatus('No saved place matched nearby. Name this location below to save it as a place.')
        document.querySelector('.place-setup')?.scrollIntoView({ behavior: 'smooth', block: 'center' })
      },
      () => {
        setLocating(false)
        setError('Could not get your location. Allow location access in Safari settings and try again.')
      },
      { enableHighAccuracy: false, maximumAge: 60000, timeout: 15000 },
    )
  }

  async function recordFeedback(trackId, action) {
    if (!sessionId) return
    setFeedbackBusy((current) => ({ ...current, [trackId]: true }))
    setError('')
    try {
      const response = await fetch('/api/feedback', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_id: sessionId, track_id: trackId, action }),
      })
      await readJson(response, 'Could not save your feedback.')
      setFeedback((current) => ({ ...current, [trackId]: action }))
    } catch (feedbackError) {
      setError(feedbackError.message)
    } finally {
      setFeedbackBusy((current) => ({ ...current, [trackId]: false }))
    }
  }

  useEffect(() => {
    if (!connected) return
    let cancelled = false
    async function load() {
      try {
        const response = await fetch('/api/places')
        const result = await readJson(response, 'Could not load saved places.')
        if (!cancelled) {
          setPlaces(result.places)
          await requestRecommendations()
        }
      } catch (loadError) {
        if (!cancelled) setError(loadError.message)
      }
    }
    void load()
    return () => { cancelled = true }
  }, [connected, historyVersion, requestRecommendations])

  useEffect(() => {
    if (!connected || !liveTracking) return undefined
    if (!navigator.geolocation) return undefined
    let watchId = null
    let timerId = null
    let latestPosition = null
    let afterMs = Date.now()
    let syncing = false

    function stop() {
      if (watchId !== null) navigator.geolocation.clearWatch(watchId)
      if (timerId !== null) window.clearInterval(timerId)
      watchId = null
      timerId = null
    }

    async function sync() {
      if (!latestPosition || syncing || document.hidden) return
      syncing = true
      const nextAfterMs = Date.now()
      try {
        const response = await fetch('/api/location/sync', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            after_ms: afterMs,
            latitude: latestPosition.latitude,
            longitude: latestPosition.longitude,
          }),
        })
        const result = await readJson(response, 'Could not sync location with listening history.')
        afterMs = nextAfterMs
        setLocationStatus(result.place
          ? `Location active near ${result.place.name}; tagged ${result.tagged} new plays.`
          : 'Location is on; plays are only tagged near a saved place.')
      } catch (syncError) {
        setError(syncError.message)
        setLiveTracking(false)
      } finally {
        syncing = false
      }
    }

    function start() {
      if (document.hidden || watchId !== null) return
      afterMs = Date.now()
      watchId = navigator.geolocation.watchPosition(
        (position) => {
          latestPosition = {
            latitude: position.coords.latitude,
            longitude: position.coords.longitude,
          }
          setLocationStatus('Location is active while this page is open.')
          if (timerId === null) {
            void sync()
            timerId = window.setInterval(() => void sync(), 120000)
          }
        },
        () => {
          setError('Location access failed. Allow it in browser settings or turn live location off.')
          setLiveTracking(false)
        },
        { enableHighAccuracy: false, maximumAge: 60000, timeout: 15000 },
      )
    }

    function visibilityChanged() {
      if (document.hidden) {
        stop()
        latestPosition = null
        setLocationStatus('Location paused while this page is hidden.')
      } else {
        start()
      }
    }

    start()
    document.addEventListener('visibilitychange', visibilityChanged)
    return () => {
      stop()
      document.removeEventListener('visibilitychange', visibilityChanged)
    }
  }, [connected, liveTracking])

  async function savePlace(event) {
    event.preventDefault()
    const parsedLatitude = Number(latitude)
    const parsedLongitude = Number(longitude)
    if (!placeName.trim() || !Number.isFinite(parsedLatitude) || !Number.isFinite(parsedLongitude)) {
      setError('Enter a place name and valid latitude and longitude.')
      return
    }
    try {
      const response = await fetch('/api/places', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name: placeName.trim(),
          latitude: parsedLatitude,
          longitude: parsedLongitude,
          radius_km: 0.4,
        }),
      })
      const result = await readJson(response, 'Could not save that place.')
      const nextPlaces = [...places, result.place].sort((a, b) => a.name.localeCompare(b.name))
      setPlaces(nextPlaces)
      selectedPlaceId.current = String(result.place.id)
      setPlaceId(String(result.place.id))
      setPlaceName('')
      setLatitude('')
      setLongitude('')
      await requestRecommendations(String(result.place.id))
    } catch (saveError) {
      setError(saveError.message)
    }
  }

  if (!connected) {
    return (
      <section>
        <h2>For You</h2>
        <p>Connect Spotify to automatically rank tracks from your listening patterns.</p>
      </section>
    )
  }

  return (
    <section className="discovery-page">
      <div className="section-heading">
        <div>
          <h2>For You</h2>
          <p>Tracks are ranked automatically from songs that tend to follow one another, artists you replay, and when you listen. No ratings or hand-picked seed songs required.</p>
        </div>
        <button className="connect-button secondary" type="button" onClick={() => void requestRecommendations()} disabled={loading}>
          {loading ? 'Ranking…' : 'Refresh ranking'}
        </button>
      </div>

      {error && <p className="status-message error-message" role="alert">{error}</p>}
      {status && <p className="status-message">{status}</p>}
      <label className="field context-place">
        Listening context
        <select
          value={placeId}
          onChange={(event) => {
            selectedPlaceId.current = event.target.value
            setPlaceId(event.target.value)
            void requestRecommendations(event.target.value)
          }}
        >
          <option value="">Current time, any place</option>
          {places.map((place) => <option key={place.id} value={place.id}>{place.name}</option>)}
        </select>
      </label>
      <div className="location-check-in">
        <button className="connect-button secondary" type="button" onClick={useCurrentLocation} disabled={locating}>
          {locating ? 'Checking location…' : 'Use my current location'}
        </button>
        <p className="location-privacy">Uses GPS once to match a saved place. Exact coordinates are not sent to Aftertone unless you save a new place.</p>
      </div>
      <label className="live-location-toggle">
        <input
          type="checkbox"
          checked={liveTracking}
          onChange={(event) => {
            setError('')
            if (event.target.checked && !navigator.geolocation) {
              setError('Location is not available in this browser.')
              return
            }
            setLiveTracking(event.target.checked)
            setLocationStatus(event.target.checked ? 'Waiting for location permission…' : 'Live location is off.')
          }}
        />
        Tag plays near saved places while For You is open
      </label>
      <p className="location-privacy">Optional. Tracking pauses when this page is hidden. Raw GPS coordinates are not saved on play events.</p>
      {locationStatus && <p className="status-message">{locationStatus}</p>}

      <div className="recommendation-list">
        {recommendations.map((item, index) => (
          <article className="recommendation-card" key={item.track.id}>
            <span className="recommendation-rank">{String(index + 1).padStart(2, '0')}</span>
            {item.track.image
              ? <img src={item.track.image} alt="" />
              : <div className="track-placeholder" aria-hidden="true" />}
            <div className="track-details">
              <p className="track-name">{item.track.name}</p>
              <p>{item.track.artists.join(', ')}</p>
              <p className="recommendation-reason">{item.why}</p>
            </div>
            <div className="recommendation-actions">
              {item.track.url && <a href={item.track.url} target="_blank" rel="noreferrer">Open in Spotify</a>}
              <div className="feedback-actions" aria-label={`Feedback for ${item.track.name}`}>
                {[
                  ['played', 'Played'],
                  ['liked', 'Like'],
                  ['not_for_me', 'Not for me'],
                ].map(([action, label]) => (
                  <button
                    className={`feedback-button${feedback[item.track.id] === action ? ' selected' : ''}`}
                    key={action}
                    type="button"
                    disabled={Boolean(!sessionId || feedback[item.track.id] || feedbackBusy[item.track.id])}
                    aria-pressed={feedback[item.track.id] === action}
                    onClick={() => void recordFeedback(item.track.id, action)}
                  >
                    {feedbackBusy[item.track.id] ? 'Saving…' : feedback[item.track.id] === action ? 'Saved' : label}
                  </button>
                ))}
              </div>
            </div>
          </article>
        ))}
      </div>

      <details className="learning-progress">
        <summary>ML learning progress</summary>
        {learningStatus ? (
          <>
            <p>
              {learningStatus.training_examples} labeled recommendations collected
              across {learningStatus.training_sessions} labeled recommendation sessions.
              Training needs at least {learningStatus.minimum_training_examples} labels
              across {learningStatus.minimum_training_sessions} sessions, including both
              positive feedback (Played or Like) and negative feedback (Not for me).
            </p>
            <p className="stats-caption">
              Feedback is saved with the recommendation’s time and selected place. Only
              label recommendations you actually listened to or have a clear opinion about.
            </p>
          </>
        ) : <p className="stats-caption">Loading your feedback totals…</p>}
      </details>

      <details className="place-setup">
        <summary>Add a saved place</summary>
        <p>Save named places so recommendations and feedback can be associated with context. Use current location to fill coordinates, or enter them manually.</p>
        <form className="place-form place-coordinate-form" onSubmit={savePlace}>
          <label>Place name<input value={placeName} onChange={(event) => setPlaceName(event.target.value)} placeholder="e.g. Gym" required /></label>
          <label>Latitude<input type="number" min="-90" max="90" step="any" value={latitude} onChange={(event) => setLatitude(event.target.value)} required /></label>
          <label>Longitude<input type="number" min="-180" max="180" step="any" value={longitude} onChange={(event) => setLongitude(event.target.value)} required /></label>
          <button className="connect-button secondary" type="button" onClick={useCurrentLocation} disabled={locating}>
            Fill from current location
          </button>
          <button className="connect-button" type="submit">Save place</button>
        </form>
      </details>

      <p className="discovery-limits">The current Spotify playback feed does not include dependable genre tags or Bluetooth connection names. For now, ranking uses your saved sequences, artist frequency, time, and saved-place labels; recommendations come from tracks already in your synced history.</p>
    </section>
  )
}

export default Recommendations
