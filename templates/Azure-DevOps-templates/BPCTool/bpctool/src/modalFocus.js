const FOCUSABLE = [
  'a[href]',
  'button',
  'input',
  'select',
  'textarea',
  '[tabindex]',
].join(',')

export function focusableIn(container) {
  return Array.from(container.querySelectorAll(FOCUSABLE)).filter(
    (el) => !el.disabled && el.getAttribute('tabindex') !== '-1',
  )
}

/** Keeps Tab / Shift+Tab cycling inside `container`. */
export function wrapTabFocus(event, container) {
  if (event.key !== 'Tab' || !container) {
    return
  }
  const items = focusableIn(container)
  const active = container.ownerDocument.activeElement
  if (items.length === 0) {
    event.preventDefault()
    container.focus()
    return
  }
  const first = items[0]
  const last = items[items.length - 1]
  const outside = !container.contains(active)
  if (event.shiftKey && (outside || active === first || active === container)) {
    event.preventDefault()
    last.focus()
  } else if (!event.shiftKey && (outside || active === last)) {
    event.preventDefault()
    first.focus()
  }
}

/**
 * Makes `background` inert while a modal is open. The returned function undoes that and
 * gives focus back to whatever had it when the modal opened (e.g. the Browse button).
 */
export function holdModalFocus(background, doc = background?.ownerDocument) {
  const opener = doc?.activeElement ?? null
  const wasInert = Boolean(background?.inert)
  if (background) {
    background.inert = true
  }
  return () => {
    if (background) {
      background.inert = wasInert
    }
    if (opener && opener.isConnected && typeof opener.focus === 'function') {
      opener.focus()
    }
  }
}
