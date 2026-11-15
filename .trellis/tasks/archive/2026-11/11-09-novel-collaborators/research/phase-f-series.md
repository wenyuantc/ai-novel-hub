# 阶段 F：系列协作实现与验证记录

## 接入范围

- `SeriesOut` 的真实 HTTP 列表、详情和系列写响应新增必填 `is_owner`、`can_manage_collaborators`，按实际 actor 计算。`GET /series` 返回作者自有及已接受邀请的系列；待接受或已撤销邀请、仅有管理员权限均不授予系列权限。系列改名、删除、书籍加入与退出、成员管理保持系列作者专属。
- 系列 editor 可读取、新建及编辑共享档案。`SeriesEntryOut.has_uses` 包含隐藏和回收站成员书；`uses`、详情 `books` 只展示调用者当前可读且活跃的作品。系列作者仍可看到所有书籍和引用。共享条目分页按结果批量计算绑定和可见作品，不逐条查询；系列条目的数量不改变本次分页 SELECT 次数。
- 编辑共享基础与 AI 可见策略使用内部投影模式，沿原有系列 → 按项目 ID 排序的发送写门禁 → 同序项目父锁，保留各书本地演变及作者原有依赖预检；隐藏书的字段约束及删除中状态等冲突只返回 `series_projection_conflict`，无书名、书内 ID、字段名或依赖详情。
- 编辑者删除已采用的条目返回 `series_entry_bound`，包含隐藏书与回收站书；无绑定才可删除。提升、采用、取消采用、移动及影响预览额外检查所有受影响书的作者或 accepted 作品 editor 身份；管理员人工权限不能替代该身份。无权/回收书的归属调整返回匿名 `series_book_edit_required`。项目授权在父行锁后再次读取。
- 系列写入先按数据库真实 owner 身份获取认证 gate/User 屏障，再锁系列行并重读系列协作者；SQLite 为认证锁和系列锁统一应用限时，归还连接前恢复原 `busy_timeout`。系列只读及影响预览在 `no_autoflush` 下执行；刷新 Project、Series、SeriesEntry 时保留合法普通待写字段，身份、归属或 revision 有待写变化时拒绝且保留原暂存值。成员书与绑定的身份字段也在重读前保护；仅系列作者可暂存系列名称。
- `tests/test_series.py` 的作者完整响应断言更新以包含新身份字段。阶段 D 的 `test_single_book_collaborator_does_not_see_series_identity` 中，原来标注“阶段 F 接入前”的系列 404 断言改为系列 200；同时断言被撤销书籍权限后隐藏 `books`、`uses`、仍显示 `has_uses`，并维持单书 404。

## 验证记录与边界

本机隔离 PostgreSQL 版本为 **16.15**。连接使用专用的 `collab_test` / `series_verify` 本地库，每个测试创建随机 schema。`ai_dispatch` advisory lock 使用项目 ID 且不包含 schema；不同测试如共用同一数据库可能因同号项目互相争锁，最终阶段应按数据库隔离并错峰执行。

已执行：

```sh
# 后续事务边界和少量安全错误处理修改之前，SQLite 系列与原投影、备份回归：
uv run pytest -q tests/test_collaboration_series.py tests/test_series.py \
  tests/test_series_projection_guards.py tests/test_series_transfer.py
# 21 passed in 35.23s

# 加入只读 no_autoflush、跨书角色和作者身份字段后，聚焦系列及阶段 D 交叉断言：
uv run pytest -q tests/test_collaboration_series.py \
  tests/test_collaboration_story_access.py::test_single_book_collaborator_does_not_see_series_identity
# 9 passed in 30.95s；执行后继续补强了项目身份、隐藏书删除中的匿名错误分支。

# 实际 PostgreSQL，执行时其他测试最初也使用了 collab_test；随后已分库。
AINOVEL_COLLAB_POSTGRES_TEST_URL=... AINOVEL_SERIES_POSTGRES_TEST_URL=... \
  uv run pytest -q tests/test_collaboration_series_postgresql.py tests/test_series_postgresql.py
# 8 passed in 253.22s；其中 7 项系列协作（包括两个参数化撤权先后、
# 真实 OpenAI 兼容 HTTP 发送门禁、采用与删除两种先后顺序），1 项原系列锁/级联回归。
# 此进程在最后的只读/身份与匿名错误补强之前加载代码；不得视为最终版本验证。

uv run ruff check app/services/series_service.py app/schemas/series.py \
  tests/test_collaboration_series.py tests/test_collaboration_series_postgresql.py \
  tests/test_series.py tests/test_collaboration_story_access.py
# All checks passed!（在最后代码修改后执行）

# identity-map 定向测试：Series/SeriesEntry 普通字段保留、危险身份拒绝，
# SeriesBook/SeriesEntryUse 身份与绑定拒绝，且均不隐式 flush。
uv run pytest -q \
  tests/test_collaboration_series.py::test_series_reads_preserve_pending_values_and_reject_pending_identity \
  tests/test_collaboration_series.py::test_series_reads_reject_pending_membership_and_binding_identity
# 2 passed in 1.61s

# 最后 identity-map 与隐藏书删除中安全错误修复后的 HTTP 小回归：
uv run pytest -q \
  tests/test_collaboration_series.py::test_series_editor_reads_only_authorized_books_and_can_edit_shared_baseline \
  tests/test_collaboration_series.py::test_hidden_book_propagation_conflict_does_not_disclose_dependency \
  tests/test_series.py::test_promote_keeps_original_id_and_basis_then_reprojects_each_book
# 3 passed in 8.03s
```

一次较宽组合在阶段 D 临时断言仍假定系列协作者 404 时停止：`24 passed, 1 failed`；该断言已按新契约修正，随后单独交叉测试通过，**整个组合尚未以最终代码重跑**。

## 后续核验

主代理在系统负载恢复后统一错峰重跑上述系列和阶段 D 交叉回归，以及完整 PostgreSQL 协作/系列组合；独立审查锁序、可见性投影、身份字段与有无其他遗漏后再决定阶段 F 门禁。系列前端已收到字段契约，最终整体部署仍受 E、G、H 全部完成和验证约束。
