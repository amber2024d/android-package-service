# 阶段 14：后台定时刷新

## 目标

加全局定时刷新任务（默认**每 5h**）对已跟踪包跑增量采集，把「保持新鲜」从读路径整体挪到后台——`/versions` 因此
基本只读库。多 worker 下用 leader 选主保证只有一个 runner。

## 输入文档

- [版本目录设计 v2 §G（后台定时刷新）/ §D（增量）](../../android-package-service-version-catalog-design.md)
- [Docker Compose 部署设计](../../android-package-service-deployment.md)

## 交付范围

新增：

```text
app/catalog/scheduler.py
tests/catalog/test_scheduler.py
```

改动：

```text
app/main.py                   # 启动调度器（或独立 worker 入口）
app/core/config.py            # CATALOG_REFRESH_ENABLED / CATALOG_REFRESH_INTERVAL_HOURS=5
develop/android-package-service-deployment.md   # 调度器承载方式
```

## 实施步骤

1. **调度器**：每 `CATALOG_REFRESH_INTERVAL_HOURS`（默认 5）对 `collection_state` 里**所有已跟踪包**跑增量
   （复用阶段 11 的 `ensure_collected`，共用收集单飞）。
2. **leader 选主**：SQLite 一行 `scheduler_lock` + `BEGIN IMMEDIATE`，多 worker 只一个 runner；**租约带超时**
   （防 worker 崩溃后永久占用，超时后他人可重抢）。
3. **限流与隔离**：刷新集逐包串行或小并发（避免同时多包过 Cloudflare / 触发封号）；单包失败只记日志不阻断整轮；
   整轮耗时、成功 / 失败数可观测。包多时分片错峰。
4. **承载选型（落地前定）**：FastAPI 进程内调度器（带 leader 锁）vs 独立 scheduler 容器——选定并写入部署文档。
5. 与读路径关系：定时任务为**主刷新源**（不受按需 TTL 门限制）；按需 TTL 降级为**兜底**（定时停摆时自救）。

## 测试

- 到点触发增量（注入时钟 / 直接调度函数），只对已跟踪包跑。
- leader 锁：多实例只有一个 runner；租约超时后可重抢。
- 单包失败不阻断整轮；`CATALOG_REFRESH_ENABLED=false` 时不跑。

## 验收标准

- 每 5h 已跟踪包被增量刷新；多 worker 不双跑同一包。
- 某包刷新失败不影响整轮其余包。
- `/versions` 对已跟踪包稳定读库返回、不被刷新拖慢。

## 当前状态

- 未开始。依赖阶段 11（ensure_collected）。承载方式（进程内 vs 独立容器）与租约超时取值落地前定。

## 本阶段不做

- 不做发现性扫描、不扫无关包（只刷已跟踪集）。
- 不替代「从没采过」的首次同步收集（阶段 13）。
- 不接 APKMirror（阶段 15）。
