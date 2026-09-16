import { API_BASE, isDesktop } from '../apiBase.js'

export class ApiError extends Error {
  constructor(message, { status = 0, detail = null } = {}) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
  }
}

let unauthorizedHandler = null

export function setUnauthorizedHandler(handler) {
  unauthorizedHandler = typeof handler === 'function' ? handler : null
}

/**
 * 组合 external signal + internal timeout → 统一 AbortSignal。
 *
 * 任一 abort 都会触发底层 fetch 取消；两者都 abort 后清理所有监听器。
 * 兼容不支持 AbortSignal.any() 的浏览器。
 */
function _combinedSignal(externalSignal, timeoutMs) {
  const ctrl = new AbortController()
  const timeoutId = setTimeout(() => ctrl.abort(), timeoutMs)

  const _onExternal = () => ctrl.abort()
  const _cleanup = () => {
    clearTimeout(timeoutId)
    if (externalSignal) {
      try { externalSignal.removeEventListener('abort', _onExternal) } catch {}
    }
  }

  if (externalSignal) {
    if (externalSignal.aborted) {
      clearTimeout(timeoutId)
      ctrl.abort()  // 立即 abort，不再设 timeout
      return { signal: ctrl.signal, cleanup: _cleanup }
    }
    externalSignal.addEventListener('abort', _onExternal, { once: true })
  }

  return { signal: ctrl.signal, cleanup: _cleanup }
}

export async function apiRequest(url, options = {}) {
  const method = (options.method || 'GET').toUpperCase()
  const headers = new Headers(options.headers || {})
  const timeout = options.timeout || 20_000
  const externalSignal = options.signal || null

  if (!headers.has('Accept')) headers.set('Accept', 'application/json')
  if (!['GET', 'HEAD', 'OPTIONS'].includes(method)) {
    headers.set('X-WaveFlow-Request', '1')
  }

  const { signal, cleanup } = _combinedSignal(externalSignal, timeout)

  try {
    const res = await fetch(`${API_BASE}${url}`, {
      ...options,
      method,
      headers,
      signal,
      credentials: isDesktop ? 'include' : 'same-origin',
    })
    cleanup()
    if (!res.ok) {
      const err = await res.json().catch(() => ({}))
      const apiError = new ApiError(err.detail || `HTTP ${res.status}`, {
        status: res.status,
        detail: err,
      })
      if (res.status === 401 && unauthorizedHandler) {
        try { unauthorizedHandler({ method, url }) } catch {}
      }
      throw apiError
    }
    return res
  } catch (error) {
    cleanup()
    if (error?.name === 'AbortError') {
      // 区分：外部 signal abort vs 超时
      if (externalSignal?.aborted) {
        throw new ApiError('请求已取消', { status: 0 })
      }
      throw new ApiError('请求超时', { status: 0 })
    }
    throw error
  }
}
