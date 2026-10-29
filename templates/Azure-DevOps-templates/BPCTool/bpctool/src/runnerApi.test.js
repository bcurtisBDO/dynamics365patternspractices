import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  buildRunnerJobBody,
  createRunnerJob,
  RUNNER_UNREACHABLE,
  runnerErrorMessage,
  runnerFetch,
} from './runnerApi'

describe('runnerFetch', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('explains a network failure instead of "Failed to fetch"', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')))
    await expect(runnerFetch('/api/defaults')).rejects.toThrow(RUNNER_UNREACHABLE)
  })

  it('treats the dev proxy 502 (runner not listening) as unreachable', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('', { status: 502 })))
    await expect(runnerFetch('/api/defaults')).rejects.toThrow(RUNNER_UNREACHABLE)
  })

  it('passes other responses and aborts through', async () => {
    const ok = new Response('{}', { status: 200 })
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(ok))
    await expect(runnerFetch('/api/defaults')).resolves.toBe(ok)

    const abort = new DOMException('stopped', 'AbortError')
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(abort))
    await expect(runnerFetch('/api/jobs/1')).rejects.toBe(abort)
  })
})

describe('runnerErrorMessage', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('formats a 422 validation list instead of [object Object]', () => {
    const payload = {
      detail: [
        { loc: ['body', 'steps'], msg: 'steps must list at least one step', type: 'value_error' },
        { loc: ['body', 'org_url'], msg: 'Field required', type: 'missing' },
      ],
    }
    expect(runnerErrorMessage(payload, 422)).toBe(
      'steps: steps must list at least one step; org_url: Field required',
    )
  })

  it('passes string details through and falls back to the status', () => {
    expect(runnerErrorMessage({ detail: 'Job not found' }, 404)).toBe('Job not found')
    expect(runnerErrorMessage({}, 500)).toBe('Runner error (500)')
    expect(runnerErrorMessage(null, 404, 'Request failed')).toBe('Request failed (404)')
  })

  it('is what parsePayload throws for runner errors', async () => {
    const body = JSON.stringify({ detail: [{ loc: ['body', 'pat'], msg: 'Field required' }] })
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(body, { status: 422 })))
    await expect(createRunnerJob({})).rejects.toThrow('pat: Field required')
  })
})

describe('buildRunnerJobBody', () => {
  const base = {
    orgUrl: 'https://dev.azure.com/contoso',
    pat: 'x'.repeat(20),
    processName: 'BPC',
    projectName: 'Contoso',
    templatePath: '/t.xlsx',
    updatePolicy: 'always-update',
  }

  it('sends the explicit step list', () => {
    expect(buildRunnerJobBody({ ...base, steps: ['catalog', 'report'] }).steps).toEqual([
      'catalog',
      'report',
    ])
  })

  it('refuses a missing or empty step list (the runner would run every step)', () => {
    expect(() => buildRunnerJobBody({ ...base, steps: [] })).toThrow(/Nothing to run/)
    expect(() => buildRunnerJobBody({ ...base, steps: null })).toThrow(/Nothing to run/)
    expect(() => buildRunnerJobBody(base)).toThrow(/Nothing to run/)
  })
})
