import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const here = path.dirname(fileURLToPath(import.meta.url))
const mainSource = fs.readFileSync(path.resolve(here, '../main/index.js'), 'utf8')
const preloadSource = fs.readFileSync(path.resolve(here, '../preload/index.js'), 'utf8')
const desktopEntrySource = fs.readFileSync(path.resolve(here, '../../backend/desktop_entry.py'), 'utf8')

test('Desktop startup owns mode and one-shot bootstrap outside renderer content', () => {
  assert.match(mainSource, /WAVEFLOW_MODE: 'desktop'/)
  assert.match(mainSource, /WAVEFLOW_DESKTOP_SESSION: DESKTOP_BOOTSTRAP_SECRET/)
  assert.match(mainSource, /establishDesktopSession\(\)/)
  assert.match(mainSource, /session\.defaultSession\.cookies\.set/)
  assert.match(mainSource, /secure: true/)
  assert.match(mainSource, /sameSite: 'no_restriction'/)
  assert.doesNotMatch(preloadSource, /DESKTOP_BOOTSTRAP_SECRET|desktop_session/)
})

test('Desktop shell keeps runtime and navigation boundaries explicit', () => {
  assert.match(mainSource, /requestSingleInstanceLock\(\)/)
  assert.match(mainSource, /assertBackendPortAvailable\(\)/)
  assert.match(mainSource, /setWindowOpenHandler/)
  assert.match(mainSource, /will-navigate/)
  assert.match(mainSource, /contextIsolation: true/)
  assert.match(mainSource, /nodeIntegration: false/)
  assert.match(mainSource, /backendProcess\.kill\(\)/)
  assert.match(desktopEntrySource, /os\.environ\["WAVEFLOW_MODE"\] = "desktop"/)
})
