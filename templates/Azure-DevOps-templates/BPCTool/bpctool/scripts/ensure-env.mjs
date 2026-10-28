#!/usr/bin/env node
/**
 * One-time / first-launch environment bootstrap (no manual venv steps).
 * npm and pip installs rerun only when package-lock.json / runner requirements change.
 */
import { spawnSync } from 'node:child_process'
import { createHash } from 'node:crypto'
import { existsSync, readFileSync, realpathSync, writeFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const isWin = process.platform === 'win32'
const venvDir = path.join(ROOT, '.venv')
const venvPython = path.join(venvDir, isWin ? 'Scripts/python.exe' : 'bin/python')
// Stamps live inside the installed folders, so deleting a folder also forces a reinstall.
const NPM_STAMP = path.join(ROOT, 'node_modules', '.bpctool-setup-stamp')
const PIP_STAMP = path.join(venvDir, '.bpctool-setup-stamp')

export const NODE_REQUIREMENT = '^20.19 || >=22.12'
export const PYTHON_MIN = [3, 11]

const parseVersion = (text) => {
  const match = String(text || '').match(/(\d+)\.(\d+)(?:\.(\d+))?/)
  return match ? match.slice(1, 4).map((part) => Number(part || 0)) : null
}

/** Vite 8 needs Node ^20.19 || >=22.12. */
export function nodeVersionSupported(version) {
  const parsed = parseVersion(version)
  if (!parsed) {
    return false
  }
  const [major, minor] = parsed
  return (major === 20 && minor >= 19) || (major === 22 && minor >= 12) || major > 22
}

export function pythonVersionSupported(version) {
  const parsed = parseVersion(version)
  if (!parsed) {
    return false
  }
  const [major, minor] = parsed
  return major > PYTHON_MIN[0] || (major === PYTHON_MIN[0] && minor >= PYTHON_MIN[1])
}

/** Hash of the files an install depends on; missing files hash as empty. */
export function installStamp(files) {
  const digest = createHash('sha256')
  for (const file of files) {
    digest.update(`${path.basename(file)}\0`)
    digest.update(existsSync(file) ? readFileSync(file) : '')
    digest.update('\0')
  }
  return digest.digest('hex')
}

const readStamp = (file) => {
  try {
    return readFileSync(file, 'utf8').trim()
  } catch {
    return ''
  }
}

/** Command-line options; `--python-only` skips npm install (node_modules may be in use). */
export function parseSetupArgs(args) {
  const options = { pythonOnly: false }
  for (const arg of args) {
    if (arg === '--python-only') {
      options.pythonOnly = true
    } else {
      throw new Error(`Unknown option ${arg} (supported: --python-only)`)
    }
  }
  return options
}

/** Parses the output of PYTHON_PROBE: the interpreter's absolute path, then its version. */
export function parsePythonProbe(output) {
  const [executable, version] = String(output || '').trim().split(/\r?\n/).map((line) => line.trim())
  return executable && version ? { executable, version: `Python ${version}` } : null
}

const PYTHON_PROBE = 'import sys, platform; print(sys.executable); print(platform.python_version())'

export const pythonCandidates = (windows = isWin) => [
  ['python3'],
  ['python'],
  // The py launcher comes with python.org installs even when "Add python.exe to PATH" was missed.
  ...(windows ? [['py', '-3']] : []),
]

const realPath = (file) => {
  try {
    return realpathSync(file)
  } catch {
    return path.resolve(file)
  }
}

/**
 * Whether this module is the script Node was started with. Node reports the entry module by its
 * real path, so a junction or symlink in argv[1] must be resolved before comparing.
 */
export function isEntryScript(argv1, moduleUrl, windows = isWin) {
  if (!argv1) {
    return false
  }
  const [entry, self] = [realPath(argv1), realPath(fileURLToPath(moduleUrl))]
  return windows ? entry.toLowerCase() === self.toLowerCase() : entry === self
}

function fail(message) {
  console.error(`[setup] ${message}`)
  process.exit(1)
}

function run(command, args, hint) {
  const result = spawnSync(command, args, {
    cwd: ROOT,
    stdio: 'inherit',
    // Only .cmd shims need cmd.exe; a shell would split executable paths that contain spaces.
    shell: isWin && command.toLowerCase().endsWith('.cmd'),
  })
  if (result.error) {
    console.error(`[setup] Could not start: ${[command, ...args].join(' ')} (${result.error.message})`)
    fail(hint)
  }
  if (result.status !== 0) {
    process.exit(result.status ?? 1)
  }
}

/** { executable, version } for a Python command, or null when it can't be run. */
function probePython([command, ...args]) {
  const probe = spawnSync(command, [...args, '-c', PYTHON_PROBE], { encoding: 'utf8' })
  if (probe.error && probe.error.code !== 'ENOENT') {
    console.log(`[setup] Could not run ${[command, ...args].join(' ')}: ${probe.error.message}`)
  }
  return probe.status === 0 ? parsePythonProbe(probe.stdout) : null
}

function resolvePython() {
  const found = []
  const candidates = pythonCandidates()
  for (const candidate of candidates) {
    const python = probePython(candidate)
    if (python && pythonVersionSupported(python.version)) {
      return python
    }
    if (python) {
      found.push(`${python.version} (${python.executable})`)
    }
  }
  const tried = candidates.map((candidate) => candidate.join(' ')).join(', ')
  fail(
    found.length
      ? `Python ${PYTHON_MIN.join('.')}+ is required, but only ${found.join(', ')} was found. ` +
          'Install a newer Python 3 from python.org, then start BPCTool again.'
      : `Python ${PYTHON_MIN.join('.')}+ is required but was not found (tried: ${tried}). ` +
          'Install it from python.org (on Windows, tick "Add python.exe to PATH"), then start BPCTool again.',
  )
}

function main() {
  let options
  try {
    options = parseSetupArgs(process.argv.slice(2))
  } catch (error) {
    fail(error.message)
  }
  console.log('[setup] Working directory:', ROOT)

  if (!nodeVersionSupported(process.versions.node)) {
    fail(
      `Node.js ${process.versions.node} is too old for BPCTool (needs ${NODE_REQUIREMENT}). ` +
        'Install the current LTS from nodejs.org, then start BPCTool again.',
    )
  }

  const npmInputs = [path.join(ROOT, 'package-lock.json')]
  if (options.pythonOnly) {
    console.log('[setup] Skipping npm dependencies (--python-only).')
  } else if (!existsSync(path.join(ROOT, 'node_modules')) || readStamp(NPM_STAMP) !== installStamp(npmInputs)) {
    console.log('[setup] Installing npm dependencies...')
    run(
      isWin ? 'npm.cmd' : 'npm',
      ['install'],
      'npm comes with Node.js: reinstall the current LTS from nodejs.org, then start BPCTool again.',
    )
    // npm install may rewrite the lock file, so stamp what it left behind.
    writeFileSync(NPM_STAMP, `${installStamp(npmInputs)}\n`)
  } else {
    console.log('[setup] npm dependencies up to date.')
  }

  if (!existsSync(venvPython)) {
    const python = resolvePython()
    console.log(`[setup] Creating Python venv with ${python.version} (${python.executable})...`)
    run(
      python.executable,
      ['-m', 'venv', '.venv'],
      'Reinstall Python 3 from python.org, then start BPCTool again.',
    )
  } else {
    const python = probePython([venvPython])
    if (python && !pythonVersionSupported(python.version)) {
      fail(
        `The runner venv uses ${python.version}; BPCTool needs Python ${PYTHON_MIN.join('.')}+. ` +
          `Install a newer Python 3, delete the folder ${venvDir}, then start BPCTool again.`,
      )
    }
    console.log('[setup] Python venv already present.')
  }

  const pipStamp = installStamp([path.join(ROOT, 'runner', 'requirements.txt')])
  if (readStamp(PIP_STAMP) !== pipStamp) {
    console.log('[setup] Installing runner requirements...')
    // Not the venv's pip script: it embeds the interpreter path from before the folder was moved.
    run(
      venvPython,
      ['-m', 'pip', 'install', '-r', 'runner/requirements.txt'],
      `Delete the folder ${venvDir}, then start BPCTool again.`,
    )
    writeFileSync(PIP_STAMP, `${pipStamp}\n`)
  } else {
    console.log('[setup] Runner requirements up to date.')
  }

  console.log('[setup] Ready. Use Start BPCTool (or npm start) to launch.')
}

if (isEntryScript(process.argv[1], import.meta.url)) {
  main()
}
