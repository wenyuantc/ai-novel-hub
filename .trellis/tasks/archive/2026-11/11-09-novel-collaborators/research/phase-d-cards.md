# 阶段 D：角色与组织卡片协作

## 2026-11-10 隔离 PostgreSQL 补测

在本机独立 `collab_cards` 测试库（PostgreSQL 16.15、随机 schema）执行 `AINOVEL_COLLAB_CARDS_POSTGRES_TEST_URL=<专用测试库 URL> uv run pytest -q tests/test_collaboration_cards_postgresql.py`：**8 passed in 27.49s**，包括后来补充的真实模型发送门禁用例。

当前代码的 SQLite 组合 `uv run pytest -q tests/test_collaboration_cards_access.py tests/test_cards.py --tb=short`：**55 passed in 45.08s**，覆盖新增的卡片权限和原卡片功能；不能替代最终全后端回归。

## 能力与事务

- 卡片导出和不落库的导入预览使用作品 `export` 能力：作者及 accepted 作品协作者可用，纯管理员仍按原作者专属边界返回 404。待接受、拒绝、撤销与非成员均不能访问。
- 导入先以同一事务取得认证/User 屏障、AI 发送写门禁和作品父行锁并在锁后重新检查 `export` 与 `content.edit`；查看者返回 403，编辑者可写。管理员兼查看者仅在自身具有原有人工编辑能力时可导入，纯管理员不能以管理身份扩充卡片入口。v1–v6 均取得发送写门禁：旧格式也可能新建带 legacy 常驻或提及策略的角色。
- 导入保留字段、分类和 AI 设置原有预览版本检查、显式映射、单事务提交及系列绑定保护。失败时主动整体回滚，防止内部调用者捕获异常后提交已 flush 的部分角色或时间线；涉及网络的模型调用和后台任务均未引入。
- 仅有单书权限时，同名系列角色导致卡片预览或导入跳过的警告改为不带系列身份的中性说明；作品作者、具有原有管理员读取权限的协作者或获相应系列权限的人仍可见原说明。系列名称和编号从未进入警告。
- 卡片导出批量验证角色关联职业所属作品、阶段所属职业、关系载体所属作品及关系类型为本书或全局预置。异常关联以匿名 `409 invalid_card_association` 拒绝；普通跨作品 ID 与映射依旧被拒绝。
- 旧 v1–v5 导入新增可追踪名称或别名可能遮蔽已有唯一命中，原实现没有推进可见性代次；v6 原先只推进 revision 而不封存历史。现在对导入前后的匹配候选做保守比较：同词、包含或部分重叠可能发生竞争时推进 revision 与 epoch，令旧负载及会话失效；无竞争的旧卡片保持原代次，无竞争的 v6 保持原 revision 递增但不封存历史。比较超过安全工作量时保守封存。

## 验证证据

在 `backend/` 中执行；PostgreSQL 是本机隔离库，测试为每个用例创建随机 schema。并行测试时专用环境变量优先，避免不同 schema 的同项目编号共享 advisory lock；最终串行测试可使用标准环境变量。

```sh
env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 \
  AINOVEL_COLLAB_CARDS_POSTGRES_TEST_URL='<隔离 collab_cards URL>' \
  uv run pytest -q tests/test_collaboration_cards_postgresql.py
# 6 passed in 154.60s：v1/v6 撤权先提交、降级先提交、导入先提交；本轮启动后新增的 2 项真实模型发送 lease 测试不在这 6 项中。

env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest -q tests/test_collaboration_cards_access.py
# 12 passed in 41.27s；启动后新增的 pending/declined/revoked 断言、拒绝前未提交对象回滚测试与部分词面重叠测试尚未执行。

env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest -q \
  tests/test_cards.py tests/test_setting_ai_visibility_formats.py tests/test_series_projection_guards.py
# 59 passed in 149.26s；该组合开始于最终候选竞争/原子回滚修改和关系服务最新归属前检之前，须以最新代码重跑。

uv run ruff check app/services/card_service.py tests/test_collaboration_cards_access.py \
  tests/test_collaboration_cards_postgresql.py
# All checks passed；其后新增了一项部分词面重叠测试，最终须再跑。
```

首次新角色矩阵与跨作品关系类型泄漏、v1/v6 候选竞争、已 flush 后内部捕获异常的原子性均先有失败复现；修复后的 SQLite 测试中前两组及直接失败回滚已分别通过。未运行完整后端或前端门禁。

## 后续

- 按主协调安排错峰重跑完整 `tests/test_collaboration_cards_access.py`、`tests/test_collaboration_cards_postgresql.py` 和原卡片/可见性/系列保护组合；不要把尚未运行的发送 lease 测试或最近追加断言计为通过。
- `CharactersPanel.tsx` 原先把卡片两个弹窗绑定 `canUseAI`；前端需按导出/只读预览和人工编辑分别控制，并保护撤权与旧响应。
- 普通角色新增等其他入口也可能产生候选竞争；该切片不修改这些服务，已提醒主协调独立复核。
- 本切片不代表 D/E/F 已完成，不得单独部署。
