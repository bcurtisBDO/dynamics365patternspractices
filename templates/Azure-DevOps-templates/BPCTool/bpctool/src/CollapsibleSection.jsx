import { useState } from 'react'

/**
 * Simple collapsible panel section. Defaults to expanded (uncollapsed).
 */
export default function CollapsibleSection({
  title,
  summary = null,
  defaultOpen = true,
  children,
}) {
  const [open, setOpen] = useState(defaultOpen)

  return (
    <section className="panel collapsible-panel">
      <h2 className="collapsible-heading">
        <button
          type="button"
          className="panel-heading-row collapsible-trigger"
          onClick={() => setOpen((value) => !value)}
          aria-expanded={open}
        >
          <span className="collapsible-title">{title}</span>
          <span className="collapse-meta">
            <span className="collapse-caret" aria-hidden="true">
              {open ? '▾' : '▸'}
            </span>
          </span>
        </button>
      </h2>

      {open ? (
        <div className="collapsible-body">{children}</div>
      ) : summary ? (
        <p className="collapsed-summary">{summary}</p>
      ) : null}
    </section>
  )
}
