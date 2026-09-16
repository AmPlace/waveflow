import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { assertBundledPluginRuntime, runtimeTarget } from '../main/desktopRuntime.js'

const here = path.dirname(fileURLToPath(import.meta.url))
const mainSource = fs.readFileSync(path.resolve(here, '../main/index.js'), 'utf8')
const windowsBuildSource = fs.readFileSync(path.resolve(here, '../../scripts/build-desktop-win.ps1'), 'utf8')

function writeRuntime(root, metadata = {}) {
  const runtimeRoot = path.join(root, 'python-runtime')
  fs.mkdirSync(runtimeRoot, { recursive: true })
  fs.writeFileSync(path.join(runtimeRoot, 'python.exe'), 'windows runtime')
  fs.writeFileSync(path.join(runtimeRoot, 'runtime.json'), JSON.stringify({
    schema_version: 1,
    runtime_type: 'python',
    python_version: '3.14.7',
    python_abi: 'cp314',
    os: 'windows',
    arch: 'x86_64',
    executable: 'python.exe',
    tree_sha256: 'a'.repeat(64),
    tree_file_count: 1,
    ...metadata,
  }))
  return path.join(root, 'waveflow-backend.exe')
}

test('Windows runtime target uses python.exe without POSIX executable bits', () => {
  assert.deepEqual(runtimeTarget('win32', 'x64'), {
    os: 'windows',
    arch: 'x86_64',
    pythonAbi: 'cp314',
    pythonVersionPrefix: '3.14.',
    executable: 'python.exe',
    requirePosixExecutable: false,
  })
})

test('Electron validates the Windows runtime path and metadata before startup', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'waveflow-runtime-'))
  try {
    const backend = writeRuntime(root)
    assert.doesNotThrow(() => assertBundledPluginRuntime(backend, { platform: 'win32', arch: 'x64' }))
    assert.throws(
      () => assertBundledPluginRuntime(backend, { platform: 'win32', arch: 'arm64' }),
      /unsupported|missing|invalid|incompatible/i,
    )
  } finally {
    fs.rmSync(root, { recursive: true, force: true })
  }
})

test('Electron fails closed when the Windows sidecar is missing', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'waveflow-runtime-'))
  try {
    const backend = path.join(root, 'waveflow-backend.exe')
    assert.throws(
      () => assertBundledPluginRuntime(backend, { platform: 'win32', arch: 'x64' }),
      /missing|invalid/i,
    )
  } finally {
    fs.rmSync(root, { recursive: true, force: true })
  }
})

test('Windows packaging stages the locked runtime and does not use latest ffmpeg discovery', () => {
  assert.match(windowsBuildSource, /build-desktop-python-runtime\.ps1/)
  assert.match(windowsBuildSource, /hidden-import adapters\.17live/)
  assert.match(windowsBuildSource, /collect-data zhconv/)
  assert.match(windowsBuildSource, /WAVEFLOW_DESKTOP_FFMPEG_SHA256/)
  assert.doesNotMatch(windowsBuildSource, /download-ffmpeg\.sh/)
  assert.match(mainSource, /assertBundledPluginRuntime\(backendPath\)/)
  assert.doesNotMatch(mainSource, /spawn\('cmd\.exe'/)
})
