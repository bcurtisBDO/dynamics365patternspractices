/**
 * Vite plugin: start/stop the local FastAPI runner with the UI so users do not
 * need a second terminal (`npm run runner`).
 *
 * (Re)starts an owned uvicorn on each launch so code changes are picked up
 * (`npm run dev` adds --reload). An idle BPCTool runner on :8787 is replaced;
 * one with a run in progress is reused, and an unrelated program on the port
 * is left alone.
 */
import { execSync, spawn } from 'node:child_process'
import { existsSync, readFileSync } from 'node:fs'
import http from 'node:http'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const ROOT = path.resolve(__dirname, '..')
const RUNNER_DIR = path.join(ROOT, 'runner')
const HOST = '127.0.0.1'
const PORT = 8787
const HEALTH = `http://${HOST}:${PORT}/health`
const ENV_FILE = path.join(ROOT, 'bpctool.env')

/** BPCTOOL_* settings from bpctool.env; real environment variables win. */
export function parseEnvFile(text) {
  const values = {}
  for (const raw of text.split(/\r?\n/)) {
    const line = raw.trim()
    if (!line || line.startsWith('#')) continue
    const eq = line.indexOf('=')
    if (eq <= 0) continue
    const key = line.slice(0, eq).trim()
    if (!key.startsWith('BPCTOOL_')) continue
    let value = line.slice(eq + 1).trim()
    if (value.length >= 2 && (value[0] === '"' || value[0] === "'") && value.at(-1) === value[0]) {
      value = value.slice(1, -1)
    }
    if (value) values[key] = value
  }
  return values
}

function runnerEnv(logger = console) {
  if (!existsSync(ENV_FILE)) {
    return { ...process.env }
  }
  const fromFile = parseEnvFile(readFileSync(ENV_FILE, 'utf8'))
  const keys = Object.keys(fromFile)
  if (keys.length) {
    logger.info?.(`[bpc-runner] Loaded ${keys.join(', ')} from bpctool.env`)
  }
  return { ...fromFile, ...process.env }
}

// Console scripts such as .venv/bin/uvicorn embed the absolute interpreter path from when the
// venv was created, so they break once the BPCTool folder is moved; `python -m` does not.
function resolveVenvPython() {
  const venvPython = path.join(
    ROOT,
    '.venv',
    process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python',
  )
  return existsSync(venvPython) ? venvPython : null
}

/** First launch from a fresh copy (e.g. `npm start` without Start BPCTool): create the venv. */
function runSetup(logger = console) {
  return new Promise((resolve, reject) => {
    // npm install would rewrite node_modules while Vite is running from it (EBUSY on Windows).
    const setup = spawn(process.execPath, [path.join(__dirname, 'ensure-env.mjs'), '--python-only'], {
      cwd: ROOT,
      stdio: ['ignore', 'pipe', 'pipe'],
    })
    const relay = (chunk) => {
      for (const line of String(chunk).split(/\r?\n/)) {
        if (line.trim()) logger.info?.(`[bpc-setup] ${line.trim()}`)
      }
    }
    setup.stdout?.on('data', relay)
    setup.stderr?.on('data', relay)
    setup.on('error', reject)
    setup.on('exit', (code) => {
      if (code === 0) {
        resolve()
      } else {
        reject(
          new Error(
            `First-launch setup failed (exit ${code}); see the [bpc-setup] lines above. ` +
              'Python 3.11+ must be installed and on PATH; then run: npm run setup',
          ),
        )
      }
    })
  })
}

/** Resolves once the runner answers /health; rejects early if `child` exits first. */
export function waitForHealth(child, { url = HEALTH, timeoutMs = 20000 } = {}) {
  const started = Date.now()
  return new Promise((resolve, reject) => {
    let done = false
    const finish = (error) => {
      if (done) return
      done = true
      child.off('exit', onExit)
      child.off('error', onExit)
      if (error) {
        reject(error)
      } else {
        resolve()
      }
    }
    const onExit = () =>
      finish(
        new Error(
          'The Python runner stopped while starting (see the [bpc-runner] lines above). ' +
            'Close BPCTool and double-click "Start BPCTool" to repair the setup, or run: npm run setup',
        ),
      )
    child.once('exit', onExit)
    child.once('error', onExit)

    const tick = () => {
      if (done) return
      const req = http.get(url, (res) => {
        res.resume()
        if (res.statusCode === 200) {
          finish()
          return
        }
        retry()
      })
      req.on('error', retry)
      req.setTimeout(1000, () => {
        req.destroy()
        retry()
      })
    }
    const retry = () => {
      if (done) return
      if (Date.now() - started > timeoutMs) {
        finish(new Error(`Local runner did not become healthy on ${url}`))
        return
      }
      setTimeout(tick, 250)
    }
    tick()
  })
}

/**
 * Stop the runner and anything it started. On Windows the venv python.exe is a launcher that
 * runs the real interpreter as a child, and --reload adds a worker; killing only the PID we
 * spawned would leave those holding the port.
 */
export function killProcessTree(proc, { platform = process.platform, exec = execSync } = {}) {
  if (!proc?.pid) return
  try {
    if (platform === 'win32') {
      exec(`taskkill /PID ${proc.pid} /T /F`, { stdio: 'ignore' })
    } else {
      proc.kill('SIGTERM')
    }
  } catch {
    // already gone
  }
}

/**
 * What is listening on the runner port: null (nothing), or { bpctool, activeJobs }.
 * activeJobs is null for BPCTool runners from before /health reported it.
 */
export function describeListener(statusCode, body) {
  let health
  try {
    health = JSON.parse(body)
  } catch {
    health = null
  }
  const bpctool =
    statusCode === 200 && health?.status === 'ok' && 'teams_writer_mtime' in health
  return {
    bpctool,
    activeJobs: bpctool && typeof health.active_jobs === 'number' ? health.active_jobs : null,
  }
}

function probeListener() {
  return new Promise((resolve) => {
    const req = http.get(HEALTH, (res) => {
      let body = ''
      res.setEncoding('utf8')
      res.on('data', (chunk) => {
        body += chunk
      })
      res.on('end', () => resolve(describeListener(res.statusCode, body)))
    })
    req.on('error', () => resolve(null))
    req.setTimeout(800, () => {
      req.destroy()
      resolve(null)
    })
  })
}

/** Stop whatever is listening on the runner port (stale uvicorn from prior sessions). */
function freeRunnerPort(logger = console) {
  try {
    if (process.platform === 'win32') {
      const out = execSync(`netstat -ano | findstr :${PORT}`, {
        encoding: 'utf8',
        stdio: ['ignore', 'pipe', 'ignore'],
      })
      const pids = new Set()
      for (const line of out.split(/\r?\n/)) {
        if (!line.includes('LISTENING')) continue
        const pid = line.trim().split(/\s+/).pop()
        if (pid && /^\d+$/.test(pid)) pids.add(pid)
      }
      for (const pid of pids) {
        try {
          execSync(`taskkill /PID ${pid} /T /F`, { stdio: 'ignore' })
          logger.info?.(`[bpc-runner] Stopped stale process PID ${pid} on :${PORT}`)
        } catch {
          /* ignore */
        }
      }
      return
    }

    const out = execSync(`lsof -tiTCP:${PORT} -sTCP:LISTEN`, {
      encoding: 'utf8',
      stdio: ['ignore', 'pipe', 'ignore'],
    }).trim()
    if (!out) return
    for (const pid of out.split(/\s+/).filter(Boolean)) {
      try {
        process.kill(Number(pid), 'SIGTERM')
        logger.info?.(`[bpc-runner] Stopped stale process PID ${pid} on :${PORT}`)
      } catch {
        /* ignore */
      }
    }
  } catch {
    // nothing listening
  }
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

/**
 * `configure` hook for the /api proxy: while the runner is starting (or stopped) answer
 * 502 without Vite's stack trace. The UI treats 502 as "runner not up yet" and retries.
 */
export function quietRunnerUnavailable(proxy) {
  const emit = proxy.emit.bind(proxy)
  proxy.emit = (event, error, req, res, ...rest) => {
    if (event === 'error' && error?.code === 'ECONNREFUSED' && res && 'req' in res) {
      if (!res.headersSent && !res.writableEnded) {
        res.writeHead(502, { 'Content-Type': 'text/plain' }).end()
      }
      return true
    }
    return emit(event, error, req, res, ...rest)
  }
}

// Opened here rather than with `vite --open` so the page's first runner calls don't race
// the runner's startup. globalThis survives Vite re-importing this file on a config restart.
function openBrowserOnce(server) {
  if (typeof server.openBrowser !== 'function' || globalThis.__bpctoolBrowserOpened) {
    return
  }
  globalThis.__bpctoolBrowserOpened = true
  server.openBrowser()
}

export function localRunnerPlugin() {
  let child = null
  let owned = false
  let shuttingDown = false

  const stop = () => {
    if (!child || !owned) {
      return
    }
    shuttingDown = true
    killProcessTree(child)
    child = null
    owned = false
  }

  const start = async (logger = console) => {
    const listener = await probeListener()
    if (listener && !listener.bpctool) {
      throw new Error(
        `Port ${PORT} is in use by another program. Close it, then restart BPCTool.`,
      )
    }
    if (listener?.activeJobs > 0) {
      logger.warn?.(
        `[bpc-runner] The BPCTool runner already on :${PORT} has ${listener.activeJobs} run(s) in ` +
          'progress; using it as-is. Restart BPCTool after the run finishes to load code changes.',
      )
      return
    }
    if (listener) {
      // A runner from an earlier session would serve that session's code. Older builds can't
      // report runs in progress; keeping them would hide every later fix.
      logger.info?.(
        listener.activeJobs === null
          ? `[bpc-runner] Replacing a BPCTool runner from an older version on :${PORT} (it can't report runs in progress)`
          : `[bpc-runner] Replacing idle BPCTool runner on :${PORT}`,
      )
      freeRunnerPort(logger)
      await sleep(400)
    }

    let python = resolveVenvPython()
    if (!python) {
      logger.info?.(
        '[bpc-runner] Python runner venv not found — running first-launch setup (about a minute)...',
      )
      await runSetup(logger)
      python = resolveVenvPython()
    }
    if (!python) {
      throw new Error(
        'Python runner venv still not found after setup. Double-click "Start BPCTool" or run: npm run setup',
      )
    }

    // --reload restarts the runner on any code change, killing a run in progress (e.g. a
    // git pull mid-import), so only `npm run dev` uses it.
    const reload = process.env.npm_lifecycle_event === 'dev'
    logger.info?.(
      `[bpc-runner] Starting ${python} -m uvicorn on ${HOST}:${PORT}${reload ? ' (--reload)' : ''}`,
    )
    const runner = spawn(
      python,
      [
        '-m',
        'uvicorn',
        'app.main:app',
        '--host',
        HOST,
        '--port',
        String(PORT),
        ...(reload ? ['--reload', '--reload-dir', 'app'] : []),
      ],
      {
        cwd: RUNNER_DIR,
        stdio: ['ignore', 'pipe', 'pipe'],
        env: runnerEnv(logger),
      },
    )
    child = runner
    owned = true
    shuttingDown = false

    runner.on('error', (error) => {
      logger.error?.(
        `[bpc-runner] Could not start the Python runner (${error.message}). ` +
          'Close BPCTool and double-click "Start BPCTool" to repair the setup, or run: npm run setup',
      )
      if (child === runner) {
        child = null
        owned = false
      }
    })
    runner.stdout?.on('data', (chunk) => {
      const text = String(chunk).trim()
      if (text) {
        logger.info?.(`[bpc-runner] ${text}`)
      }
    })
    runner.stderr?.on('data', (chunk) => {
      const text = String(chunk).trim()
      if (text) {
        logger.info?.(`[bpc-runner] ${text}`)
      }
    })
    runner.on('exit', (code, signal) => {
      if (!shuttingDown && owned) {
        logger.error?.(
          `[bpc-runner] Exited unexpectedly (code=${code}, signal=${signal})`,
        )
      }
      if (child === runner) {
        child = null
        owned = false
      }
    })

    await waitForHealth(runner)
    logger.info?.('[bpc-runner] Ready.')
  }

  const attach = (server) => {
    const logger = server.config.logger
    server.httpServer?.once('listening', () => {
      start(logger)
        .catch((error) => {
          logger.error(`[bpc-runner] ${error.message}`)
        })
        .finally(() => openBrowserOnce(server))
    })

    const shutdown = () => stop()
    process.once('exit', shutdown)
    process.once('SIGINT', () => {
      stop()
      process.exit(0)
    })
    process.once('SIGTERM', () => {
      stop()
      process.exit(0)
    })
  }

  return {
    name: 'bpc-local-runner',
    apply: 'serve',
    configureServer: attach,
    configurePreviewServer: attach,
  }
}
