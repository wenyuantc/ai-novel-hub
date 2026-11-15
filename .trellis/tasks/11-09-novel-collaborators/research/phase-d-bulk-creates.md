# D 阶段：批量及组合创建

## 实现边界

- `POST /projects/{project_id}/careers/batch`、`/organizations/with-members`、`/relationships/batch` 和 `/characters/with-relations` 是人工确认的组合保存。路由显式传 `manual=True`，使用 `content.edit`；未携带 `manual` 的内部 service 仍执行原严格作者／管理员检查。已接受的 editor 可保存，viewer 403，未授权者 404；作者及 `projects.read` + `projects.edit` 管理员仍可用，管理员兼 viewer 取权限并集。未开放 AI 生成、任务或索引入口。
- 检查授权时禁用 autoflush。人工写按认证/User 屏障、必要时发送写门禁、项目父行锁与锁后重读成员权限的既有顺序执行。职业、组织、角色创建都会新增提及候选，因此这三种批量／组合入口获取发送写门禁；关系批量本身不增加候选，使用普通项目锁。
- 三种新增候选的入口重用卡片导入的候选竞争判断；存在新旧候选名称或别名竞争时，同一事务递增 AI 可见性 revision 与 history epoch，拒绝旧负载及旧会话历史继续使用；无竞争不递增。候选读取及版本更新都在发送门禁和项目锁内。
- 关系批量和人物组合在建立任何关系前统一校验所有既有人物属于目标作品，再执行全局人物对查重，异书存在人物与缺失人物返回同一 400。关系类型限预置或本书；职业和阶段限本书及其所属职业；组织上级、成员、章节、场景沿用逐项的同书校验。返回的人物、关系和职业采用本书已有响应投影，不添加系列身份。
- 四个入口在授权成功后的创建或提交过程中发生错误／取消时明确回滚事务；覆盖后项错误、已 flush 的前项及 `assign_new` 内嵌保存点。普通授权拒绝不先 flush 或回滚调用者待处理数据。

## 验证

- 新增 `backend/tests/test_collaboration_bulk_creates.py`：真实 HTTP 角色矩阵，service 默认权限，授权拒绝无 autoflush，跨书父组织／成员／章节／场景／人物／关系类型／职业／阶段、后项失败与内部调用者捕获异常后再次提交，嵌套保存点、异书人物对存在性、系列身份投影、真实 provider 发送门禁、候选竞争和无竞争两类版本变化。
- `cd backend && uv run pytest -q tests/test_collaboration_bulk_creates.py --maxfail=1`：32 项通过。随后扩充异书人物对测试，单独重跑该项及 4 个既有组合保存回归，5 项通过。
- 可见性版本调整之前，`tests/test_careers.py tests/test_organizations.py tests/test_relationships.py tests/test_collaboration_story_access.py tests/test_collaboration_story_dispatch.py` 共 46 项通过；随后重跑上述 4 个直接受影响的旧组合保存回归。改动文件和新测试的 Ruff 检查通过。
- `backend/tests/test_collaboration_bulk_creates_postgresql.py`：两实例项目行锁下撤权／降权的四入口竞争、角色组合嵌套保存点失败后的提交、锁后撤权及不提前 flush。2026-11-10 在 PostgreSQL 16.15 的本机 `collab_test` 隔离库错峰执行：**10 passed in 38.08s**。fixture 创建独立 schema，完成后清理。

## 跟进

- 单项的角色、职业、组织创建及其他身份修改入口也可能导致新增候选竞争；此切片不修改这些 service 之外的调用链，应另行审计并按相同发送门禁及版本合同处理。
- 上述 PostgreSQL 测试覆盖锁后撤权、降权与原子失败；各入口完整的发送门禁跨实例顺序仍需在整体接线后验证，SQLite 真实发送验证不能替代 PostgreSQL 的行锁／advisory lock 证据。
