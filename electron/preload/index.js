import { contextBridge } from 'electron'

contextBridge.exposeInMainWorld('WAVEFLOW_DESKTOP', {
  apiBase: 'http://127.0.0.1:18765',
  platform: process.platform,
})