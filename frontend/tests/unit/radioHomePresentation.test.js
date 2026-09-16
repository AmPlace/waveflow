import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'

const frontendRoot = path.resolve(new URL('../..', import.meta.url).pathname)
const homeSource = fs.readFileSync(path.join(frontendRoot, 'src/views/Home.vue'), 'utf8')
const bottomPlayerSource = fs.readFileSync(path.join(frontendRoot, 'src/components/BottomPlayer.vue'), 'utf8')

test('Radio Home exposes distinct catalog loading, empty, error, and degraded states', () => {
  assert.match(homeSource, /fetchRadioCatalog/)
  assert.match(homeSource, /radioCatalogState === 'error'/)
  assert.match(homeSource, /radioCatalogState === 'empty'/)
  assert.match(homeSource, /radioCatalogState\.value === 'stale'/)
  assert.match(homeSource, /radioCatalogState\.value === 'degraded'/)
  assert.match(homeSource, /电台目录暂时无法加载/)
  assert.match(homeSource, /暂无可用电台/)
})

test('Radio Home guards catalog/programme responses and keeps selection identity explicit', () => {
  assert.match(homeSource, /let radioCatalogRequestSeq = 0/)
  assert.match(homeSource, /requestSeq !== radioCatalogRequestSeq/)
  assert.match(homeSource, /disposed\s*\n\s*\|\|\s*requestSeq !== programmeRequestSeq/)
  assert.match(homeSource, /:aria-current="isCurrentStationSelected\(item\.station\.id\)/)
  assert.match(homeSource, /'channel-card-selected': isCurrentStationSelected/)
  assert.match(homeSource, /'channel-card-current': isCurrentStationPlaying/)
})

test('BottomPlayer keys artwork by current station identity to prevent stale fallback state', () => {
  assert.match(bottomPlayerSource, /:key="displayIptvChannel\?\.canonical_key \|\| currentStation \|\| 'empty'"/)
})
