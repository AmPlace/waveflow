# WaveFlow 自动任务总体架构

> 状态：架构决策
> 决策日期：2026-08-05
> 首个使用模块：Market 自动检查与自动更新
> 适用范围：WaveFlow 后端周期任务、延迟任务及手动触发的后台任务

## 1. 背景

WaveFlow 后续可能需要多种自动任务：

* Market 自动检查和自动更新；
* IPTV 订阅自动刷新；
* EPG 自动刷新；
* Cover、Preview、临时缓存清理；
* 数据库维护；
* 备份任务；
* 应用版本检查；
* 运行状态检测。

这些任务不能分别在各模块中随意调用 `asyncio.create_task()`，否则会产生：

* 生命周期无法统一管理；
* 服务关闭时任务无法安全停止；
* 重复初始化产生多个任务；
* 手动操作和自动操作并发冲突；
* 无法持久化最近运行结果；
* 测试需要真实等待；
* 不同模块重复实现调度、锁、状态和错误处理。

因此 WaveFlow 建立统一的自动任务架构。

---

## 2. 架构目标

自动任务框架必须满足：

1. 由后端服务统一运行，不依赖浏览器页面保持打开。
2. 与 FastAPI 生命周期共同启动和关闭。
3. 一个任务只能有一份调度实例。
4. 自动触发和手动 API 复用相同业务执行入口。
5. 任务配置和最近运行结果可以持久化。
6. 服务异常退出后可以识别上次任务被中断。
7. 支持安全停止，不产生半完成状态。
8. 支持 fake clock、可注入 sleep 等自动测试方式。
9. 新模块可以注册任务，而不是重新编写调度器。
10. 当前保持轻量，不引入 Redis、Celery 或外部消息队列。

---

## 3. 当前部署约束

只读落地审计确认当前正式运行方式如下：

* Docker 使用单个 Uvicorn worker；
* Compose 当前只有一个 backend service；
* Electron 只启动一个内置后端进程；
* 当前没有正式的多 worker、reload 或多 backend replica 启动路径。

因此当前 Docker 和 Electron 正式运行方式满足单进程 Scheduler 假设。首轮 AutomationService 的唯一性将依赖单进程内状态、`asyncio` 锁和数据库条件 claim。

正式部署必须明确：

```text
uvicorn workers = 1
禁止将 reload 用作正式启动方式
禁止多个 backend replica 同时主动调度
```

Docker 部署必须将数据库目录挂载到持久化卷，否则容器重建后任务配置和最近运行状态会丢失。Electron 中的自动任务只在应用内置后端运行期间执行；应用完全退出后不会继续调度。

开发环境手工使用 `--reload` 时可能发生 lifespan 重建，只允许用于本地开发，不得据此假定 Scheduler 具备多进程唯一性。多进程、多副本和集群部署不在本阶段支持范围内。

未来若支持多 worker 或多副本，需要增加以下任一方案：

* 数据库 lease；
* leader election；
* 独立 scheduler worker；
* 外部任务系统。

---

## 4. 总体结构

```text
FastAPI lifespan
    │
    ▼
AutomationService
    ├── AutomationScheduler
    ├── AutomationRegistry
    ├── AutomationRunner
    └── AutomationStateRepository
             │
             ▼
      各模块业务入口
      ├── Market
      ├── Subscription
      ├── EPG
      ├── Cache
      └── Backup
```

### 4.1 首轮代码结构

首轮实际落地采用最小拆分：

```text
backend/automation.py
backend/market_tasks.py
```

职责：

* `automation.py`：通用 Definition、Repository、Runner、Scheduler 和 AutomationService；
* `market_tasks.py`：Market 的 `check`、`auto_update`、`update_all` 业务编排；
* `market.py`：继续保留来源刷新、版本判断、安装、更新和卸载；
* `main.py`：只负责 lifespan 接入和 API 转发。

完整的 `backend/automation/` package 是未来演进目标，只在第二种自动任务接入，或 `automation.py` 已明显增长、继续单文件维护会破坏职责边界时再拆分。当前不以目录形式作为强制要求。

### 4.2 AutomationService

自动任务系统的总入口，负责：

* 初始化 Registry；
* 启动 Scheduler；
* 保存 scheduler task handle；
* 接收配置变更通知；
* shutdown 时停止并等待任务退出；
* 防止重复初始化。

FastAPI 中保存：

```text
app.state.automation_service
app.state.automation_scheduler_task
```

不允许各业务模块自行在 `lifespan` 中创建长期调度协程。

该约束适用于新增任务和后续迁移。当前已经存在的 EPG、Token、Radio 等后台循环属于旧生命周期实现，不在 Market M2 中顺带重构；后续应按独立批次逐步迁入 AutomationService。

### 4.3 AutomationRegistry

保存所有已注册的自动任务定义。

任务模块通过注册声明：

* 任务标识；
* 显示名称；
* 默认是否启用；
* 默认执行周期；
* 启动延迟；
* 执行入口；
* 超时策略；
* 是否允许手动触发；
* 冲突策略。

Registry 只保存定义，不执行任务。

### 4.4 AutomationScheduler

Scheduler 只负责：

* 判断任务何时应该运行；
* 等待启动延迟；
* 根据周期计算下一次运行；
* 响应配置变化；
* 响应 shutdown；
* 调用统一 Runner。

Scheduler 不负责：

* Market 刷新；
* 订阅解析；
* 数据库业务更新；
* 版本判断；
* 任务结果统计。

### 4.5 AutomationRunner

Runner 负责执行一轮任务：

```text
取得任务执行权
→ 持久化 running
→ 调用任务业务入口
→ 收集计数和错误
→ 持久化最终状态
→ 释放执行权
```

所有调用来源必须进入同一 Runner：

* Scheduler 自动触发；
* 管理 API 手动触发；
* 设置页面操作；
* 测试触发。

业务模块不能绕过 Runner 直接创建后台任务。

### 4.6 AutomationStateRepository

负责：

* 自动任务配置；
* 最近一次运行状态；
* run token；
* 开始和完成时间；
* 成功、跳过和失败计数；
* 最近错误；
* 启动时中断状态恢复。

Repository 不保存大型业务结果，也不代替各模块自己的数据表。

---

## 5. 任务定义

每种自动任务使用统一定义，例如：

```python
AutomationTaskDefinition(
    task_id="market_auto_update",
    display_name="Market 自动更新",
    default_enabled=True,
    default_interval_seconds=86400,
    initial_delay_seconds=300,
    handler=run_market_auto_update,
    allow_manual_trigger=True,
)
```

推荐的定义字段：

```text
task_id
display_name
default_enabled
default_interval_seconds
initial_delay_seconds
handler
allow_manual_trigger
minimum_interval_seconds
maximum_interval_seconds
conflict_group
```

`conflict_group` 用于声明互斥域。Market 的 `check`、`auto_update` 和 `update_all` 使用同一互斥域，其他模块是否可以并行由各自任务定义明确决定。

同一 `conflict_group` 内的任务互斥，但这不意味着所有未来自动任务全局串行。未来 EPG、缓存维护、备份等任务应根据数据库、网络或业务资源冲突选择相同或不同的 group。Runner 的非排队 claim 以 `conflict_group` 为互斥单位。

暂不加入：

* Cron 表达式；
* 复杂依赖图；
* 多步骤工作流；
* 优先级队列；
* 分布式任务路由。

---

## 6. 调度语义

### 6.1 服务启动

```text
读取任务配置
→ 将残留 running 状态恢复为 interrupted
→ 创建唯一 Scheduler task
→ 对启用任务等待 initial delay
→ 触发首轮执行
```

Market 首轮延迟默认为 5 分钟。

### 6.2 周期计算

默认从上一轮完成后计算下一周期：

```text
任务完成
→ 等待 interval
→ 执行下一轮
```

不追求严格墙钟时刻。

这样可以避免长任务造成重叠。

### 6.3 配置变化

修改以下配置后通过 `asyncio.Event` 唤醒 Scheduler：

* enabled；
* interval。

语义：

* 关闭任务：取消后续等待，不中断已经进入原子业务步骤的操作；
* 开启任务：重新执行启动延迟，不立即自动运行；
* 修改周期：重新计算等待时间，不立即运行；
* 用户需要立即运行时使用手动操作 API。

### 6.4 服务关闭

shutdown 流程：

```text
设置 stop event
→ 唤醒 Scheduler
→ 不启动下一轮
→ 不开始下一个业务项
→ 当前原子业务项完成或回滚
→ 写入 cancelled
→ await task
→ 清理 app.state
```

等待中的 Scheduler 可以直接取消。已经进入业务 handler 的任务优先采用协作式停止，不应在任意网络请求或数据库提交点粗暴取消；shutdown 可以设置有限宽限时间，超时后由进程退出，并在下次启动时恢复为 `interrupted`。

已经开始业务更新时：

* 不再开始下一个业务项；
* 当前数据库原子操作应完成或回滚；
* 最终状态记为 `cancelled`；
* 进程被强制终止且无法写入状态时，下次启动恢复为 `interrupted`。

普通 shutdown 是协作式停止。SIGKILL、断电和进程崩溃无法保证写入 `cancelled`。下次服务启动时必须在 Scheduler 启动前执行：

```text
running → interrupted
```

恢复时保留上一轮已经写入的计数和业务结果，只修正终止状态、结束时间和结束原因。

---

## 7. 运行状态

统一状态：

| 状态            | 含义         |
| ------------- | ---------- |
| `never_run`   | 从未运行       |
| `running`     | 当前进程正在执行   |
| `success`     | 所有参与步骤成功   |
| `partial`     | 部分成功、部分失败  |
| `failed`      | 任务无法形成有效结果 |
| `cancelled`   | 收到正常停止请求   |
| `interrupted` | 上次进程异常退出   |

规则：

* 没有需要处理的项目不是失败；
* 没有可用更新通常属于成功；
* 一个业务项失败、其他项成功属于 `partial`；
* 全部来源或全部业务项失败可记为 `failed`；
* `running` 必须对应当前有效的 `run_token`。
* `enabled=false` 是调度配置，不是最近一次执行结果；关闭任务不能覆盖最近一次 `success`、`partial` 或 `failed`。

---

## 8. Run Token

每轮任务生成唯一 `run_token`。

用途：

* 作为数据库 claim 的所有权标识；
* 防止旧任务晚到结果覆盖新任务；
* 保证完成状态只能通过 token 条件更新对应的运行；
* 关联日志和单项状态；
* 为未来跨进程 lease 预留演进能力。

状态更新规则：

```text
开始任务：
写入 run_token + running

完成任务：
仅当数据库中的 run_token 仍然匹配时写入最终状态
```

不允许只新增 `run_token` 字段却完全不使用。

`run_token` 是内部运行所有权凭据，管理 API 不对外暴露。

---

## 9. 锁和冲突

### 9.1 Scheduler 实例保护

保证同一进程只能存在一个 Scheduler。

重复调用启动函数时：

* 已存在且未结束：直接返回；
* 已结束：允许重新创建；
* 不产生第二个循环。

### 9.2 Conflict Group 执行权

同一 `conflict_group` 内任务的自动和手动执行不能重叠。Market 的 `check`、`auto_update` 和 `update_all` 共用一个 conflict group。

冲突策略：

```text
已有任务运行
→ 新请求立即返回 busy
→ 不等待
→ 不排队
→ 不启动第二轮
```

busy 返回至少包含：

* task_id；
* 当前 task type；
* started_at；
* 当前状态。

busy 响应不得包含 `run_token`。

不要仅使用：

```python
if lock.locked():
    ...
await lock.acquire()
```

需要由一个原子保护区维护当前运行声明，避免检查和获取之间出现竞态。

### 9.3 业务模块锁

业务模块继续拥有自己的细粒度锁。

例如 Market：

* Market conflict-group task claim；
* Market source refresh lock；
* Market package update lock；
* 数据库事务。

自动任务框架不能复制或替代 M1 已有的 package lock。

---

## 10. 数据库事务原则

自动任务不得从网络请求开始持有一个长 SQLite 事务。

应采用短事务：

```text
短事务：写 running
执行远程请求
短事务：写检查结果
调用业务模块自己的原子事务
短事务：写单项结果
短事务：写全局最终状态
```

原因：

* 避免长时间锁库；
* 不影响播放器和管理请求；
* 单项失败可以隔离；
* 网络超时不会占用数据库事务；
* 业务模块仍能自行保证原子性。

---

## 11. 持久化模型

首轮采用通用专用表，不将自动任务配置或运行状态放入现有 `app_settings` JSON。原因：

* 未来存在多个自动任务；
* task claim、run token 和残留 running 恢复需要结构化条件更新；
* 自动任务状态生命周期不同于普通应用设置；
* task 状态需要独立查询和事务更新。

数据库迁移继续沿用当前 `_SCHEMA` 和幂等 `ALTER TABLE ADD COLUMN` 方式，不在 M2 中引入新的 migration 框架。

### 11.1 通用任务配置

首轮使用通用表：

```text
automation_task_config
- task_id
- enabled
- interval_seconds
- updated_at
```

### 11.2 通用最近运行状态

```text
automation_task_state
- task_id
- run_token
- task_type
- last_started_at
- last_finished_at
- last_status
- checked_count
- updated_count
- skipped_count
- failed_count
- last_error
```

每个 `task_id` 保留最近一次状态。

### 11.3 模块业务状态

模块可以在自己的表中保存额外状态。

例如 Market 安装记录：

```text
last_checked_at
remote_version
version_status
last_update_started_at
last_update_finished_at
last_update_status
last_update_error
```

通用任务表不保存 Market 版本、订阅频道数或 EPG 日期等业务字段。

---

## 12. API 原则

首轮只公开 Market 语义 API：

```text
GET   /api/admin/market/automation
PATCH /api/admin/market/automation
GET   /api/admin/market/automation/status
POST  /api/admin/market/check-updates
POST  /api/admin/market/run-auto-update
POST  /api/admin/market/update-all
```

通用 Automation API 延后到第二种自动任务接入后再决定，首轮不同时建立一套未被使用的通用路由。

现有兼容 API：

```text
POST /api/admin/market/updates/run
```

在 M2 中继续保留，但必须改为转调统一 Runner，不允许绕过 `conflict_group` task claim。

所有 Market 自动任务 API 最终都必须调用同一个 Runner。

API 返回必须区分：

* busy；
* success；
* partial；
* failed；
* invalid configuration。

当前管理 API 默认等待任务完成并返回最终结果，不把重复请求加入隐藏队列。未来如增加显式异步提交接口，才允许返回 `accepted`，并且必须同时返回可查询的运行标识。

busy 使用稳定的 `409 Conflict` 响应，例如：

```json
{
  "detail": {
    "code": "market_task_busy",
    "message": "已有 Market 任务正在运行",
    "current": {
      "task_id": "market_auto_update",
      "task_type": "check",
      "started_at": "2026-08-05T12:00:00Z",
      "status": "running"
    }
  }
}
```

该结构不得包含 `run_token`。

不暴露：

* Python traceback；
* 原始锁对象；
* 数据库内部异常；
* 本地绝对路径。

---

## 13. Market 首个落地实例

Market M2 使用：

```text
task_id = market_auto_update
```

### 13.1 Market Task Runner

支持三种 task type：

#### check

```text
刷新所有启用 Market 来源
→ 获取本轮 source 结果
→ 计算已安装包版本状态
→ 持久化检查结果
→ 不执行安装
```

#### auto_update

```text
执行 check
→ 选择本轮来源成功
→ version_status == upgrade
→ auto_update == true
→ 串行调用 M1 原子更新入口
```

#### update_all

```text
执行 check
→ 选择本轮来源成功
→ 所有 version_status == upgrade
→ 不受单包 auto_update 开关限制
→ 串行调用 M1 原子更新入口
```

### 13.2 stale 来源边界

Market 浏览缓存和自动更新资格必须分开：

```text
上次成功缓存
→ 可以继续用于 Market 页面浏览

本轮刷新失败或 stale
→ 不能用于本轮自动更新
```

`refresh_market()` 必须返回本轮来源结果，例如：

```json
{
  "source_results": [
    {
      "source_id": 1,
      "source_key": "official",
      "source_revision": 5,
      "status": "success",
      "usable_for_update": true,
      "error": null
    },
    {
      "source_id": 2,
      "source_key": "community",
      "source_revision": 3,
      "status": "stale",
      "usable_for_update": false,
      "error": "timeout"
    }
  ]
}
```

每项至少包含：

```text
source_id
source_key
source_revision
status
usable_for_update
error
```

`status` 必须能够表达：

* `success`；
* `failed`；
* `stale`；
* `revision_discarded`。

旧缓存可以继续用于 Market 浏览，但本轮 `failed`、`stale` 或 `revision_discarded` 来源不得参与自动更新。Runner 只能处理 `usable_for_update=true` 来源中的 package。

Runner 不得从合并后的缓存反向猜测本轮来源状态。

### 13.3 Market 业务复用

Market Runner 必须复用：

* M1 source refresh；
* M1 版本判断；
* M1 原地事务更新；
* M1 package lock；
* M1 identity 规则；
* M1 回滚机制。

不得实现第二套安装或更新流程。

---

## 14. 未来任务接入方式

### 14.1 任务类型边界

应注册到 AutomationService：

* Market 自动检查和更新；
* 后续订阅自动刷新；
* 后续 EPG 周期刷新；
* 后续可配置缓存维护和备份。

不应机械注册到 AutomationService：

* 用户请求产生的短任务；
* 播放 session；
* 流式响应重连；
* RTSP FFmpeg 播放进程；
* 请求级 `BackgroundTasks`；
* 媒体资源守护循环。

RTSP HLS 资源清理可以继续拥有独立生命周期。Logo template 启动 one-shot 暂不注册为周期任务。

### 14.2 订阅自动刷新

可注册：

```text
task_id = subscription_auto_refresh
```

业务入口负责：

* 查找启用自动刷新的订阅；
* 逐个使用现有订阅刷新入口；
* 空刷新继续使用已有保护；
* 单订阅失败不阻断其他订阅。

### 14.3 EPG 自动刷新

```text
task_id = epg_auto_refresh
```

负责：

* 刷新启用的 EPG 来源；
* 保留旧数据直到新数据成功；
* 单来源失败隔离；
* 不影响正在播放。

### 14.4 缓存清理

```text
task_id = cache_cleanup
```

负责：

* 清理过期 Preview；
* 清理临时 Cover；
* 不删除仍被引用的数据；
* 保存清理数量和失败信息。

### 14.5 备份

```text
task_id = database_backup
```

备份任务需要额外定义：

* 保存数量；
* 保存目录；
* 保留周期；
* 磁盘空间保护；
* 数据库一致性快照。

备份不在 Market M2 范围内。

---

## 15. 测试规范

所有 Scheduler 测试必须使用：

* fake clock；
* 可注入 sleep；
* 可控 Event；
* 或明确的 scheduler step。

禁止真实等待：

* 5 分钟；
* 24 小时；
* 任意真实长周期。

通用框架至少测试：

1. 重复启动不会创建多个 Scheduler。
2. disabled 任务不自动执行。
3. 启动延迟后执行一次。
4. 周期到达后再次执行。
5. 配置修改能够唤醒等待。
6. shutdown 取消等待。
7. shutdown 不再开始下一业务项。
8. 自动和手动触发冲突返回 busy。
9. busy 不排队。
10. run token 防止旧结果覆盖。
11. 残留 running 启动后恢复为 interrupted。
12. success、partial、failed、cancelled 状态正确。
13. 计数持久化正确。
14. handler 异常不会杀死整个 Scheduler。
15. 单个任务失败不影响其他已注册任务。

模块任务还需覆盖自身业务语义。

---

## 16. 可观测性

每轮任务日志应至少包含：

```text
task_id
task_type
run_token
trigger
started_at
finished_at
status
checked_count
updated_count
skipped_count
failed_count
```

`trigger` 可取：

* scheduler；
* manual_api；
* internal。

错误日志应包含模块上下文，但 API 返回应进行用户友好转换。

未来设置页和诊断页面只读取持久化状态，不依赖进程日志推断任务是否运行。

---

## 17. 安全要求

* 只有管理员可以读取或修改自动任务配置；
* 只有管理员可以手动触发任务；
* interval 必须有上下限；
* 不允许用户通过任务配置注入任意 Python、Shell 或 URL；
* 自动任务只能调用预注册 handler；
* 第三方 Market 包不能注册自动任务；
* Market 仍只导入数据配置，不执行第三方代码。

---

## 18. 当前旧后台协程

只读审计确认当前 lifespan 仍存在使用裸 `asyncio.create_task()` 启动的旧后台任务，包括：

* 动态 Radio token 刷新；
* 云听缓存刷新；
* MyRadio 周期刷新；
* Radio Browser 缓存预热；
* EPG 周期刷新；
* RTSP HLS 资源清理；
* Logo template 启动 one-shot 刷新。

当前这些任务没有统一保存 task handle，shutdown 也没有逐个停止并 await。处理原则：

* Market M2 不顺手迁移这些旧任务；
* Radio、EPG、Token 和缓存预热后续按独立批次逐项评估；
* RTSP HLS 资源清理可以继续使用独立资源生命周期；
* Logo template 启动 one-shot 暂不注册为周期任务；
* 新 AutomationService 必须从首轮开始独立保存 handle、幂等启停并正确 shutdown；
* 旧任务不是 Market M2 的实施阻断项。

后续即使某项不迁入 AutomationService，也应明确其生命周期所有者并补齐 shutdown 清理，不能继续无限增加无 handle 的长期协程。

---

## 19. 明确不采用的方案

当前不采用：

* 浏览器 `setInterval`；
* FastAPI `BackgroundTasks` 作为周期任务；
* 不保存 handle 的裸 `create_task()`；
* 每个模块单独编写 scheduler；
* APScheduler；
* Celery；
* Redis；
* 消息队列；
* 外部 cron 作为唯一实现；
* 多 worker 分布式锁。

外部 cron 未来可以作为服务器部署的可选触发方式，但仍应调用同一个 Runner/API。

---

## 20. 代码组织

首轮实际结构：

```text
backend/
├── automation.py
├── market_tasks.py
├── market.py
├── database.py
└── main.py
```

职责边界以第 4.1 节为准。首轮不建立大量仅有少量代码的空抽象。

未来在第二种自动任务接入，或 `automation.py` 明显增长后，可以演进为：

```text
backend/
├── automation/
│   ├── __init__.py
│   ├── definitions.py
│   ├── registry.py
│   ├── runner.py
│   ├── scheduler.py
│   ├── service.py
│   └── repository.py
├── market_tasks.py
├── market.py
├── database.py
└── main.py
```

该 package 是未来拆分目标，不是 Market M2 的强制目录结构。拆分前后都必须保持 Definition、Repository、Runner、Scheduler、Service 和业务 handler 的职责边界。

---

## 21. M2A 实施拆分

每一步必须单独实现、测试、审核和提交，不把多个根因一次混入同一提交。

### M2A-1：持久化底座

* 建立 `automation_task_config`；
* 建立 `automation_task_state`；
* 为 Market install 增加最近检查和更新状态；
* 实现幂等迁移；
* 实现 claim、run token 条件完成和 `running → interrupted` 恢复；
* 只测试 Repository 和数据库语义。

### M2A-2：通用 AutomationService

* 在 `automation.py` 实现 Definition、Registry、Repository 适配、Runner、Scheduler 和 Service；
* 实现 conflict group 非排队 claim；
* 实现可注入时间等待；
* 实现幂等启动和协作式 shutdown；
* 不接入 Market 业务和管理 API。

### M2A-3：Market 本轮来源刷新结果

* 调整 `refresh_market()` 返回本轮结构化 `source_results`；
* 明确 `success`、`failed`、`stale`、`revision_discarded`；
* 区分浏览缓存和自动更新资格；
* 保持 M1 来源缓存、revision guard 和锁语义不变。

### M2A-4：Market Task Handler

* 在 `market_tasks.py` 实现 `check`、`auto_update` 和 `update_all`；
* 只处理 `usable_for_update=true` 的来源；
* 复用 M1 版本判断、原子更新、package lock 和回滚；
* 逐包串行，并在业务项边界响应 stop event；
* 持久化全局计数和单安装包状态。

### M2A-5：lifespan 与 API

* 在 `main.py` 接入 AutomationService 生命周期；
* 添加首轮 Market 语义 API；
* 保持旧 `/api/admin/market/updates/run` 兼容并转调统一 Runner；
* 实现稳定的配置校验、409 busy 和用户友好错误结构；
* 不修改 Settings、MarketView 或 AdminView UI。

### M2A-6：整体验证和兼容收口

* 验证 Docker 和 Electron 单进程启动；
* 验证 scheduler、API 和手动兼容入口共用 Runner；
* 验证 shutdown、interrupted 恢复和计数持久化；
* 运行 Market 生命周期、数据库和后端全量回归；
* 运行前端现有回归，确认后端变更没有破坏现有 UI；
* 完成本批架构和兼容性审核。

M2A 完成后才能进入 Settings UI。其他任务后续接入时必须：

1. 复用通用框架；
2. 使用模块现有业务入口；
3. 提供独立测试；
4. 明确 conflict group 和失败语义；
5. 不直接在 lifespan 中创建自己的周期协程。

---

## 22. 架构决策结论

WaveFlow 自动任务统一采用：

> FastAPI lifespan 管理的单实例 asyncio AutomationService，内部由 Registry、Scheduler、Runner 和持久化 Repository 组成，各业务模块只注册任务定义并提供可复用业务入口。

当前部署保持单进程。

Market 是首个使用模块，但架构不能与 Market 强耦合。

任何新的周期任务、自动刷新或延迟维护任务，都必须先评估是否应注册到 AutomationService，禁止在模块中自行创建无法管理的后台循环。
