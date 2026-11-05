# PostgreSQL 16 验证记录

日期：2026-11-05。仅本任务临时容器／数据库，无用户作品数据。

## 环境与真实入口

- 镜像 `postgres:16-alpine`，容器 `ainovel-planning-verify-20261105`，仅本机端口 32813。
- 用 Alembic 从空库升级至 0034；新约束在第二个空库 `planning_fresh` 重新完整升级后复验。
- 脚本 `/tmp/ainovel-planning-browser.J1ZUSm/verify_postgres.py` 调用真实 planning_service／chapter_service；两个并发 AsyncSession，未模拟事务或外键。
- 命令使用 `LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8`，避免主机默认 ASCII 导致 Alembic 读中文配置失败。

## 发现与修复

初次真实服务验证中，`DELETE FROM projects` 因 `subplot_crossing_participants.subplot_id` 默认 NO ACTION 外键检查过早而失败。交汇、支线和规划单元确实属于同一作品，排除了跨项目错误归属。

模型与迁移同步把该约束设为 `DEFERRABLE INITIALLY DEFERRED`；保留服务层对被引用支线删除的 409 保护，没有关闭外键或删除约束。其语义依据 [PostgreSQL 16 CREATE TABLE](https://www.postgresql.org/docs/16/sql-createtable.html)：可延期的外键可以在事务结束时检查。

## 复验结果

- 空库完整迁移至 0034：通过。
- 两个相同 planning_revision 的真实并发写：一个成功并推进到 revision 1，另一个 409；失败方未留下多余 unit。
- 只有交汇、没有普通节点：两条支线格子均显示推进和同一事件。
- 删除被交汇引用的支线：业务 409。
- 通过 chapter_service 删除源章：单元进入待关联，交汇与两个参与者保留。
- 整作品删除：所有规划／支线／交汇及参与关系完成级联，事务提交通过。

本记录不替代全量后端测试、浏览器验收或后续代码复核。

## 快照与序列补充验证

同一隔离PostgreSQL 16的`planning_fresh`库，使用真实snapshot/binding服务：原计划删除后恢复相同unit绑定；保留快照之后改写的node正文；预先推进outlines/chapter_plans序列20次，恢复不回退且随后自动ID更大；显式改挂后再脱离，旧快照保持detached并提示位置变化。全部断言通过。临时执行脚本：`/tmp/ainovel-planning-browser.J1ZUSm/verify_pg_snapshots.py`。
