# WaveFlow Market V1 Product Contract

This document describes the current Market product behavior. It is not an
implementation log and it is not proof that a package is installed.

## Authority

Package/catalog data is the authority for user-visible semantics: `name`,
`description`, `display.subtitle`, `display.summary`, explicit identity,
tags, regions, operators, providers, package type, counts, and metadata.

The frontend owns layout, responsive behavior, tag overflow, filtering
presentation, and install/update runtime state. It must not infer meaning
from a package name, id, region, operator, tag, provider, publisher, or
Plugin manifest.

When a package omits a field, the field stays absent. The only generic visual
fallback is a neutral identity placeholder with no semantic label.

## Information Architecture

Market keeps the current IA:

- Content / Plugins package switch;
- `全部`, `已安装`, and `有更新` quick filters;
- package search and explicit catalog sorting;
- refresh, Market source management, and update actions;
- responsive package grid;
- package detail drawer;
- anchored desktop structured filters and a mobile bottom-sheet filter.

This contract does not add a recommendation area or change package
install/update lifecycle semantics.

## Card Anatomy

The normal card projects package data in this order:

1. explicit identity visual, or neutral placeholder;
2. package `name` exactly as supplied;
3. optional `display.subtitle`;
4. optional `display.summary`;
5. explicit package metric;
6. ordered root `tags`;
7. install, update, installed, or unsupported runtime state.

The card does not show generated source labels, guessed region/provider
identity, transport sentences, capability-derived Plugin tags, or generated
subtitle/summary text. Full `description`, raw machine metadata, and channel
preview remain in the detail drawer.

## Identity And Description

Identity is explicit package data. `display.identity.brand` selects an
existing built-in asset; it is not a detector. An explicit
`display.identity.icon` may select a built-in icon or HTTPS image. An
explicit `display.badge` supplies badge text and tone.

Changing a package name, region, operator, provider, publisher, or scheme
must not change its identity visual unless the package's `display` object
changes.

`description` is the complete detail description. `display.summary` is an
optional short card/detail summary and `display.subtitle` is an optional
explicit subtitle. None of these fields is generated from another field.

## Tags

The package root-level `tags` is the one tag list shared by card and detail.
The frontend preserves the package's original text and order, including
unknown/custom values. It never applies aliases, category inference,
priority, sorting, or hidden semantic filtering.

Cards use `AdaptiveTagList` to render one row according to actual available
width. Only tags that do not fit produce `+N`; there is no fixed `maxItems`
limit. The detail drawer renders the same complete ordered list.

`categories` and `tags` remain separate: categories are controlled filter
taxonomy, tags are package-authored display/discovery labels.

## Metrics

Content cards display only an explicit positive `channel_count` as
`N 个频道`. Logo cards display only an explicit positive `logo_count` as
`N 个台标`. `source_count` remains source count and is never a fallback for
channel count. Plugin cards do not invent a scheme/provider/permission
count.

Index counts describe the package's catalog snapshot. Preview counts are
computed after the manifest is actually parsed and supported sources are
resolved. The UI does not present one metric as another.

## Filters

Structured filters project raw package fields:

| UI label | Package field |
| --- | --- |
| 地区 | `regions[]` |
| 运营商 | `operators[]` |
| 提供方 | `providers[]` |
| 类型 | `kind` |
| 状态 | lifecycle/runtime status |
| 分类 | `categories[]` |
| 标签 | root `tags[]` |

地区 options are built from explicit `country`, `province`, `city`, and
`global` values. The UI does not turn provider/broadcaster values into
operators, and does not alias or discard options.

On desktop, the high-frequency quick filters stay visible. Region,
provider, and type remain anchored controls; operator, status, category, and
tag are available from the anchored `更多筛选` panel. The panel is bounded,
scrollable, preserves the Market context, shows selected state, and closes
on outside click. On mobile, all structured filters remain in the bottom
sheet.

## Search And Sort

The Market package search is the canonical search interaction on the Market
route. The AppShell global search is hidden there because it does not consume
Market package search state.

Backend search covers explicit package fields: id, name, description, regions,
operators, providers, languages, categories, tags, publisher, and for Plugin
packages the publisher and owned schemes. Search does not parse package names
to synthesize a semantic field. Catalog recommendation order uses explicit
`catalog.sort_weight` plus runtime state; it does not hard-code official,
provider, or title heuristics.

## Runtime State

Install, update, installed, unsupported, permission confirmation, loading,
and source refresh states come from backend/runtime state. These interaction
labels may be composed by the frontend, but they are not package metadata.

## Package Authoring Rule

When a package needs new card-facing meaning, add the explicit field to the
package/catalog contract and package JSON first. Do not add a title detector,
tag priority table, alias map, manifest-derived card presenter, or per-package
branch in `MarketView.vue`.
