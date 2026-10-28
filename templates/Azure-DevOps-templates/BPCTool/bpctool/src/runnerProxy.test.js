import { EventEmitter } from 'node:events'
import { describe, expect, it } from 'vitest'
import { quietRunnerUnavailable } from '../scripts/localRunnerPlugin.js'

function fakeResponse() {
  return {
    req: { url: '/defaults' },
    headersSent: false,
    writableEnded: false,
    status: null,
    writeHead(status) {
      this.status = status
      return this
    },
    end() {
      this.writableEnded = true
    },
  }
}

describe('quietRunnerUnavailable', () => {
  it('answers 502 without passing a refused connection on to Vite', () => {
    const proxy = new EventEmitter()
    quietRunnerUnavailable(proxy)
    const logged = []
    proxy.on('error', (error) => logged.push(error))
    const res = fakeResponse()
    const refused = Object.assign(new Error('connect ECONNREFUSED 127.0.0.1:8787'), {
      code: 'ECONNREFUSED',
    })

    proxy.emit('error', refused, {}, res, 'http://127.0.0.1:8787')

    expect(res.status).toBe(502)
    expect(res.writableEnded).toBe(true)
    expect(logged).toEqual([])
  })

  it('passes other proxy errors through', () => {
    const proxy = new EventEmitter()
    quietRunnerUnavailable(proxy)
    const logged = []
    proxy.on('error', (error) => logged.push(error.code))
    const res = fakeResponse()

    proxy.emit('error', Object.assign(new Error('socket hang up'), { code: 'ECONNRESET' }), {}, res)

    expect(logged).toEqual(['ECONNRESET'])
    expect(res.status).toBeNull()
  })
})
