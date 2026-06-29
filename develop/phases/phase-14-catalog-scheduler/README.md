# 阶段 14：后台定时刷新

## 目标

加全局定时刷新任务（默认**每 12h**）对已跟踪包跑增量采集，把「保持新鲜」从读路径整体挪到后台——`/versions` 因此
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
app/catalog/scheduler.py      # 独立 scheduler 入口（python -m app.catalog.scheduler）
app/core/config.py            # CATALOG_REFRESH_ENABLED / CATALOG_REFRESH_INTERVAL_HOURS=12
develop/android-package-service-deployment.md   # 调度器承载方式
```

## 实施步骤

1. **调度器**：每 `CATALOG_REFRESH_INTERVAL_HOURS`（默认 12）对 `collection_state` 里**所有已跟踪包**跑增量
   （复用阶段 11 的 `ensure_collected`，共用收集单飞）。
2. **单实例保护**：独立 scheduler 容器正常只跑一个实例；SQLite 一行 `scheduler_lock` + `BEGIN IMMEDIATE`
   作为误启多实例时的防重保护，**租约带超时**（防实例崩溃后永久占用，超时后他人可重抢）。
3. **限流与隔离**：刷新集逐包串行或小并发（避免同时多包过 Cloudflare / 触发封号）；单包失败只记日志不阻断整轮；
   整轮耗时、成功 / 失败数可观测。包多时分片错峰。
4. **承载选型**：独立 scheduler 容器，Web worker 不启动刷新调度。
5. 与读路径关系：定时任务为**主刷新源**（不受按需 TTL 门限制）；按需 TTL 降级为**兜底**（定时停摆时自救）。

## 测试

- 到点触发增量（注入时钟 / 直接调度函数），只对已跟踪包跑。
- leader 锁：多实例只有一个 runner；租约超时后可重抢。
- 单包失败不阻断整轮；`CATALOG_REFRESH_ENABLED=false` 时不跑。

## 验收标准

- 每 12h 已跟踪包被增量刷新；Web worker 数量不影响刷新频率。
- 某包刷新失败不影响整轮其余包。
- `/versions` 对已跟踪包稳定读库返回、不被刷新拖慢。

## 当前状态

- **已完成（2026-06-25），2026-06-29 修正承载方式**。依赖阶段 11（ensure_collected）。
- 承载选型（步骤 4）：**独立 scheduler 容器 + SQLite `scheduler_lock` 防重**。原 FastAPI lifespan 进程内方案在
  gunicorn 多 worker 下会因各 worker 睡眠期间租约过期而轮流提前抢跑；现已从 Web worker 生命周期移除。
  理由见[部署文档「后台定时刷新调度器」](../../android-package-service-deployment.md)；租约超时默认 900s。
- 落地：`app/catalog/scheduler.py` 的 `CatalogRefreshScheduler`——`run_once`（leader 选主 + 超时重抢、
  遍历 `last_full_at` 非空的已跟踪包、逐包串行 `ensure_collected(force=True)`、单包失败隔离、整轮 ok/failed 可观测）
  + `run_forever(stop)`（启动后先等待一个 interval，到点后刷新；`stop` 置位退出）。独立入口为 `python -m app.catalog.scheduler`。
  `catalog.ensure_collected` 加 `force`（旁路 TTL，定时为主刷新源）。`store` 加 `scheduler_lock` 表。
  装配抽到 `app/catalog/runtime.py`（`build_catalog`，路由与 scheduler 复用）。
- config：`CATALOG_REFRESH_ENABLED`（默认 true）、`CATALOG_REFRESH_INTERVAL_HOURS=12`、`CATALOG_SCHEDULER_LEASE_SECONDS=900`。
- 测试：`tests/catalog/test_scheduler.py` 覆盖只刷已跟踪、单包失败不阻断整轮、非 leader 跳过、租约超时重抢、
  force 旁路 TTL、run_forever 首个 interval 前不刷新、到点后刷新、配置关闭入口直接退出；`tests/test_api_catalog.py`
  覆盖 Web lifespan 不启动 scheduler。

## 本阶段不做

- 不做发现性扫描、不扫无关包（只刷已跟踪集）。
- 不替代「从没采过」的首次同步收集（阶段 13）。
- 不接 APKMirror（阶段 15）。
