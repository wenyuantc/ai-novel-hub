# 实施与验收

作者已确认实施方案。第 24 项统一验收，按两个阶段实施。AI 建议使用跨章移动阶段确定的章节身份和版本规则，并验证移动后已生成建议的过期行为。

## A. 跨章移动

- [x] 冻结同书移动预览／确认接口、章节首次身份、历史归属和删除语义。把原章与目标章的继承差异纳入预览。
- [x] 实现场景归属记录、数据库迁移、备份格式升级和双向重映射。同步章历史读取、批次历史的章景校验和章删除图登记。
- [x] 抽取可在已有作品写屏障内按稳定顺序锁章的共用逻辑。实现双章快照预览、固定插入点、合法接缝、双章投影、时间线重验、配对历史和变更审计，在一次提交内完成。
- [x] 复用稳定章身份计算，核对连载定稿、全书搜索范围、草稿版本及受影响的旧写入口。验收任何持有早期场景版本的请求在移动后都须重新核对。
- [x] 接入章节编辑器场景菜单，选择目标和位置、查看预览、明确确认。处理保存失败、冲突、权限变化、网络结果未知和跳转。用现有章节目录／排序入口激活页面刷新。

## B. AI 拆并建议

- [x] 定义任务种类、严格的输入／输出、作者确认接口和任务结果生命周期。接入原任务的配额、模型、可见性与撤权门禁。
- [x] 提取或复用手动拆并预检，计算安全候选和可发送的正文范围。FakeLLM 证明篡改候选、禁用来源、超限和未确认任务对现稿零写入。
- [x] 复用现有结构写入事务和合并预览。补建议绑定、冲突映射、执行回执、单次采纳和任务记录重开。
- [x] 加入拆分、合并建议按钮及结果预览，验收保存、取消、无建议、冲突、任务终态、重复确认和移动后旧建议失效。继续覆盖手动拆并。

## 跨阶段验收

| 范围 | 梳理的落点 | 验收重点 |
| --- | --- | --- |
| A 场景／结构与设置 | `test_scenes.py`、`test_scene_versions.py`、`test_scene_progressions.py`、`test_scene_planning.py`、新增聚焦跨章用例 | 移唯一场景、首尾分隔、符号与 CRLF、同书权限、同事务回滚、矩阵和场景事件重算。 |
| A 历史／备份与旧消费者 | `test_scene_transfer.py`、`test_batch_revision_transfer.py`、`test_batch_revision.py`、`test_serialization_{write,concurrency,chapter_lifecycle,transfer}.py`、`test_mentions.py` | 原章读历史、旧恢复／撤销冲突、定稿留存、导入后再次移动、旧章数字 ID 重用拦截、删章时无受损历史或跨书引用。 |
| B AI 与富文档 | `test_scene_dispatch.py`、`test_scene_review_regressions.py`、`test_scene_generation.py`，新增聚焦建议用例 | 使用 FakeLLM 核对真实出站范围，检查取消、模型坏结果、个体权限和模型绑定、确认前稿件零写入，逐字符验收采纳。 |
| 双方交互 | 对应的场景 API 与页面组件测试，隔离作品下 Chrome 桌面与 375px | 用正常编辑入口完成操作，演练双页竞争、撤权、仅剩一个场景、任务重开和连接中断。 |

## 验证命令

在 backend 目录执行定向用例，再执行完整门禁。PostgreSQL 使用 [项目规定的隔离测试库](../../spec/backend/quality-guidelines.md#postgresql-隔离测试库与环境变量)，串行复跑迁移、并发、备份与撤权。所有命令使用可解析中文 Alembic 配置的 UTF-8 locale。

```bash
uv run alembic heads
uv run ruff check .
uv run pytest -n 4 --dist loadfile --durations=20
```

在 frontend 目录执行现有全量门禁。

```bash
npm run lint
npm run test
npm run build
```

记录实际测试范围、隔离数据库类型、测试用户角色和模型类型。完整验收后按现有规范同步 README、技术规格和 TODO #24 的完成状态。
