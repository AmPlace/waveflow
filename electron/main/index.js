import { app, BrowserWindow, session, shell } from 'electron'
import { randomBytes } from 'node:crypto'
import { spawn } from 'node:child_process'
import fs from 'node:fs'
import http from 'node:http'
import net from 'node:net'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { assertBundledPluginRuntime } from './desktopRuntime.js'

const __filename = fileURLToPath(import.meta.url)
const __dirname = path.dirname(__filename)

const BACKEND_HOST = '127.0.0.1'
const BACKEND_PORT = 18765
const API_BASE = `http://${BACKEND_HOST}:${BACKEND_PORT}`
const DESKTOP_BOOTSTRAP_SECRET = randomBytes(32).toString('base64url')
const DESKTOP_DEBUG =
  process.env.WAVEFLOW_DESKTOP_DEBUG === '1' || process.argv.includes('--waveflow-debug')
const HAS_SINGLE_INSTANCE_LOCK = app.requestSingleInstanceLock()

let backendProcess = null
let backendReady = false
let backendRestartTimer = null
let desktopBootstrapPromise = null
let mainWindow = null
let quitting = false

function getBackendExecutableName() {
  return process.platform === 'win32' ? 'waveflow-backend.exe' : 'waveflow-backend'
}

function getBackendPath() {
  const executableName = getBackendExecutableName()

  if (app.isPackaged) {
    return path.join(process.resourcesPath, 'backend', executableName)
  }

  return path.join(app.getAppPath(), 'backend_dist', executableName)
}

function getFrontendIndexPath() {
  return path.join(app.getAppPath(), 'frontend', 'dist', 'index.html')
}

function getBackendArgs() {
  return [
    '--host',
    BACKEND_HOST,
    '--port',
    String(BACKEND_PORT),
    '--data-dir',
    app.getPath('userData'),
  ]
}

function getBackendEnvironment() {
  const environment = { ...process.env }
  for (const name of [
    'WAVEFLOW_DB_PATH',
    'WAVEFLOW_DATA_DIR',
    'WAVEFLOW_MODE',
    'WAVEFLOW_PRODUCTION',
    'WAVEFLOW_ALLOWED_ORIGINS',
    'WAVEFLOW_SESSION_COOKIE_SECURE',
    'WAVEFLOW_DESKTOP_SESSION',
  ]) {
    delete environment[name]
  }

  return {
    ...environment,
    WAVEFLOW_MODE: 'desktop',
    WAVEFLOW_ALLOWED_ORIGINS: 'null',
    WAVEFLOW_SESSION_COOKIE_SECURE: '0',
    WAVEFLOW_DESKTOP_SESSION: DESKTOP_BOOTSTRAP_SECRET,
  }
}

function startBackendInDebugMode(backendPath, backendArgs, environment) {
  if (process.platform === 'win32') {
    const logFd = fs.openSync(path.join(app.getPath('userData'), 'backend.log'), 'a')
    try {
      return spawn(backendPath, backendArgs, {
        env: environment,
        windowsHide: false,
        stdio: ['ignore', 'ignore', logFd],
      })
    } finally {
      fs.closeSync(logFd)
    }
  }

  return spawn(backendPath, backendArgs, {
    env: environment,
    stdio: 'inherit',
  })
}

function assertBackendPortAvailable() {
  return new Promise((resolve, reject) => {
    const probe = net.createServer()
    probe.once('error', () => {
      reject(new Error('Desktop backend port is already in use'))
    })
    probe.listen(BACKEND_PORT, BACKEND_HOST, () => {
      probe.close(() => resolve())
    })
  })
}

async function startBackend() {
  await assertBackendPortAvailable()

  const backendPath = getBackendPath()
  assertBundledPluginRuntime(backendPath)
  const backendArgs = getBackendArgs()
  const environment = getBackendEnvironment()
  const logPath = path.join(app.getPath('userData'), 'backend.log')
  fs.mkdirSync(app.getPath('userData'), { recursive: true })

  let child
  if (DESKTOP_DEBUG) {
    child = startBackendInDebugMode(backendPath, backendArgs, environment)
  } else {
    const logFd = fs.openSync(logPath, 'a')
    try {
      child = spawn(backendPath, backendArgs, {
        env: environment,
        windowsHide: true,
        stdio: ['ignore', 'ignore', logFd],
      })
    } finally {
      fs.closeSync(logFd)
    }
  }

  backendProcess = child
  child.once('error', (error) => {
    console.error('[WaveFlow backend process error]', error.message)
  })
  child.once('exit', (code) => {
    if (backendProcess === child) backendProcess = null
    if (!quitting && backendReady) {
      backendReady = false
      scheduleBackendRecovery()
    }
    console.log('[WaveFlow backend exited]', code)
  })
}

function stopBackend() {
  backendReady = false
  if (backendRestartTimer) {
    clearTimeout(backendRestartTimer)
    backendRestartTimer = null
  }

  if (backendProcess) {
    backendProcess.kill()
    backendProcess = null
  }
}

function waitForBackend(timeoutMs = 12000) {
  const startedAt = Date.now()

  return new Promise((resolve, reject) => {
    let settled = false

    const finish = (error) => {
      if (settled) return
      settled = true
      if (error) reject(error)
      else resolve()
    }

    const check = () => {
      if (settled) return
      if (Date.now() - startedAt > timeoutMs) {
        finish(new Error('Desktop backend startup timed out'))
        return
      }

      const request = http.get(`${API_BASE}/health`, (response) => {
        const healthy = response.statusCode === 200
        response.resume()
        if (healthy) {
          finish()
          return
        }
        setTimeout(check, 300)
      })

      request.once('error', () => setTimeout(check, 300))
      request.setTimeout(1000, () => request.destroy())
    }

    check()
  })
}

function parseSessionCookie(setCookieHeaders) {
  const header = setCookieHeaders.find((item) => item.startsWith('waveflow_session='))
  if (!header) throw new Error('Desktop bootstrap did not return a session cookie')

  const pair = header.split(';', 1)[0]
  const separator = pair.indexOf('=')
  if (separator <= 0) throw new Error('Desktop bootstrap returned an invalid session cookie')

  const attributes = {}
  for (const rawAttribute of header.split(';').slice(1)) {
    const [rawName, ...rawValue] = rawAttribute.trim().split('=')
    attributes[rawName.toLowerCase()] = rawValue.join('=')
  }

  return {
    name: pair.slice(0, separator),
    value: pair.slice(separator + 1),
    maxAge: Number(attributes['max-age'] || 0),
  }
}

async function installSessionCookie(setCookieHeaders) {
  const cookie = parseSessionCookie(setCookieHeaders)
  const details = {
    url: `${API_BASE}/`,
    name: cookie.name,
    value: cookie.value,
    path: '/',
    httpOnly: true,
    secure: true,
    sameSite: 'no_restriction',
  }
  if (cookie.maxAge > 0) {
    details.expirationDate = Math.floor(Date.now() / 1000) + cookie.maxAge
  }
  await session.defaultSession.cookies.set(details)
}

function requestDesktopSession() {
  return new Promise((resolve, reject) => {
    const request = http.request(`${API_BASE}/api/auth/desktop`, {
      method: 'POST',
      headers: {
        Accept: 'application/json',
        'Content-Type': 'application/json',
      },
    }, (response) => {
      const setCookieHeaders = response.headers['set-cookie'] || []
      response.resume()
      response.once('end', async () => {
        if (response.statusCode !== 200) {
          reject(new Error(`Desktop bootstrap rejected (${response.statusCode || 'unknown'})`))
          return
        }

        try {
          await installSessionCookie(setCookieHeaders)
          resolve()
        } catch (error) {
          reject(error)
        }
      })
    })

    request.once('error', () => reject(new Error('Desktop bootstrap request failed')))
    request.end(JSON.stringify({ desktop_session: DESKTOP_BOOTSTRAP_SECRET }))
  })
}

function establishDesktopSession() {
  if (!desktopBootstrapPromise) desktopBootstrapPromise = requestDesktopSession()
  return desktopBootstrapPromise
}

function scheduleBackendRecovery() {
  if (backendRestartTimer || quitting || !mainWindow) return

  backendRestartTimer = setTimeout(async () => {
    backendRestartTimer = null
    try {
      await startBackend()
      await waitForBackend()
      backendReady = true
    } catch (error) {
      console.error('[WaveFlow backend recovery failed]', error.message)
    }
  }, 500)
}

function openExternalUrl(url) {
  try {
    const parsed = new URL(url)
    if (parsed.protocol === 'http:' || parsed.protocol === 'https:') {
      void shell.openExternal(url)
    }
  } catch {
    // Invalid renderer URLs stay blocked.
  }
}

function isExpectedRendererUrl(url) {
  try {
    const parsed = new URL(url)
    return parsed.protocol === 'file:' &&
      path.resolve(fileURLToPath(parsed)) === path.resolve(getFrontendIndexPath())
  } catch {
    return false
  }
}

async function createWindow() {
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.show()
    mainWindow.focus()
    return mainWindow
  }

  const win = new BrowserWindow({
    width: 1280,
    height: 820,
    minWidth: 1000,
    minHeight: 680,
    title: 'WaveFlow',
    backgroundColor: '#f8f8f7',
    titleBarStyle: 'hidden',
    titleBarOverlay: {
      color: '#f8f8f7',
      symbolColor: '#111827',
      height: 28,
    },
    autoHideMenuBar: true,
    show: false,
    webPreferences: {
      preload: path.join(__dirname, '../preload/index.mjs'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
      devTools: DESKTOP_DEBUG,
    },
  })
  mainWindow = win

  win.webContents.setWindowOpenHandler(({ url }) => {
    openExternalUrl(url)
    return { action: 'deny' }
  })
  win.webContents.on('will-navigate', (event, url) => {
    if (!isExpectedRendererUrl(url)) {
      event.preventDefault()
      openExternalUrl(url)
    }
  })
  win.webContents.on('will-redirect', (event, url) => {
    if (!isExpectedRendererUrl(url)) {
      event.preventDefault()
      openExternalUrl(url)
    }
  })

  if (DESKTOP_DEBUG) {
    win.webContents.on('before-input-event', (event, input) => {
      if (input.type === 'keyDown' && input.key === 'F12') {
        win.webContents.toggleDevTools()
      }
    })
  }

  win.once('ready-to-show', () => win.show())
  win.once('closed', () => {
    if (mainWindow === win) mainWindow = null
  })

  await win.loadFile(getFrontendIndexPath())
  return win
}

if (!HAS_SINGLE_INSTANCE_LOCK) {
  app.quit()
} else {
  app.on('second-instance', () => {
    if (mainWindow && !mainWindow.isDestroyed()) {
      if (mainWindow.isMinimized()) mainWindow.restore()
      mainWindow.show()
      mainWindow.focus()
    }
  })

  app.whenReady().then(async () => {
    await startBackend()
    await waitForBackend()
    await establishDesktopSession()
    backendReady = true
    await createWindow()
  }).catch((error) => {
    console.error('[WaveFlow Desktop startup failed]', error.message)
    stopBackend()
    app.quit()
  })

  app.on('activate', () => {
    if (backendReady && BrowserWindow.getAllWindows().length === 0) {
      void createWindow().catch((error) => {
        console.error('[WaveFlow window restore failed]', error.message)
      })
    }
  })
}

app.on('before-quit', () => {
  quitting = true
  stopBackend()
})

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit()
})
