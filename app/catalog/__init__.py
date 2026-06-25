"""版本目录（Version Catalog）持久层。

阶段 10 打底：SQLite 单库（store）+ 名↔号账本（ledger）+ 产物 manifest 解析（manifest）。
对外接口、源采集器、编排器、定时刷新分别在阶段 11–14 接入，本包只提供持久层与回填能力。

设计见 develop/android-package-service-version-catalog-design.md 的 v2 §C/§E。
"""
