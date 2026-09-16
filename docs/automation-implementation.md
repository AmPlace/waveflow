# WaveFlow 自动任务架构实施记录

> 本文记录自动任务架构各落地批次的背景、成因、方案、实现、边界和验证结果。
> 架构决策以 `docs/automation-architecture.md` 为准；本文只记录已经实际完成的能力，不把后续计划写成已实现。

## M2A-1：自动任务持久化底座

### 背景与成因

Market 自动检查和自动更新需要跨进程重启保存配置及最近运行状态。仅依赖进程内 `asyncio.Lock` 无法处理异常退出，也无法阻止旧运行结果覆盖新运行，因此需要数据库级执行权和状态所有权。

### 采用方案

- 使用独立通用表 `automation_task_config` 保存任务配置。
- 使用 `automation_task_state` 保存当前或最近一次运行状态。
- 使用 `conflict_group` partial unique index保证同一互斥域最多一条 `running`。
- 使用 `BEGIN IMMEDIATE` 在短事务内完成非排队 claim。
- 使用 `task_id + run_token + running` 条件更新进度和最终状态。
- 启动恢复时将残留 `running` 转换为 `interrupted`。
- 为 Market install 增加最近检查和更新状态，并使用单包 run token 防止晚到结果覆盖。

### 实际落地

- 修改 `backend/database.py`，沿用 `_SCHEMA` 和幂等 `ALTER TABLE ADD COLUMN`。
- 新增配置、claim、progress、complete、busy 查询和 interrupted 恢复数据库入口。
- 新增 Market install 检查及更新状态入口。
- 保持 M1 package 原地更新、subscription/channel 行和公开 `source_id` 不变。

### 验证结果

- 新增 `backend/tests/test_automation_persistence.py`。
- 覆盖新库初始化、旧库升级、重复初始化、配置持久化、并发 claim、run token、interrupted 恢复、Market install 状态和 M1 事务兼容。
- 后端全量回归通过。

### 边界

本批未实现 Runner、Scheduler、长期后台任务、lifespan 或 API。

## M2A-2a：通用 Automation Runner 与执行模型

### 背景与成因

M2A-1 只提供数据库所有权。如果 Scheduler、管理 API 和业务模块各自拼接 claim、handler、状态计算和 complete，会形成多套执行路径，导致 busy 语义、取消处理、错误净化和 run token 条件完成不一致。因此需要一个不负责调度的统一单轮 Runner。

### 采用方案

- 使用 dataclass 定义任务、运行请求、handler 结果、运行结果、busy、配置和状态模型。
- 使用显式 `AutomationRegistry` 注册唯一 `task_id`，不做插件扫描或动态 import。
- 使用 `AutomationRepository` 薄封装 M2A-1 数据库入口，不复制 SQL 和事务。
- `AutomationTaskContext` 提供 stop 查询和受 run token 保护的 progress 上报。
- `AutomationRunner.run()` 作为 scheduler、API 和内部调用未来的唯一执行入口。
- Runner 只同步执行一轮任务，不等待、不排队、不创建后台 task。

### 执行流程

```text
查找 definition
→ 校验 task_type、trigger 和配置一致性
→ 生成唯一 run token
→ 数据库原子 claim
→ busy 时立即返回
→ 创建 context 并执行 handler
→ 统一计算 success / partial / failed / cancelled
→ run token 条件完成
→ 返回统一结果
```

### 状态和所有权语义

- 无失败，包括没有可处理项目：`success`。
- 有有效工作且部分业务项失败：`partial`。
- 全部必要工作失败、handler 异常或非法结果：`failed`。
- 协作式停止或 handler 在安全边界返回取消：`cancelled`。
- progress 或 complete 的 token 条件更新失败时抛出所有权丢失，不能返回虚假的 success。
- `AutomationBusy` 和 `AutomationRunResult` 不暴露 run token。

### 异常和取消

- 普通异常转换为 `failed`；数据库和返回结果只保存净化后的简化错误。
- 错误去除 NUL、限制长度并隐藏本地绝对路径，不暴露 traceback。
- 强制 `CancelledError` 会尽力写入 `cancelled`，随后继续传播取消。
- 测试发现并修复了一个取消竞态：当取消时 run token 已失效，状态写入失败不能用所有权异常替换原始 `CancelledError`。

### 实际落地

- 新增 `backend/automation.py`。
- 新增 `backend/tests/test_automation_runner.py`。
- 未修改 `backend/database.py`、`backend/main.py`、Market 业务或前端。

### 测试先行证据

1. 创建 Runner 测试后，当前 HEAD 因不存在 `automation.py` 稳定出现 15 个导入错误。
2. 初版实现后新增 token 已失效的强制取消测试，稳定复现 `AutomationOwnershipLostError` 替换 `CancelledError`。
3. 最小修复后 Runner 定向测试 17 项全部通过。

### 验证结果

- Runner 定向测试：17 passed。
- Runner、M2A-1 持久化和 Market 生命周期：52 passed。
- 后端全量：273 passed，54 subtests passed。
- `git diff --check` 和 Python 编译检查通过。
- 确认生产实现中不存在 Scheduler、AutomationService、`asyncio.create_task()` 或周期等待。

### 边界

本批未实现 Scheduler、AutomationService 生命周期、FastAPI lifespan、Market task handler 或 API。

## M2A-2b：通用 AutomationScheduler 与 AutomationService 生命周期

### 背景与成因

M2A-2a 已经统一了单轮任务的 claim、run token、handler 执行和状态结算，但仍没有长期调度生命周期。如果各业务模块自行使用 `asyncio.create_task()` 和 `asyncio.sleep()`，会再次出现重复 Scheduler、不可控 shutdown、配置变化不生效、后台异常无人读取，以及手动任务与自动任务走不同执行路径的问题。因此本批只补通用调度和 task handle 所有权，不接入任何 Market 业务。

### 采用方案

- `AutomationTaskDefinition` 增加 `scheduled_task_type` 和 `allow_automatic_scheduling`。
- 自动调度默认关闭；启用时必须明确 scheduled task type，且该类型必须属于 `allowed_task_types`。
- `AutomationEventWaiter` 统一等待 timeout、config changed 和 stop 三种结果。
- `AutomationScheduler` 一个实例只负责一个 definition，只读取配置、等待和调用统一 Runner。
- `AutomationService` 是长期 Scheduler task handle 的唯一所有者，负责 interrupted 恢复、幂等 start/stop、配置唤醒、异常观察和可选强制取消。
- Runner、数据库 claim、run token 和完成状态语义保持不变。

### Scheduler 时间语义

```text
enabled 首次启动
→ 读取持久化配置
→ 等待完整 initial delay
→ Runner.run(trigger=scheduler)
→ 从本轮完成时开始等待 interval
→ 下一轮 Runner
```

- disabled 时使用无超时等待，不运行 Runner，也不 busy loop。
- disabled → enabled 后重新等待完整 initial delay，不立即执行。
- interval 修改会中断当前周期等待，重新读取配置并从头等待新 interval。
- initial delay 中只修改 interval 不重置初始 deadline；关闭任务会退出 initial 阶段，重新启用后重新等待完整 initial delay。
- Runner 返回 success、partial、failed 或 busy 后都进入正常 interval，不排队、不立即重试。
- Runner 返回 cancelled 或 stop event 已设置时退出 Scheduler。
- Repository/Runner 基础设施异常记录日志并等待默认 60 秒受控退避，避免高速重试；definition 与持久化 conflict group 不一致属于终止性配置错误。

### Waiter 和资源清理

生产 waiter 使用两个短期 task 分别监听 stop event 和 config event，并使用 `asyncio.wait(..., timeout=delay)` 实现可取消等待。返回前会取消并 await 所有未完成的内部 task，不遗留 pending task。测试使用可控 waiter 显式释放 timeout、reconfigured 和 stopped，不真实等待 5 分钟或 24 小时，也不 patch 全局 `asyncio.sleep()`。

### Service task handle 所有权

- Service 保存 `task_id → AutomationScheduler` 和 `task_id → asyncio.Task`。
- 长期 Scheduler 的 `asyncio.create_task()` 只出现在 Service 启动路径。
- start 先执行 `running → interrupted` 恢复，恢复完成前不创建任何 Scheduler task。
- start 和 stop 均受 lifecycle lock 保护并保持幂等。
- stop 设置每个 Scheduler 的共享 stop event，等待当前 Runner 协作退出。
- 只有调用者显式提供 timeout 时，超时后才强制 cancel task；取消继续传播到 Runner，保留其 cancelled 尽力落库语义。
- stop 后清空 Scheduler 和 task handle，可在测试或新生命周期中重新 start。
- Scheduler 异常由 done callback 主动读取并记录，不产生 `Task exception was never retrieved`，也不取消其他 Scheduler。

### 配置变化通知

`AutomationService.notify_config_changed(task_id)` 只设置目标 Scheduler 的 Event。Event 只作为合并唤醒信号，Scheduler 醒来后始终从 Repository 重新读取真实配置。重复通知会自然合并，不携带配置快照，也不会唤醒其他任务。

### 实际落地

- 修改 `backend/automation.py`：补充调度字段、只读 definition 列表、Waiter、Scheduler 和 Service。
- 新增 `backend/tests/test_automation_scheduler.py`。
- 最小调整 `backend/tests/test_automation_runner.py`：将“Runner 不包含后台循环”的源码检查限定到 Runner 类本身。
- 未修改 `backend/database.py`、`backend/main.py`、`backend/market.py` 或任何前端文件。

### 测试先行证据

1. 在生产代码修改前新增 Scheduler/Service 定向测试。
2. 当前 HEAD 因 `AutomationTaskDefinition` 不支持 `scheduled_task_type`，8 个测试稳定失败。
3. 初版实现后测试进一步暴露 mock Runner 调用形态和异步等待夹具的时序问题；修正测试夹具后未发现需要改变业务策略的额外生产缺陷。
4. 扩展测试覆盖 initial/interval/disabled/config change、stop/cancel/busy/failed、waiter 清理、Service handle、interrupted 恢复、任务隔离、异常观察、配置冲突和强制取消。

### 验证结果

- Scheduler/Service 定向与 Runner 测试：38 passed。
- Scheduler、Runner、持久化和 Market 生命周期组合：73 passed，4 subtests passed。
- 后端全量：294 passed，54 subtests passed。
- Python 编译检查和 `git diff --check` 通过。
- 全量端口型 HLS 集成测试在允许绑定 localhost 端口的环境运行通过；沙箱内失败原因为 Uvicorn 无法启动，不是代码回归。

### 边界

本批未实现 Market task handler、`market_tasks.py`、FastAPI lifespan、管理 API、前端设置页或旧 Radio/EPG 后台任务迁移。AutomationService 目前只是可复用通用底座，尚未在应用启动时创建，也不会自动执行任何业务任务。

## M2A-3：Market 本轮来源刷新结果契约

### 背景与成因

M1 的来源级缓存允许远端 Market 暂时失败时继续展示上一次成功 package，这是正确的浏览降级行为。但原 `refresh_market()` 只返回合并后的 `market_summary()`，调用者无法区分“本轮真实成功”和“使用历史 stale cache”。如果自动更新据此判断来源资格，旧缓存会被误认为本轮成功数据；revision guard 丢弃的晚到请求也没有显式结果，下一阶段无法可靠计算 success、partial 或 failed。

### 采用方案

- 保留 `market_summary()` 的原有字段，在单次 `refresh_market()` 返回值中追加本轮局部结果，不使用模块级“最后一次刷新结果”。
- 每个涉及来源返回 `source_id`、`source_key`、名称、请求 URL、当前 URL、请求/当前 revision、状态、更新资格、错误、cache 是否更新和本轮接受的 package identity。
- 顶层追加 `refresh_status`、固定状态计数以及各状态的 source ID 集合。
- 使用 `usable_for_update` 作为后续 Market Task Handler 的唯一来源更新资格，不允许从长期 cache 猜测。
- 延续 `source_key::original_package_id` identity；官方来源继续使用原始 package ID。
- 显式单源刷新 disabled 来源时返回 `disabled`，不执行网络请求；全量刷新仍只处理 enabled 来源。

### 状态语义

- `success`：本轮 fetch、schema/package 解析成功，前后 revision 一致，来源仍启用，结果已写入 cache；仅此状态 `usable_for_update=true`。
- `stale`：本轮失败，但同 source、同 URL 存在历史成功 cache；旧数据继续用于浏览，本轮不具备更新资格。
- `failed`：本轮失败且不存在历史成功 cache；返回结构化失败，不再只抛异常。
- `revision_discarded`：请求开始前或结果返回时来源已删除、禁用、改 URL 或配置 revision；旧结果不写 cache，也不更新当前来源成功/失败状态。
- `disabled`：显式请求了 disabled 来源，刷新未执行；作为非错误跳过返回，不能参与更新。

顶层状态按本轮结果计算：存在 success 且另有 stale/failed/revision discarded 为 `partial`；没有 success 且存在上述失败状态为 `failed`；其余为 `success`。该状态是 Market 刷新契约，不复用 Automation 的持久化状态枚举。

### 本轮结果与长期 cache 边界

- source result 只存在于当前函数返回值，并发调用各自持有独立列表。
- source result 的 `package_ids` 只包含本轮成功接受的数据；stale cache 中仍可浏览的旧 package 不会伪装为本轮 package。
- 成功 cache 写入 `last_success_at` 和 `has_successful_cache=true`。
- 首次失败创建的空错误 entry 标记 `has_successful_cache=false`，后续失败仍为 `failed`，不会错误升级为 `stale`。
- 兼容 M1 旧成功 entry：同 URL 且存在历史 market 文档时仍识别为成功 cache。
- cache rebuild、source CRUD、同来源锁和 revision guard 的既有语义保持不变。

### 错误与所有权处理

- 刷新错误统一去除 NUL、折叠空白、隐藏常见本地绝对路径和内部对象 repr，并限制为 2048 字符。
- 净化后的错误同时用于 source result、来源 DB 状态和 stale cache，避免通过管理 API 暴露本地路径或超长远端响应。
- revision discarded 不写当前来源状态，避免旧 URL 请求把新 revision 标成 ok 或 error。

### 实际落地

- 修改 `backend/market.py`：增加 source revision 序列化、错误净化、成功 cache 判定、source result 和顶层 refresh result 构造，并扩展 `refresh_market()`。
- 新增 `backend/tests/test_market_refresh_results.py`。
- 调整 `backend/tests/test_market_lifecycle.py` 中首次来源失败的旧异常断言，改为验证结构化 `failed` 结果及 cache 错误保留。
- 未修改 `backend/automation.py`、`backend/database.py`、`backend/main.py` 或任何前端文件。

### 测试先行证据

1. 在生产代码修改前新增真实 `refresh_market()` 契约测试，只 mock 来源列表和远端 package 加载。
2. 当前 HEAD 稳定得到 `12 failed, 2 passed`：返回值缺少 `refresh_status/source_results`，首次失败仍抛 `MarketError`，disabled 来源没有显式结果。
3. 实现后来源结果定向测试为 `12 passed, 2 subtests passed`。
4. 与既有 Market 生命周期测试组合为 `34 passed, 2 subtests passed`。

### 验证范围

测试覆盖成功、历史 cache stale、首次失败、URL/revision 变化、删除/禁用晚到结果、disabled 单源、官方/第三方相同原始 package ID、部分失败、单源隔离、并发调用结果隔离、同来源串行锁、错误净化、旧 summary 字段兼容，以及未引入 AutomationRunner 或 package 更新依赖。

### 验证结果

- 来源结果定向与 Market 生命周期组合：34 passed，2 subtests passed。
- Market、数据库、订阅刷新和 media handle 定向回归：72 passed，2 subtests passed。
- 后端全量：306 passed，56 subtests passed。
- Python 编译检查和 `git diff --check` 通过。
- 端口型 HLS 集成测试在允许绑定 localhost 的环境运行通过；沙箱内失败仍是 Uvicorn 无法绑定端口的环境限制。

### 边界

本批未创建 `backend/market_tasks.py`，未实现 Market handler、Runner 注册、Scheduler/lifespan 接入、管理 API、自动 package 更新或前端设置。`refresh_market()` 只提供 M2A-4 所需的可靠本轮输入。
