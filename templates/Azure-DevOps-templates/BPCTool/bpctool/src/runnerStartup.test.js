import { EventEmitter } from 'node:events'
import http from 'node:http'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { killProcessTree, listeningPids, waitForHealth } from '../scripts/localRunnerPlugin.js'

function listen(handler) {
  return new Promise((resolve) => {
    const server = http.createServer(handler)
    server.listen(0, '127.0.0.1', () => resolve(server))
  })
}

async function unusedUrl() {
  const server = await listen(() => {})
  const { port } = server.address()
  await new Promise((resolve) => server.close(resolve))
  return `http://127.0.0.1:${port}/health`
}

describe('waitForHealth', () => {
  let server

  afterEach(async () => {
    if (server) {
      await new Promise((resolve) => server.close(resolve))
      server = null
    }
  })

  it('resolves once the runner answers /health', async () => {
    server = await listen((req, res) => res.writeHead(200).end('{}'))
    const url = `http://127.0.0.1:${server.address().port}/health`
    const child = new EventEmitter()

    await expect(waitForHealth(child, { url })).resolves.toBeUndefined()
    expect(child.listenerCount('exit')).toBe(0)
  })

  it('stops waiting as soon as the runner exits, pointing to setup', async () => {
    const child = new EventEmitter()
    const started = Date.now()
    const waiting = waitForHealth(child, { url: await unusedUrl(), timeoutMs: 20000 })
    setTimeout(() => child.emit('exit', 1, null), 50)

    await expect(waiting).rejects.toThrow(/npm run setup/)
    expect(Date.now() - started).toBeLessThan(5000)
  })

  it('stops waiting when the runner cannot be spawned', async () => {
    const child = new EventEmitter()
    const waiting = waitForHealth(child, { url: await unusedUrl() })
    child.emit('error', Object.assign(new Error('spawn python ENOENT'), { code: 'ENOENT' }))

    await expect(waiting).rejects.toThrow(/Start BPCTool/)
  })

  it('gives up after the timeout', async () => {
    const url = await unusedUrl()
    await expect(waitForHealth(new EventEmitter(), { url, timeoutMs: 300 })).rejects.toThrow(
      /did not become healthy/,
    )
  })

  it('waits for the runner it started, not an older one still on the port', async () => {
    let instance = 'old-runner'
    server = await listen((req, res) =>
      res.writeHead(200).end(JSON.stringify({ status: 'ok', pid: 111, instance })),
    )
    const url = `http://127.0.0.1:${server.address().port}/health`
    let resolved = false
    const waiting = waitForHealth(new EventEmitter(), { url, instance: 'new-runner' }).then(() => {
      resolved = true
    })

    await new Promise((resolve) => setTimeout(resolve, 600))
    expect(resolved).toBe(false)
    instance = 'new-runner'
    await waiting
    expect(resolved).toBe(true)
  })

  it('reports an older runner holding the port when the new one exits', async () => {
    server = await listen((req, res) =>
      res.writeHead(200).end(JSON.stringify({ status: 'ok', instance: 'old-runner' })),
    )
    const url = `http://127.0.0.1:${server.address().port}/health`
    const child = new EventEmitter()
    const waiting = waitForHealth(child, { url, instance: 'new-runner' })
    setTimeout(() => child.emit('exit', 1, null), 400)

    await expect(waiting).rejects.toThrow(/older BPCTool runner is still using port/)
  })
})

describe('listeningPids', () => {
  const netstat = [
    'Active Connections',
    '',
    '  Proto  Local Address          Foreign Address        State           PID',
    '  TCP    0.0.0.0:135            0.0.0.0:0              LISTENING       1000',
    '  TCP    127.0.0.1:8787         0.0.0.0:0              ABHÖREN         4321',
    '  TCP    127.0.0.1:8787         127.0.0.1:50000        HERGESTELLT     4321',
    '  TCP    127.0.0.1:50000        127.0.0.1:8787         HERGESTELLT     9999',
    '  TCP    127.0.0.1:18787        0.0.0.0:0              ABHÖREN         2000',
    '  TCP    127.0.0.1:8787         127.0.0.1:50010        TIME_WAIT       0',
    '  TCP    [::]:8787              [::]:0                 ÉCOUTE          5555',
    '  UDP    0.0.0.0:8787           *:*                                    3000',
  ].join('\r\n')

  it('finds listeners by address columns, whatever language the state is in', () => {
    expect(listeningPids(netstat, 8787)).toEqual(['4321', '5555'])
  })

  it('returns nothing when no one listens on the port', () => {
    expect(listeningPids(netstat, 5173)).toEqual([])
    expect(listeningPids('', 8787)).toEqual([])
  })
})

describe('killProcessTree', () => {
  it('kills the whole process tree on Windows', () => {
    const exec = vi.fn()
    const proc = { pid: 4242, kill: vi.fn() }

    killProcessTree(proc, { platform: 'win32', exec })

    expect(exec).toHaveBeenCalledWith('taskkill /PID 4242 /T /F', { stdio: 'ignore' })
    expect(proc.kill).not.toHaveBeenCalled()
  })

  it('sends SIGTERM elsewhere and ignores processes that never started', () => {
    const exec = vi.fn()
    const proc = { pid: 4242, kill: vi.fn() }

    killProcessTree(proc, { platform: 'darwin', exec })
    killProcessTree({ pid: undefined, kill: vi.fn() }, { platform: 'win32', exec })

    expect(proc.kill).toHaveBeenCalledWith('SIGTERM')
    expect(exec).not.toHaveBeenCalled()
  })
})
