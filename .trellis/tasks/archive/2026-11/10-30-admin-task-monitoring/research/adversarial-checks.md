# 独立对抗性后端测试

## 变更边界

- 仅新增 `backend/tests/test_admin_task_adversarial.py` 与本记录；不修改产品实现、既有测试、规范、任务状态或他人改动。
- 最小缺口是正常路径测试无法证明“最后一次检查之后、真实状态写之前”的竞争、失败事务无副作用，以及旧取消清理不跨 UUID 代次。
- 测试使用公共管理/所有者 HTTP 接口、现有 TaskEngine、独立 SQLite 会话和 `asyncio.Event`；只在真实写边界或 handler 清理处设置可控屏障，不模拟最终状态写入、不调用真实模型。
- owner CAS 测试使用无作品任务以排除父作品写锁提前串行化，仅隔离具体 handler 的恢复/结束预条件；所验证的数据库条件写与统计仍为真实实现。
- SQLite 结果不代表 PostgreSQL 并发验收；前端浏览器、迁移及全量质量门禁由主会话协调，不在本测试代理范围。

## 已实测覆盖矩阵

1. owner cancel（queued/paused/孤立 running）、resume、finish 的真实 UPDATE 前插入已提交的管理员受理。
2. 原 finalize 已观察空 marker、abort 失败时，UPDATE 前插入受理，不能提交释放屏障的假终态。
3. 审计行 flush / 真实 commit 前失败：marker 回滚、无 signal/abort/终态统计，HTTP 与日志无私密异常。
4. 真实 dispatch 已扫描而未 claim 的 queued 受理；abort 失败仍占章节资源；重试完成后旧意图不回滚后来任务。
5. 旧 drain 清理被阻塞后 SQLite 真复用整数 Task ID；旧 UUID 请求与收尾不得清空新行的取消意图或修改新统计。
6. 私有 title/input/output/checkpoint/error、Hub 插件 stage 的管理投影与搜索侧漏，及自动取消审计摘要；running 的残留原始 error 不应伪报为失败，列表和详情安全摘要均为 null。
7. 无作品 pending Task 阻断用户 CASCADE 删除；真实收尾后删号恢复，累计统计/取消审计保留。
8. 四种终态 + marker 的异常组合不被单删/清理/裁剪移除；正常历史对照项仍按原规则移除。
9. 软删等待真实 `project_work`，等待超时返回 409 且释放作品删除意图，不取消独立 drain；再次等待、完成收尾后软删/彻删正常。
10. 回收站作品存在独立 drain 时不允许 purge；收尾后恢复正常 purge。异常成功终态 + marker 不盲目 abort 或 purge。

共 16 个 pytest case（含 owner 5 项与审计故障 2 项参数化）。原有正常路径测试未修改，测试未弱化断言以配合产品实现。

## 实测结果

2026-10-31，在 `backend/` 目录运行：

```bash
env UV_CACHE_DIR=/private/tmp/ainovel-adversarial-uv-01a19294 uv run --no-sync ruff check tests/test_admin_task_adversarial.py
```

结果：`All checks passed!`。

```bash
env UV_CACHE_DIR=/private/tmp/ainovel-adversarial-uv-01a19294 LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run --no-sync pytest tests/test_admin_task_adversarial.py -q
```

结果：初次 `16 passed in 13.13s`；按下述最终错误摘要契约更新后复跑为 `16 passed in 15.90s`。

```bash
env UV_CACHE_DIR=/private/tmp/ainovel-adversarial-uv-01a19294 LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run --no-sync pytest tests/test_admin_task_adversarial.py tests/test_task_statistics_identity.py tests/test_recycle_bin_tasks.py tests/test_admin_audit_logs.py -q
```

结果：`75 passed in 44.06s`。这是错误摘要契约最终收紧之前的联合关键回归；当时本文件 16 项均再次通过。

### 最终错误摘要契约复验

主会话真实浏览器发现“已成功 + 残留 error”会被误标执行失败，因此产品实现进一步收紧为只按真实 `failed/cancelled/interrupted/pending` 状态展示固定摘要，并彻底移除管理读取中的原始 `Task.error`。已核实服务实现，仅更新 running 隐私场景的摘要预期：列表和详情 `error_code/error_message` 必须均为 null。所有原始标题、错误、插件阶段、搜索、自动审计隐私断言保留；这不是放宽秘密外泄或失败状态的断言。

该调整后本文件 ruff 复验仍为 `All checks passed!`，独立测试复验为 `16 passed in 15.90s`。

```bash
env UV_CACHE_DIR=/private/tmp/ainovel-adversarial-uv-01a19294 LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run --no-sync pytest tests/test_admin_task_adversarial.py tests/test_admin_tasks.py tests/test_admin_task_cancellation.py -q
```

最终契约下管理端相关联合复验：`60 passed in 47.37s`，无失败。

在仓库根目录运行：

```bash
git diff --check -- backend/tests/test_admin_task_adversarial.py .trellis/tasks/10-30-admin-task-monitoring/research/adversarial-checks.md
```

结果：退出码 0。两个文件目前均为未跟踪的新文件；同时已通过 ruff 检查测试文件的空白与语法规范。

环境说明：初次直接 `uv run` 因沙箱无法读取 `/Users/wenyuan/.cache/uv/sdists-v6/.git` 在测试收集前失败；随后只为本次命令设置可写 uv 缓存并使用已安装依赖 `--no-sync`，没有修改依赖、锁文件或开发数据库。首次 ruff 发现新文件的未使用 import，已修正并复验通过。

## 剩余边界

- 本轮未发现需要产品修复的缺陷；无未通过的新增 case。
- PostgreSQL 真实并发、完整后端门禁、迁移和真实浏览器不由本代理验证，不能将上述 75 项通过等同整项验收。
- 真 ID 复用和终态 marker 测试有意通过独立数据库事务构造旧版本/故障组合；并未放宽生产中的 pending 删除保护。
- 未提交、未切分支、未修改其他文件或任务状态。
