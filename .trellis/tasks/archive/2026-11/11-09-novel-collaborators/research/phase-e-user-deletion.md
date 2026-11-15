# 账号删除与协作任务：有限前检

## 已实现

- 管理员删除用户原本只拒绝其名下作品和尚未收尾的管理员取消意图；接受了他人作品邀请的账号可能没有任何自有作品，但其正在排队、运行或待审核的任务会随用户被级联删除。本次在已有 `authorization_service.require(..., write=True, targets=(user_id,))` 取得认证 gate 和 actor/target User 写屏障后，检查目标用户尚未完成的项目或章节任务，返回匿名 `409 user_has_pending_tasks`。
- 已有自有作品（含回收站）仍优先走原 `user_has_projects` 路径。允许没有未完成执行占用的明确终态项目任务和没有项目、章节及管理员取消意图的个人任务沿旧行为级联删除；现有管理取消意图和非空 `Task.execution_key` 仍阻止删除。后者有界地包括无项目个人任务：若其有未完成执行标记，也先拒绝并保留证据；没有执行标记的 queued／终态个人任务仍沿旧行为处理。任务状态不是已知终态时失败关闭。若旧数据的 `project_id` 为 null，但 `chapter_id` 仍有值，不能把它当成普通无项目任务。
- 失败事务回滚后由现有 `AuditOperation` 按安全 `conflict` 类别记录真实操作者与目标账号；响应和摘要不包含项目名、章节名、任务名、任务 UUID 或项目明细。
- 新增的双连接 PostgreSQL 测试使用两个应用实例和随机 schema，模拟未来的协作任务受理遵循认证 gate → actor/owner User 锁 → 作品锁 → 同事务插入：任务提交先行时删除等待后返回 409；删除提交先行时旧受理等待后认证失效。该模拟没有开放协作者任务 HTTP 入口，也不是其端到端验收。

## 未闭合的执行期边界

此检查**不证明账号删除与所有后台执行已安全串行**。当前任务取消可先将 `Task.status` 提交为 `cancelled`，随后异步执行 `on_abort`；成功任务也可先提交 `succeeded`，随后执行 `_on_success`。这些执行体仍可能操作作品，但终态字段本身不能证明清理已结束，也不能分辨章节的 `generating` 是旧任务占用还是新任务占用。为避免将无关终态历史误当作正在清理，本切片没有仅凭章节状态扩大拒绝范围。

任务表现已加入持久 `execution_key`，删号前检也拒绝非空 key；但仅有此字段的存在不足以证明所有终态清理、章节／索引 claim UUID、派生任务和安全清理完成事实已经覆盖。仍须与 0045 的完整执行及收尾持久协议联动，并对取消先终态、成功先终态、进程崩溃及另一轮次抢占做真实双连接反例。新协作任务受理和派生创建必须取得同一 actor User 写屏障，不可仅凭本次预检假定已串行；旧版本写进程须在部署前排空。此项完成前不得宣称 E4 或账号删除并发已经整体安全。

## 验证

- 2026-11-10 在本机 `collab_test` 隔离数据库（PostgreSQL 16.15、随机 schema）运行下述双连接测试：**2 passed in 7.90s**。这是对模拟未来任务受理的锁顺序验证，终态清理窗口仍未闭合。

- 先构造真实 HTTP 反例：编辑者存在他人作品 queued 任务时，旧代码返回 204 并级联删除任务；缺 `project_id` 但有章节 ID 的 queued 任务也复现 204。修改后由新测试逐状态验证。
- PostgreSQL 两项双连接测试使用 `AINOVEL_COLLAB_POSTGRES_TEST_URL` 指定标准测试库；也可用 `AINOVEL_COLLAB_USER_DELETE_POSTGRES_TEST_URL` 指定本机 `collab_user_delete` 专用库。测试对库名和地址做安全校验，并为每次运行创建随机 schema：

  ```sh
  cd backend
  env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 \
    AINOVEL_COLLAB_POSTGRES_TEST_URL='<本机 collab_test 专用测试库 URL>' \
    uv run pytest -q tests/test_collaboration_user_deletion_postgresql.py
  ```

  `uv run pytest --collect-only -q tests/test_collaboration_user_deletion_postgresql.py`：2 tests collected。
- SQLite 新增角色/状态与旧任务、原账号删除、项目外取消收尾及任务统计回归：

  ```sh
  cd backend
  env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest -q \
    tests/test_collaboration_user_deletion.py \
    tests/test_admin_task_adversarial.py::test_pending_projectless_task_blocks_account_cascade_until_actual_drain_finishes \
    tests/test_task_statistics.py::test_user_delete_marks_unfinished_without_fabricating_terminal \
    tests/test_admin.py::test_delete_user_cascades
  # 19 passed in 55.50s（含新增个人任务执行标记的四种分支）
  ```

  其中 16 项为本切片新测，覆盖启用／禁用用户、三个活动状态、未知状态、缺项目的章节任务、明确终态与有／无执行标记的个人任务；3 项为原行为回归。它们不能替代实际 PostgreSQL 锁竞争或未闭合的终态清理验证。
- `uv run ruff check app/services/admin_service.py tests/test_collaboration_user_deletion.py tests/test_collaboration_user_deletion_postgresql.py`：All checks passed；`git diff --check` 通过。
