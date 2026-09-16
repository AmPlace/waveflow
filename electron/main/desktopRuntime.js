import fs from 'node:fs'
import path from 'node:path'

const RUNTIME_TARGETS = {
  'darwin:arm64': {
    os: 'macos',
    arch: 'arm64',
    pythonAbi: 'cp314',
    pythonVersionPrefix: '3.14.',
    executable: 'bin/python3.14',
    requirePosixExecutable: true,
  },
  'win32:x64': {
    os: 'windows',
    arch: 'x86_64',
    pythonAbi: 'cp314',
    pythonVersionPrefix: '3.14.',
    executable: 'python.exe',
    requirePosixExecutable: false,
  },
}

export function runtimeTarget(platform = process.platform, arch = process.arch) {
  return RUNTIME_TARGETS[`${platform}:${arch}`] || null
}

export function assertBundledPluginRuntime(backendPath, {
  platform = process.platform,
  arch = process.arch,
} = {}) {
  const target = runtimeTarget(platform, arch)
  if (!target) {
    if (platform === 'darwin' || platform === 'win32') {
      throw new Error('Desktop Python runtime platform or architecture is unsupported')
    }
    return
  }

  const runtimeRoot = path.resolve(path.dirname(backendPath), 'python-runtime')
  const metadataPath = path.join(runtimeRoot, 'runtime.json')
  let metadata
  try {
    metadata = JSON.parse(fs.readFileSync(metadataPath, 'utf8'))
  } catch (error) {
    throw new Error('Desktop Python runtime metadata is missing or invalid', { cause: error })
  }

  if (
    metadata.schema_version !== 1 ||
    metadata.runtime_type !== 'python' ||
    metadata.os !== target.os ||
    metadata.arch !== target.arch ||
    metadata.python_abi !== target.pythonAbi ||
    typeof metadata.python_version !== 'string' ||
    !metadata.python_version.startsWith(target.pythonVersionPrefix) ||
    metadata.executable !== target.executable ||
    typeof metadata.tree_sha256 !== 'string' ||
    !/^[a-f0-9]{64}$/.test(metadata.tree_sha256) ||
    !Number.isInteger(metadata.tree_file_count) ||
    metadata.tree_file_count < 1
  ) {
    throw new Error('Desktop Python runtime metadata is incompatible')
  }

  const executable = path.resolve(runtimeRoot, metadata.executable)
  const expectedExecutable = path.resolve(runtimeRoot, target.executable)
  const realRuntimeRoot = fs.realpathSync(runtimeRoot)
  let realExecutable
  try {
    realExecutable = fs.realpathSync(executable)
  } catch (error) {
    throw new Error('Desktop Python runtime is missing or not executable', { cause: error })
  }
  if (
    executable !== expectedExecutable ||
    !executable.startsWith(`${runtimeRoot}${path.sep}`) ||
    !realExecutable.startsWith(`${realRuntimeRoot}${path.sep}`) ||
    !fs.existsSync(executable) ||
    !fs.statSync(executable).isFile() ||
    (target.requirePosixExecutable && (fs.statSync(executable).mode & 0o111) === 0)
  ) {
    throw new Error('Desktop Python runtime is missing or not executable')
  }
}
