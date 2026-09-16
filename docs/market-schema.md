# WaveFlow Market Package Schema V1

WaveFlow Market V1 is a breaking package contract. Existing project-owned
packages are migrated to this contract; the backend does not provide a
compatibility adapter for removed package-level fields.

The authority boundary is:

```text
Package / Market catalog owns semantics
Backend validates and safely projects data
Frontend owns presentation, responsive layout, and runtime state
```

## Market Index

`market.json` is the listing/index layer. It contains package metadata needed
for listing, filtering, search, card presentation, and update checks. It must
not contain executable import configuration such as `defaults`,
`channel_sources`, `inline_channels`, `channels`, `channels_url`, `headers`,
`assets`, or `logos`.

An index package requires `id`, `name`, `description`, `kind`,
`package_type`, `version`, and `updated_at`. Other fields are optional and
are projected as empty or absent when not supplied.

```json
{
  "schema_version": 1,
  "market_version": "2026.08.23",
  "updated_at": "2026-08-23T00:00:00Z",
  "packages": [
    {
      "id": "japantv-public",
      "name": "Japan Public TV",
      "description": "Complete description shown in package details.",
      "kind": "dynamic_playlist",
      "package_type": "content_package",
      "version": "2026.08.23",
      "updated_at": "2026-08-23T00:00:00Z",
      "manifest_url": "packages/japantv-public/manifest.json",
      "regions": [{"country": "JP", "province": null, "city": null}],
      "operators": [],
      "providers": ["NHK"],
      "languages": ["ja-JP"],
      "categories": ["国际", "电视"],
      "tags": ["Japan", "Public", "HLS"],
      "channel_count": 35,
      "source_count": 35,
      "status": "stable",
      "source_origin": "community",
      "source_policy": "community_index",
      "risk_level": "low",
      "publisher": {"id": "org.example", "name": "Example"},
      "published_at": "2026-08-23T00:00:00Z",
      "compatibility": {"min_waveflow_version": "0.1.0"},
      "links": {"source": "https://example.com/source"},
      "catalog": {"sort_weight": 0, "featured": false},
      "display": {
        "subtitle": "Public Japanese TV",
        "summary": "A short package-owned card description.",
        "identity": {
          "brand": "waveflow",
          "icon": {"type": "builtin", "name": "waveflow"}
        },
        "badge": {"text": "JP", "tone": "sky"}
      }
    }
  ]
}
```

## Package Identity And Type

`kind` is the stable machine enum used by the current implementation:

| `kind` | Meaning | `package_type` |
| --- | --- | --- |
| `playlist` | Static or inline content package | `content_package` |
| `dynamic_playlist` | Remote playlist/content package | `content_package` |
| `mixed` | Content package combining supported source forms | `content_package` |
| `logo_pack` | Channel logo package | `content_package` |
| `plugin_package` | Signed WaveFlow Plugin distribution package | `plugin_package` |

`package_type` is a separate top-level contract for lifecycle routing. A
`plugin_package` must use `kind: plugin_package`; content packages cannot use
that kind. UI labels are localization of these enums, not inference.

## Region, Operator, Provider, Publisher

`regions` is an ordered array. Each item is either a global marker or a
structured location:

```json
"regions": [
  {"country": "CN", "province": "福建", "city": null},
  {"country": "HK", "province": null, "city": null},
  {"global": true}
]
```

`country` is an explicit ISO-style country/territory code. `province` and
`city` are explicit strings or `null`. Multiple entries express
multi-region content. `{ "global": true }` expresses global scope and cannot
be combined with another location in the same item.

These fields have different owners and must not be merged:

| Field | Meaning |
| --- | --- |
| `operators` | Network/telecom operators only, for example `中国移动`, `中国联通`, `中国电信` |
| `providers` | Content, broadcast, platform, or service provider, for example `YouTube`, `ARD`, `ZDF`, `TDM` |
| `publisher` | Organization or person maintaining and publishing the package; `{id,name}` |
| `market_source` | Backend-added source identity describing which Market source supplied the package |

Region names, broadcasters, platforms, and provider names must not be placed
in `operators` merely to make a filter option appear.

Removed package-level fields are rejected: `region`, `language`, and
`provider`. Channel-level fields under `defaults.channel` remain channel
metadata and are not package fields.

## Languages, Categories, And Tags

`languages` is an ordered array of explicit locale values such as `zh-CN`,
`en-US`, `ja-JP`, or `zh-HK`. The frontend never guesses language from
region.

`categories` is a controlled taxonomy for stable navigation and filtering.
The current taxonomy is:

```text
央视, 卫视, 地方, 新闻, 体育, 电影, 少儿, 教育, 纪实, 广播, 国际,
港澳台, 购物, 剧场, 动画, 音乐, 综艺, 戏曲, 财经, 生活, 影视, 电视,
插件, Provider, 健康, 宗教, 民族, 韩流
```

`tags` is the single package-owned user-visible tag list. Its text and order
are authoritative. Tags may be more free-form than categories and may
overlap with them. The frontend:

- preserves text and order;
- keeps unknown and custom tags;
- does not alias, classify, prioritize, sort, or hide tags;
- does not copy tags into `display`;
- uses the same root list for card, detail, and tag filtering.

There is no second `display.tags` contract.

## Presentation Contract

`display` is optional package-owned card presentation. The backend performs
safe projection and rejects unsafe URLs/control content; it does not invent
missing values.

| Field | Meaning |
| --- | --- |
| `display.subtitle` | Explicit short card subtitle |
| `display.summary` | Explicit short card summary |
| `display.identity.brand` | Explicit built-in asset key |
| `display.identity.icon` | Explicit `{type: builtin, name}` or HTTPS image URL |
| `display.badge.text` | Explicit text badge, at most three characters |
| `display.badge.tone` | Explicit tone: `neutral`, `rose`, `sky`, `emerald`, `orange`, or `violet` |

The built-in brand registry is an asset lookup only. A title containing
`YouTube`, a region containing `福建`, or an operator value cannot activate a
brand or badge. Missing `subtitle`, `summary`, `identity`, or `badge` stays
missing; the frontend may show only a neutral, non-semantic placeholder.

`description` is the complete package description for the detail view. It is
not silently replaced by `display.summary` or generated from the name.

## Lifecycle And Distribution Metadata

`status` is one of `active`, `experimental`, `stable`, `deprecated`,
`broken`, or `unknown`. A deprecated package may set `replacement` to the
recommended package id.

`published_at` and `updated_at` are package timestamps. `compatibility` is a
package-owned object; the standard current key is
`min_waveflow_version`. `links` may contain `homepage`, `documentation`,
`source`, and `issues`, each as an HTTPS URL without credentials.

`catalog.sort_weight` and `catalog.featured` are Market index metadata. They
control catalog ordering/curation only; a package cannot use them to change
runtime support or installation semantics.

## Metrics

Metrics never fall back across semantic types:

| Package | Card metric |
| --- | --- |
| Content package | `channel_count` -> `N 个频道` |
| Logo package | `logo_count` -> `N 个台标` |
| Plugin package | No invented count; package may provide explicit wording through `display` |

`source_count` means source count only. It is never displayed as channel
count, and a missing/zero `channel_count` does not become `N 个源`.

The count in the Market index is catalog metadata. A preview reads the
manifest and resolves/filters sources, then reports the actual parsed
`channel_count` and `source_count` for that preview. These are different
lifecycles and are not silently written back into the index.

## Manifest And Source Contract

`manifest.json` is fetched lazily for preview/import. It contains executable
configuration such as `defaults`, `channel_sources`, inline channels,
playlist URLs, and source headers. It may repeat package metadata so the
backend can validate the loaded package, but runtime fields do not generate
card semantics.

V1 supports `channel_sources[].type` values `playlist` and
`inline_channels`. Remote playlist files are parsed as channel lists; a
channel may contain multiple playback sources. Channel-level
`defaults.channel` metadata remains distinct from package-level
`regions/operators/providers/languages`.

The index must not leak private execution configuration. The backend removes
internal artifact paths and source execution inputs from public projections.

## Backend Projection And Search

Backend normalization validates enums, arrays, locales, URL safety, package
types, lifecycle values, and display members. `_package_card()` projects the
new metadata to both list and detail responses without deriving semantic
values.

Market search uses explicit fields: `id`, `original_id`, `name`,
`description`, `regions`, `operators`, `providers`, `languages`,
`categories`, `tags`, `publisher`, and Plugin publisher/scheme metadata.
Filters map directly to `regions`, `operators`, `providers`, `kind`,
`status`, `categories`, and `tags`.

Missing fields result in empty options or absent presentation, not inferred
fallback values. Invalid package items are represented as unsupported items
with a schema warning so one bad item does not make the whole source empty.
