const STATUS_MESSAGES = Object.freeze({
  400: '提交内容不符合要求，请检查后重试',
  401: '登录已失效，请重新登录',
  403: '当前账号无权执行此操作',
  409: '状态已发生变化，请刷新后重试',
  422: '提交内容不符合要求，请检查后重试',
})

function errorStatus(error) {
  const status = Number(error?.status || 0)
  return Number.isInteger(status) ? status : 0
}

export function adminRequestErrorMessage(error, fallback = '操作失败，请稍后重试') {
  const status = errorStatus(error)
  if (status === 400 || status === 422) {
    const detail = safeAdminDiagnostic(error?.message)
    if (detail && !/^HTTP\s+\d+$/i.test(detail) && !/^\[object Object\](?:,\[object Object\])*$/i.test(detail)) {
      return `提交内容不符合要求：${detail}`
    }
  }
  if (STATUS_MESSAGES[status]) return STATUS_MESSAGES[status]
  if (status >= 500) return '服务暂时不可用，请稍后重试'
  if (status === 0 && /超时|timeout/i.test(String(error?.message || ''))) {
    return '请求超时，请稍后重试'
  }
  return fallback
}

export function safeAdminUrl(value) {
  const text = String(value || '').trim()
  if (!text) return ''
  try {
    const parsed = new URL(text)
    if (!['http:', 'https:'].includes(parsed.protocol)) return ''
    parsed.username = ''
    parsed.password = ''
    parsed.search = ''
    parsed.hash = ''
    return parsed.toString().replace(/\/$/, parsed.pathname === '/' ? '/' : '')
  } catch {
    return ''
  }
}

export function safeAdminDiagnostic(value) {
  let text = String(value || '').replace(/[\u0000-\u001f\u007f]/g, ' ').trim()
  if (!text) return ''

  text = text
    .replace(/https?:\/\/[^\s"'<>]+/gi, match => safeAdminUrl(match) || '[redacted-url]')
    .replace(/\bAuthorization\s*:\s*(?:Bearer\s+)?[^\s,;]+/gi, 'Authorization: [redacted]')
    .replace(/\bCookie\s*:\s*[^\r\n]+/gi, 'Cookie: [redacted]')
    .replace(/\b(token|access_token|refresh_token|api[_-]?key|private[_-]?key|signature|sig|secret|password|key)\s*[:=]\s*(?:"[^"]*"|'[^']*'|[^\s&;,]+)/gi, '$1=[redacted]')
    .replace(/(?:\/[A-Za-z0-9._-]+){3,}/g, '[local-path]')
    .replace(/[A-Za-z]:\\(?:[^\s\\]+\\){2,}[^\s]*/g, '[local-path]')

  if (/traceback|stack trace|-----BEGIN [A-Z ]*PRIVATE KEY-----/i.test(text)) {
    text = text.replace(/traceback[\s\S]*/i, '[diagnostic-hidden]')
      .replace(/stack trace[\s\S]*/i, '[diagnostic-hidden]')
      .replace(/-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*/i, '[diagnostic-hidden]')
  }
  return text.slice(0, 512)
}
