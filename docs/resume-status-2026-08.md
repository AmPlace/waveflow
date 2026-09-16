# WaveFlow 恢复状态（2026-08-04）

## 恢复基线

- 恢复分支：`resume-2026-08-04`
- 暂停开发前最后提交：`4c77a42`，2026-06-18 09:43:20 +0800
- 本次首先整理并提交的现场：`c1cb8ae`，IPTV 首页异步请求 latest-wins
- README 与原有文档最后集中更新于 2026-06-16，不能代表当前播放器实现

## 当前验证矩阵

| 模块 | 当前实现 | 已验证 | 仍缺 |
| --- | --- | --- | --- |
| Security/Auth | 浏览、媒体凭证、signed handle、`source_id` 入口已迁移 | 后端单测 | 最终部署硬化与文档复核 |
| 普通 HLS | `channel/{key}/playlist.m3u8` 薄缓存重写 | 德化 60 秒、建宁真实播放 | 更多上游与长时间 soak |
| 短窗口 HLS | hls.js count 配置与恢复阈值已调整 | 泉州新闻 60 秒连续推进 | iOS 真机、30–60 分钟 soak |
| Adapter HLS | `/resolve` 直连 + `source_id` 代理兜底 | 福建综合 45 秒连续推进 | Adapter 失败/换 transport 专项 E2E |
| HTTP-FLV | mpegts.js + stream handle 路由已接通 | 天祝电视台 45 秒连续推进，直连与代理手动切换均通过 | 更多 Adapter HTTP-FLV 与长播 |
| MPEG-TS | mpegts.js 与 stream handle 代码存在 | 单元/后端逻辑测试 | 当前数据无真实样本 |
| RTSP | FFmpeg RTSP→HLS session manager 已存在 | 单元逻辑与代码审计 | 当前数据无真实样本、缺进程生命周期实测 |
| YouTube | iframe 与 adapter 解析代码存在 | 解析/身份单测 | 当前数据无真实样本 |
| MyRadio | 连续音频走 `/channel/{id}/stream` | 北部 FM 修复后 30 秒连续播放 | 多源回退与长播 |
| Hedged race | 直连立即、代理 2.5 秒后加入、45 秒 loser TTL | 德化/福建/建宁浏览器实测，策略单测 | 完整状态机自动化与取消泄漏测试 |
| HLS playlist | Thin playlist fetch + signed-handle rewrite | 代表 HLS 已实播 | 更多上游与长时间 soak |

## 本次恢复发现并修复

- MyRadio 新安全入口错误地先调用动态 fetcher，`mr_*` 缓存源返回 404；改为优先读取预热缓存。
- 测速 `offline/error/timeout` 不再禁止播放，只参与排序和提示；建宁被标记 offline 但实际可以播放，证明该策略必要。
- Adapter 的代理副本重新参加 2.5 秒 hedged race，不再等待直连彻底失败或被整轮跳过。
- 多源菜单手动切换保持精确 `source_id`；旧源状态会切为 stopped，不再同时显示多个“当前可播”。
- 音频并发探测不再被第一个快速失败源提前结束。

## 已知未闭环

- FullPlayer 的 hedged race 仍缺覆盖“延迟代理启动、切频道取消、winner 后彻底销毁、正式起播二次失败”的组件级自动化测试。
- 正式 HLS 起播目前仍可在首个 `FRAG_LOADED` 后进入播放确认；race 本身已有 buffer + `currentTime` 推进确认，两套标准尚未完全统一。
- `raceDirectHlsSources`、`raceProxySources` 等旧 race 实现仍留在 `FullPlayer.vue`，需要确认无调用后删除。
- 数据库 `channels` 当前没有独立的 source `enabled/disabled` 字段；代码已支持该语义，但管理端若需要人工禁用单源，还要补数据模型和 API。
- MyRadio 的 channel stream 不响应 HEAD，前端可达性预检会看到 405 后再用 GET 实播；不影响播放，但可继续收敛。
- 后端启动时泉州经济生活 92.3 的旧 API 已失效；EPG `epg.51zmt.top` 当前也返回非 gzip 内容。
- 当前真实订阅没有 MPEG-TS、RTSP、YouTube 成功样本，不能标记为浏览器回归通过。

## 下一步顺序

1. 审核并提交本次播放器/音频修复。
2. 为 FullPlayer hedged race 抽出可测试状态机或组件 harness。
3. 补齐 MPEG-TS、RTSP、YouTube 的真实成功样本回归。
4. 对德化、泉州新闻、福建综合做 30–60 分钟 soak，并补 iOS 真机验证。
5. 代表源稳定后，补充 Thin HLS 的长时间 soak 与失败恢复验证。
6. 最后更新 README、安全审计和 CI 回归说明。
