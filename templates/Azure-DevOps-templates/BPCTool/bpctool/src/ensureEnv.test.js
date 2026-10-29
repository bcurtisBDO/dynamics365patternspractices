import { mkdirSync, mkdtempSync, rmSync, symlinkSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { pathToFileURL } from 'node:url'
import { afterEach, describe, expect, it } from 'vitest'
import {
  installStamp,
  isEntryScript,
  nodeVersionSupported,
  parsePythonProbe,
  parseSetupArgs,
  pipStampExtra,
  pythonCandidates,
  pythonVersionSupported,
  venvCreateArgs,
} from '../scripts/ensure-env.mjs'

describe('nodeVersionSupported', () => {
  it('accepts the Node versions Vite 8 supports', () => {
    expect(nodeVersionSupported('20.19.0')).toBe(true)
    expect(nodeVersionSupported('22.12.0')).toBe(true)
    expect(nodeVersionSupported('24.3.0')).toBe(true)
  })

  it('rejects older and odd releases in between', () => {
    expect(nodeVersionSupported('20.18.1')).toBe(false)
    expect(nodeVersionSupported('21.7.3')).toBe(false)
    expect(nodeVersionSupported('22.11.0')).toBe(false)
    expect(nodeVersionSupported('18.20.0')).toBe(false)
    expect(nodeVersionSupported('')).toBe(false)
  })
})

describe('pythonVersionSupported', () => {
  it('requires Python 3.11 or newer', () => {
    expect(pythonVersionSupported('Python 3.11.9')).toBe(true)
    expect(pythonVersionSupported('Python 3.14.3')).toBe(true)
    expect(pythonVersionSupported('Python 3.10.12')).toBe(false)
    expect(pythonVersionSupported('Python 2.7.18')).toBe(false)
    expect(pythonVersionSupported(null)).toBe(false)
  })
})

describe('installStamp', () => {
  let dir

  afterEach(() => {
    rmSync(dir, { recursive: true, force: true })
  })

  it('changes only when the dependency files change', () => {
    dir = mkdtempSync(path.join(tmpdir(), 'bpctool-stamp-'))
    const lock = path.join(dir, 'package-lock.json')
    writeFileSync(lock, '{"lockfileVersion":3}')
    const first = installStamp([lock])

    expect(installStamp([lock])).toBe(first)
    writeFileSync(lock, '{"lockfileVersion":3,"packages":{}}')
    expect(installStamp([lock])).not.toBe(first)
    expect(installStamp([path.join(dir, 'missing.txt')])).toMatch(/^[0-9a-f]{64}$/)
  })

  it('changes when the venv interpreter changes', () => {
    dir = mkdtempSync(path.join(tmpdir(), 'bpctool-stamp-'))
    const requirements = path.join(dir, 'requirements.txt')
    writeFileSync(requirements, 'fastapi>=0.141.1\n')
    const venv = { executable: '/bpctool/.venv/bin/python', version: 'Python 3.12.4' }
    const first = installStamp([requirements], pipStampExtra(venv))

    expect(installStamp([requirements], pipStampExtra({ ...venv }))).toBe(first)
    expect(installStamp([requirements], pipStampExtra({ ...venv, version: 'Python 3.13.1' }))).not.toBe(first)
    expect(installStamp([requirements])).not.toBe(first)
  })
})

describe('venvCreateArgs', () => {
  it('clears a leftover venv so dangling interpreter links and the old pip stamp go', () => {
    expect(venvCreateArgs(false)).toEqual(['-m', 'venv', '.venv'])
    expect(venvCreateArgs(true)).toEqual(['-m', 'venv', '--clear', '.venv'])
  })
})

describe('parseSetupArgs', () => {
  it('runs the full setup by default and skips npm with --python-only', () => {
    expect(parseSetupArgs([])).toEqual({ pythonOnly: false })
    expect(parseSetupArgs(['--python-only'])).toEqual({ pythonOnly: true })
  })

  it('rejects unknown options', () => {
    expect(() => parseSetupArgs(['--pyton-only'])).toThrow(/Unknown option --pyton-only/)
  })
})

describe('parsePythonProbe', () => {
  it('reads the interpreter path and version', () => {
    expect(parsePythonProbe('C:\\Python312\\python.exe\r\n3.12.4\r\n')).toEqual({
      executable: 'C:\\Python312\\python.exe',
      version: 'Python 3.12.4',
    })
  })

  it('returns null for empty or partial output', () => {
    expect(parsePythonProbe('')).toBeNull()
    expect(parsePythonProbe('/usr/bin/python3\n')).toBeNull()
  })
})

describe('pythonCandidates', () => {
  it('adds the py launcher on Windows only', () => {
    expect(pythonCandidates(true)).toContainEqual(['py', '-3'])
    expect(pythonCandidates(false)).not.toContainEqual(['py', '-3'])
  })
})

describe('isEntryScript', () => {
  let dir

  afterEach(() => {
    rmSync(dir, { recursive: true, force: true })
  })

  it('matches the script when started through a junction or symlinked folder', () => {
    dir = mkdtempSync(path.join(tmpdir(), 'bpctool-entry-'))
    const realDir = path.join(dir, 'real', 'scripts')
    mkdirSync(realDir, { recursive: true })
    const script = path.join(realDir, 'ensure-env.mjs')
    writeFileSync(script, '')
    const linkDir = path.join(dir, 'link')
    symlinkSync(path.join(dir, 'real'), linkDir, 'junction')
    const moduleUrl = pathToFileURL(script).href

    expect(isEntryScript(path.join(linkDir, 'scripts', 'ensure-env.mjs'), moduleUrl)).toBe(true)
    expect(isEntryScript(script, moduleUrl)).toBe(true)
  })

  it('does not match other scripts or a missing argv[1]', () => {
    dir = mkdtempSync(path.join(tmpdir(), 'bpctool-entry-'))
    const script = path.join(dir, 'ensure-env.mjs')
    writeFileSync(script, '')
    const moduleUrl = pathToFileURL(script).href

    expect(isEntryScript(path.join(dir, 'vitest.mjs'), moduleUrl)).toBe(false)
    expect(isEntryScript(undefined, moduleUrl)).toBe(false)
  })
})
