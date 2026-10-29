import { useEffect, useId, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { holdModalFocus, wrapTabFocus } from './modalFocus.js'
import { runnerErrorMessage, runnerFetch } from './runnerApi.js'

async function fetchJson(url) {
  const response = await runnerFetch(url)
  const payload = await response.json().catch(() => ({}))
  if (!response.ok) {
    throw new Error(runnerErrorMessage(payload, response.status, 'Request failed'))
  }
  return payload
}

/**
 * Modal path picker backed by the local runner filesystem API.
 * Browser pickers cannot expose absolute disk paths; the runner can.
 */
export default function PathPickerModal({
  title = 'Select path',
  mode = 'file',
  initialValue = '',
  onClose,
  onConfirm,
}) {
  const titleId = useId()
  const [draft, setDraft] = useState(initialValue)
  const [listing, setListing] = useState(null)
  const [roots, setRoots] = useState([])
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  const loadRequestRef = useRef(0)
  const dialogRef = useRef(null)
  const inputRef = useRef(null)

  useEffect(() => {
    // Not autoFocus: the opener must still hold focus here so it gets it back on close.
    const release = holdModalFocus(document.getElementById('root'), document)
    inputRef.current?.focus()
    return release
  }, [])

  const loadDir = async (path) => {
    loadRequestRef.current += 1
    const request = loadRequestRef.current
    setLoading(true)
    setError('')
    try {
      const params = new URLSearchParams()
      if (path) {
        params.set('path', path)
      }
      if (mode === 'folder') {
        params.set('directories_only', 'true')
      }
      const data = await fetchJson(`/api/fs/list?${params}`)
      if (request !== loadRequestRef.current) {
        return
      }
      setListing(data)
      if (mode === 'folder') {
        setDraft(data.path)
      }
    } catch (err) {
      if (request === loadRequestRef.current) {
        setError(err instanceof Error ? err.message : 'Could not list folder')
      }
    } finally {
      if (request === loadRequestRef.current) {
        setLoading(false)
      }
    }
  }

  useEffect(() => {
    const onKeyDown = (event) => {
      if (event.key === 'Escape') {
        onClose()
      }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [onClose])

  useEffect(() => {
    let cancelled = false
    ;(async () => {
      try {
        const data = await fetchJson('/api/fs/roots')
        if (!cancelled) {
          setRoots(data.roots || [])
        }
      } catch {
        if (!cancelled) {
          setRoots([])
        }
      }
      const start =
        initialValue && (initialValue.startsWith('/') || /^[A-Za-z]:[\\/]/.test(initialValue))
          ? initialValue
          : ''
      if (!cancelled) {
        await loadDir(start)
      }
    })()
    return () => {
      cancelled = true
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- load once per open
  }, [])

  const canConfirm =
    Boolean(draft.trim()) &&
    (mode === 'folder' || draft.trim().toLowerCase().endsWith('.xlsx'))

  return createPortal(
    <div className="modal-backdrop" role="presentation" onClick={onClose}>
      <div
        ref={dialogRef}
        className="modal-card path-picker-card"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
        onClick={(event) => event.stopPropagation()}
        onKeyDown={(event) => wrapTabFocus(event, dialogRef.current)}
      >
        <div className="modal-header">
          <h3 id={titleId}>{title}</h3>
          <button type="button" className="secondary-inline" onClick={onClose}>
            Close
          </button>
        </div>

        <p className="modal-help">
          Paths come from the local runner (absolute disk paths). Browser-only dialogs cannot
          provide a full filesystem location.
        </p>

        {roots.length > 0 ? (
          <div className="path-picker-roots">
            {roots.map((root) => (
              <button
                key={root.path}
                type="button"
                className="secondary-inline"
                onClick={() => loadDir(root.path)}
              >
                {root.label}
              </button>
            ))}
          </div>
        ) : null}

        <div className="path-picker-nav">
          <button
            type="button"
            className="secondary-inline"
            disabled={!listing?.parent || loading}
            onClick={() => loadDir(listing.parent)}
          >
            Up
          </button>
          <code className="path-picker-cwd">{listing?.path || '…'}</code>
        </div>

        <div className="path-picker-list" role="group" aria-label="Directory contents">
          {loading ? <p className="modal-note">Loading…</p> : null}
          {!loading && listing?.entries?.length === 0 ? (
            <p className="modal-note">No matching items in this folder.</p>
          ) : null}
          {!loading
            ? (listing?.entries || []).map((entry) => (
                <button
                  key={entry.path}
                  type="button"
                  className={`path-picker-entry ${entry.kind}`}
                  onClick={() => {
                    if (entry.kind === 'dir') {
                      loadDir(entry.path)
                      return
                    }
                    setDraft(entry.path)
                  }}
                  onDoubleClick={() => {
                    if (entry.kind === 'dir') {
                      loadDir(entry.path)
                      return
                    }
                    onConfirm(entry.path)
                  }}
                >
                  <span className="path-picker-kind">{entry.kind === 'dir' ? 'dir' : 'xlsx'}</span>
                  <span>{entry.name}</span>
                </button>
              ))
            : null}
        </div>

        <label>
          {mode === 'folder' ? 'Selected folder path' : 'Selected workbook path'}
          <input
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            placeholder={
              mode === 'folder'
                ? '/Users/you/BPC Files/2026-Q4'
                : '/Users/you/.../ADO template guideline.xlsx'
            }
            ref={inputRef}
          />
        </label>

        {error ? <p className="modal-note path-picker-error">{error}</p> : null}
        {mode === 'file' && draft && !draft.toLowerCase().endsWith('.xlsx') ? (
          <p className="modal-note">
            Template must be an <code>.xlsx</code> file (not a folder).
          </p>
        ) : null}

        <div className="modal-actions">
          {mode === 'folder' ? (
            <button
              type="button"
              className="secondary-inline"
              disabled={!listing?.path}
              onClick={() => setDraft(listing.path)}
            >
              Use this folder
            </button>
          ) : null}
          <button
            type="button"
            className="run-button"
            onClick={() => onConfirm(draft.trim())}
            disabled={!canConfirm}
          >
            Use path
          </button>
        </div>
      </div>
    </div>,
    document.body,
  )
}
