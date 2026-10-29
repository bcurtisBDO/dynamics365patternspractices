import { describe, expect, it, vi } from 'vitest'
import { focusableIn, holdModalFocus, wrapTabFocus } from './modalFocus.js'

function fakeDom(specs) {
  const doc = { activeElement: null }
  const make = (name, attrs = {}) => ({
    name,
    disabled: Boolean(attrs.disabled),
    isConnected: true,
    ownerDocument: doc,
    getAttribute: (key) => attrs[key] ?? null,
    focus: vi.fn(function focus() {
      doc.activeElement = this
    }),
  })
  const items = specs.map(([name, attrs]) => make(name, attrs))
  const container = make('dialog', { tabindex: '-1' })
  container.querySelectorAll = () => items
  container.contains = (el) => el === container || items.includes(el)
  return { doc, container, items, make }
}

function tab(shiftKey = false) {
  return { key: 'Tab', shiftKey, preventDefault: vi.fn() }
}

describe('modal focus', () => {
  it('skips disabled and tabindex=-1 controls', () => {
    const { container } = fakeDom([['close'], ['up', { disabled: true }], ['skip', { tabindex: '-1' }], ['use']])
    expect(focusableIn(container).map((el) => el.name)).toEqual(['close', 'use'])
  })

  it('wraps Tab from the last control to the first and Shift+Tab back', () => {
    const { doc, container, items } = fakeDom([['close'], ['input'], ['use']])
    doc.activeElement = items[2]
    const forward = tab()
    wrapTabFocus(forward, container)
    expect(forward.preventDefault).toHaveBeenCalled()
    expect(doc.activeElement).toBe(items[0])

    const back = tab(true)
    wrapTabFocus(back, container)
    expect(back.preventDefault).toHaveBeenCalled()
    expect(doc.activeElement).toBe(items[2])
  })

  it('leaves Tab between inner controls to the browser', () => {
    const { doc, container, items } = fakeDom([['close'], ['input'], ['use']])
    doc.activeElement = items[0]
    const event = tab()
    wrapTabFocus(event, container)
    expect(event.preventDefault).not.toHaveBeenCalled()
    wrapTabFocus({ key: 'Enter', preventDefault: event.preventDefault }, container)
    expect(event.preventDefault).not.toHaveBeenCalled()
  })

  it('pulls focus that escaped the dialog back inside', () => {
    const { doc, container, items, make } = fakeDom([['close'], ['use']])
    doc.activeElement = make('page-button')
    wrapTabFocus(tab(), container)
    expect(doc.activeElement).toBe(items[0])
  })

  it('makes the page inert while open, then restores it and the opener focus', () => {
    const { doc, make } = fakeDom([])
    const opener = make('browse')
    const page = { inert: false, ownerDocument: doc }
    doc.activeElement = opener

    const release = holdModalFocus(page)
    expect(page.inert).toBe(true)
    doc.activeElement = make('picker-input')

    release()
    expect(page.inert).toBe(false)
    expect(opener.focus).toHaveBeenCalled()
    expect(doc.activeElement).toBe(opener)
  })

  it('does not focus an opener that has left the page', () => {
    const { doc, make } = fakeDom([])
    const opener = make('browse')
    opener.isConnected = false
    doc.activeElement = opener
    holdModalFocus({ inert: false, ownerDocument: doc })()
    expect(opener.focus).not.toHaveBeenCalled()
  })
})
