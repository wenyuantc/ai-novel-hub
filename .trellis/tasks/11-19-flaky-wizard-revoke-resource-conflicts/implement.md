# 执行计划

所有 pytest 在 `backend/` 下运行，前缀 `env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8`。按精简回归策略：中途只跑受影响文件，完整回归只在改了产品代码时于收口跑一次。

## 步骤

1. A 的依据确认：在 `.trellis/spec/backend/scenes.md`、`setting-ai-visibility.md` 与既有用例中找到「设定变化使在途生成失效」的依据，写入 `research/root-cause.md`。找不到依据则停下回到规划。
2. A 修复：按 design.md 调整 `backend/tests/test_tasks.py::test_resource_conflicts` 的等待与放行顺序。
3. A 验证：单独重复 200 次；跑 `tests/test_tasks.py` 整文件。
4. B 复现尝试（限时）：同机 8 路并发重复该用例并叠加另一组后端测试作负载，至多两轮；记录结果。
5. B 窗口审查：阅读 engine 的撤权复查、取消与收据收尾代码，把 W1–W5 与 H1–H3 逐条写入 `research/root-cause.md`，标明已有用例覆盖情况。
6. B 确定性验证：为未覆盖的窗口补用例。若某窗口证明产品有竞争，停下回到规划，向用户说明后再改产品代码。
7. B 诊断输出：为该用例补失败诊断，不改断言（H3 成立时除外，按 design.md 处理）。
8. B 验证：并发负载下重复 200 次；跑 `tests/test_collaboration_wizard_entry_e4g3c.py` 整文件。
9. 收口：`uv run ruff check .`；若改了产品代码，跑后端完整回归。
10. 更新 TODOS.md 第 20 项，如实写明 B 是否复现及结论上限。

## 验证命令

```bash
for i in $(seq 1 200); do uv run pytest "tests/test_tasks.py::test_resource_conflicts" -q -x -p no:cacheprovider || break; done
uv run pytest tests/test_tasks.py tests/test_collaboration_wizard_entry_e4g3c.py -q
uv run ruff check .
```

## 风险点与停止条件

- 任何需要改 `backend/app/` 的结论：停下，回到规划。
- 不放宽超时、不加重试、不跳过用例。H1 成立时如实记录，处理方式回到规划讨论。
- 重复运行耗时长，放后台执行。
- 本仓库常有并发会话：不使用 stash／reset／checkout 清理工作区。
