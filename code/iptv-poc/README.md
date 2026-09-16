# IPTV 回看协议 POC

本目录是独立实验，不参与 WaveFlow 生产代码和运行时装配。

## 当前结论

- 大陆运营商 `PLTV` 源没有统一的全国回看协议；公开样本中最常见的是播放 URL 搭配 `playseek=开始时间-结束时间`。
- 有些门户要求把路径从 `PLTV` 改成 `TVOD`，也有来源继续使用 `PLTV`；不能仅凭直播 URL 推断回看 URL。
- 少数门户会在节目单或 Portal 响应中直接返回 `TimeShiftURL`、`TimeShiftLength` 等字段，这比猜参数可靠。
- HTTP/HLS 回看常见为 `starttime/endtime`、`begin/end`、`delay` 或专用 `timeshift` 路径，仍属于服务商约定，不是统一标准。
- 对此前提供的福建联通直播 URL，已确认直播 RTSP 可以建立会话；对 `PLAY` 携带历史 `Range` 的尝试，服务端仍返回当前直播时钟，没有证据表明该 URL 本身支持 RTSP 历史回看。

## 探测范围

`probe_rtsp_variants.py` 会在不打印完整 URL 的情况下尝试：

1. 原始路径的直播播放；
2. 原始路径追加 `playseek`；
3. 将路径段替换为 `TVOD` 后追加 `playseek`；
4. 原始路径追加 `tvdr`；
5. RTSP `Range: npt` 和 `Range: clock` 播放请求。

脚本不会把凭据写入文件，不会把完整请求 URL 输出到终端。

## 本次实测记录

测试日期：2026-08-17，使用运行时传入的两条 CCTV 源，未持久化完整地址。

| 源 | 变体 | RTSP 结果 | 结论 |
| --- | --- | --- | --- |
| CCTV1 | 原始 `PLTV` | `PLAY 200` | 直播会话可建立 |
| CCTV1 | `PLTV + playseek` | `PLAY 200`，返回 `npt` 范围 | 服务端接受回看参数，具备继续验证价值 |
| CCTV1 | `TVOD + playseek` | `PLAY 200` | 该路径变体被接受，不能仅凭 200 证明内容是历史节目 |
| CCTV1 | `PLTV + tvdr` | `PLAY 200` | 参数被接受，尚未证明语义是回看 |
| CCTV1 | RTSP `npt` / `clock` 历史 Range | `PLAY 200`，返回当前 clock | 该直播地址的 Range 没有证明支持历史定位 |
| CCTV2 | 全部上述变体 | `DESCRIBE 302` | 当前探测器未跟随 RTSP 重定向，暂不能判断源能力 |

另外，CCTV1 原始地址和 `playseek` 变体都能识别出 H.264 视频和 MP2 音频。`playseek` 变体仍需通过采样包的媒体时间戳或有限回看结束条件确认真实历史内容，不能把 RTSP 状态码当作最终成功标准。

## WaveFlow 通用回看架构结论

### 1. 不把 URL 猜测当作能力判断

M3U 本身没有一个由所有播放器和运营商共同遵守的官方回看标准。当前最接近的行业约定是 `catchup`、`catchup-source`、`catchup-days`、`catchup-correction`，以及兼容旧播放器的 `tvg-rec`、`timeshift`。Kodi IPTV Simple 已实现 `default`、`append`、`shift` 等模式，但模板占位符仍可能因服务商和播放器不同而变化。

因此，WaveFlow 的判断分成三层：

1. 明确声明：Plugin、Market package 或 M3U `catchup*` 字段明确提供能力；
2. 推断候选：URL 出现 `playseek`、`starttime`、`tvdr`、`TVOD`、`timeshift`、`archive` 等特征；
3. 实际验证：使用一个 EPG 节目时间段发起短探测，并检查媒体时间戳或有限结束条件。

只有第一层或第三层成功，才能在产品中显示“支持回看”。第二层只能是 `needs_verification`，不能自动承诺可用。

### 2. 统一能力模型

回看能力应绑定频道的具体 source，而不是只绑定逻辑频道。建议统一为：

```text
ReplayCapability
  support: supported | unsupported | unknown | needs_verification
  origin: plugin | market | m3u | url_hint | probe
  mode: append | default | shift | provider | native_dvr
  template
  max_days
  timezone
  correction_seconds
  source_revision
  verified_at
  last_error
```

用户请求统一使用 EPG 的 UTC `start_at`、`end_at` 和 `programme_id`。核心层不理解 `playseek`、`TVOD` 或 `tvdr`，这些由 M3U 模板或 Plugin/provider resolver 负责转换。

### 3. 能力解析优先级

```text
Plugin 明确能力
→ Market package 明确能力
→ M3U catchup 模板
→ URL 启发式候选
→ 用户主动验证
→ unknown
```

Plugin 继续使用现有 `tv.resolve_stream` 查询链路，只需把回看请求作为标准化的 `mode=replay`、`start_at`、`end_at` 输入。Plugin 自己负责生成运营商 URL、HTTP API 或其他播放描述；WaveFlow 不复制运营商规则。

M3U 解析器应保留并规范化：

```text
catchup
catchup-source
catchup-days
catchup-correction
tvg-rec
timeshift
```

同时保留未知属性，避免未来播放器或 package 扩展被静默丢弃。`catchup-source` 必须明确是完整 URL 模板还是追加到直播 URL 的 query 模板。

### 4. URL 特征的安全语义

| 特征 | WaveFlow 语义 |
| --- | --- |
| `playseek`、`starttime/endtime`、`begin/end`、`tvdr` | 很可能是回看参数或模板 |
| `/TVOD/`、`/timeshift/`、`/archive/` | 很可能存在专用回看入口 |
| `/PLTV/` | 只能说明常见运营商直播路径，不能证明支持回看 |
| `accountinfo`、`tenantId` | 认证或租户字段，不提供回看能力证据 |
| RTSP 接受 `Range` | 只能说明请求被接受，必须验证实际媒体时间 |
| HLS `EXT-X-PROGRAM-DATE-TIME` | 能建立时间映射，不等于历史片段仍然保留 |

对福建联通这类源，`PLTV + playseek` 和 `TVOD + playseek` 应进入候选模板，但不能因为返回 `PLAY 200` 就自动标记为确定支持。

### 5. FullPlayer 播放语义

FullPlayer 不直接拼接回看 URL，而是提交 `ReplayRequest`：

```text
source_id + programme_id + start_at_utc + end_at_utc
```

后端根据 capability 生成新的 replay 播放描述，再复用现有 source、UA、代理、认证和 signed handle 流程。回看和直播是两个播放意图；切回直播时重新解析原始 live source，不修改原始 source URL。

首版不建设本地录像机，也不把当前 RTSP→HLS 的滚动 15 片段窗口误认为历史回看。没有明确能力时不显示确定性的回看按钮；候选能力可以提供单独的“尝试验证”入口。

### 6. HLS/RTSP 边界

HLS 的 `EXT-X-PROGRAM-DATE-TIME` 只提供媒体片段和绝对时间的映射，`EXT-X-PLAYLIST-TYPE:EVENT` 表示列表可持续追加，二者都不能单独声明运营商保留了多少天的回看。RTSP 的 `npt`、`clock` Range 也必须由具体服务端验证。

### 7. 推荐实施顺序

1. M3U parser 保留并持久化 `catchup*` 能力字段；
2. 建立 source 级 `ReplayCapability` 和统一 `ReplayRequest`；
3. 先实现 M3U `append`、`default` 与 Plugin provider；
4. FullPlayer 接入统一 replay resolve，保持现有 direct/proxy/UA/auth 语义；
5. 再增加 URL 候选识别和显式验证缓存；
6. 最后针对 PLTV/TVOD、HTTP/HLS 和各运营商补 provider/package 适配。

这个方案兼容普通 M3U、Market package 和 Plugin，又不会因看到 `PLTV` 或 `playseek` 就误报回看能力。

## 运行

使用新鲜、可撤销的测试 URL，通过环境变量传入：

```bash
IPTV_RTSP_URL='rtsp://...' python3 code/iptv-poc/probe_rtsp_variants.py
```

时间窗口默认取当前时间前 10 分钟到前 5 分钟；可用 `IPTV_POC_END_OFFSET_MINUTES` 调整结束点。回看成功不能只看 HTTP/RTSP `200`，必须同时确认返回的媒体时间范围或收到的媒体内容确实落在请求窗口内。

## 安全边界

- 不要把含 `accountinfo`、token、Cookie 或认证参数的 URL 写入 Git、日志或工单。
- 当前文档不保存福建联通的完整地址，只记录协议行为摘要。
- 如果直播凭据长期有效，应在测试后刷新或撤销。

## 参考

- [运营商 Portal 时移字段样本](https://www.right.com.cn/forum/thread-4055146-1-1.html)
- [河北电信 PLTV/playseek 样本](https://www.right.com.cn/forum/forum.php?mod=viewthread&tid=4048997)
- [上海电信 playseek 样本](https://gitee.com/mirrors_lucifersun/China-Telecom-ShangHai-IPTV-list/blob/master/README.md?skip_mobile=true)
- [APTV 对 PLTV/TVOD/playseek 的说明](https://docs.aptvapp.com/play/playseek)
