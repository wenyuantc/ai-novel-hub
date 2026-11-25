# 写作统计与码字目标实施计划

**交付目标**　在作品连载工作台同时看到本人的手写进度、AI 落稿趋势和现有存稿天数。以 [需求及验收](prd.md) 和 [技术设计](design.md) 为准。

按下面四步实施。前两步建立可信的写作记录，第三步计算目标与趋势，第四步连接工作台。本项的所有交付共用一套计量口径，保留一个 Trellis 任务。下列测试文件中标为「新增」的文件在实施阶段创建。

编写迁移时查阅[数据库规范](../../../../spec/backend/database-guidelines.md)，编写查询客户端时查阅[前端类型约定](../../../../spec/frontend/type-safety.md)，双库验收使用[后端质量规范](../../../../spec/backend/quality-guidelines.md)列明的隔离测试库和运行方式。

## 1. 建立计量基础

- [x] 在新增的 `backend/tests/test_writing_stats_count.py` 编写字符差异用例，先确定旧版没有此能力，再实现 `backend/app/services/writing_stats.py` 的计数纯函数。至少覆盖下列输入，以及长重复文本中相隔很远的两个替换。

  | 保存前 | 保存后 | 应增加 |
  | --- | --- | ---: |
  | 空稿 | 山 风。 | 3 |
  | 春风吹过山岗 | 暖风吹过山坡 | 2 |
  | 100 个甲 | 100 个乙 | 100 |
  | 春风🙂 | 春风 | 0 |
  | 海边 | 在「海」「边」之间插入空格和换行 | 0 |

- [x] 在 `backend/app/models/writing_stats.py` 新建每日统计、个人目标、统计起点三张表，在 `backend/app/models/__init__.py` 注册。执行 `uv run alembic heads`，按实施分支实际 head 编写迁移，确保新安装及升级都能读取同一启用起点。
- [x] 在 `backend/app/services/project_transfer/registry.py` 登记个人数据。新增 `backend/tests/test_writing_stats_migration.py`，验证升级旧库后已有 8 万字的旧书没有当日写作记录，单书导入从零开始，空表降级成功，有个人记录时降级受保护。
- [x] 在 `backend/` 执行 `uv run pytest -q tests/test_writing_stats_count.py tests/test_writing_stats_migration.py tests/test_project_transfer.py -k 'writing_stats or registry'`。进入下一步前核对非空白语义、两库表约束以及长段落差异的耗时。

## 2. 接入手写保存与 AI 落稿

- [x] 新增 `backend/tests/test_writing_stats_writes.py`，先用真实保存入口写出 A1–A3 的失败用例。接入设计表列出的章、场景、富文档保存；新建带正文的章或场景、自动保存各验一次。输入法组合和本地恢复沿用现有编辑器与保存契约。
- [x] 接入场景／整章生成、批量生成、核对后应用、选区改写及智能体直接改稿。用假模型验证一批多个场景只有改变的部分入账，采纳 2,000 个新字只增加 AI 2,000。用采纳服务的真实写入请求覆盖「日志输出多于实际落稿」；保留现有预览交互。
- [x] 按设计在原事务中原子累加。新增 `backend/tests/test_writing_stats_concurrency.py`，用独立连接覆盖两章同人同日同时写入、同一版本竞争、事务回滚、AI 完成收据重放。核验的是最终库内总数。编辑器请求冲突后拿到 409，失败一方新增为 0。
- [x] 走一遍排除入口：纯格式、作者区块、区块转正文、拆并／移动、批量替换／撤销、版本恢复、预览放弃。验证章节仍按原逻辑保存或恢复，每日总数保持原值。删除源章后当天写作量仍可读。
- [x] 在 `backend/` 执行 `uv run pytest -q tests/test_writing_stats_writes.py tests/test_writing_stats_concurrency.py tests/test_chapters.py`。另外在已配置独立本机测试库 `AINOVEL_POSTGRES_TEST_URL` 的环境中，串行执行 `uv run pytest -n 0 -q tests/test_writing_stats_concurrency.py tests/test_writing_stats_migration.py`，分别记录 SQLite 和 PostgreSQL 结果。

## 3. 提供本人在当前作品的查询和目标接口

- [x] 新增 `backend/app/schemas/writing_stats.py`、`backend/app/api/v1/writing_stats.py`，在 `backend/app/api/v1/__init__.py` 注册路由。服务层统一计算目标、连续天数和趋势。新增 `backend/tests/test_writing_stats_api.py`，先以作者、合作者、只读者及已撤权账号验证权限，再验证响应内容。
- [x] 固定服务器时间，覆盖北京时间午夜、周日到周一、今天未写但昨天写过、连续串中间只有 AI、趋势区间比连续串短、启用前空值。验证修改或清除目标、同一初始版本同时设置、旧标签页覆盖关闭后的目标，后两类冲突应有明确结果。
- [x] 在 `backend/` 执行 `uv run pytest -q tests/test_writing_stats_api.py tests/test_serialization_read.py tests/test_serialization_write.py`。按 A4–A6 对照：切换作品或账号后各自的数据独立，调整码字目标不改变连载日更章数。

## 4. 接入连载工作台并验收

- [x] 新增 `frontend/src/api/writingStats.ts`、`frontend/src/components/serialization/WritingStatsPanel.tsx`，接入 `frontend/src/pages/project/SerializationPage.tsx`。区分「设码字目标」与「设置日更目标」，并列展示可支撑天数、手写／AI 字数、当期进度、连续天数。日期范围只控制趋势。
- [x] 新增 `frontend/src/components/serialization/WritingStatsPanel.test.ts`，覆盖加载、首次无记录、两个趋势序列、未设目标、超过目标、当前周期与筛选日期独立、保存冲突、失败重试、只读以及切书后的迟到响应。在 `frontend/` 执行 `npm test -- WritingStatsPanel serializationWorkbench`。
- [x] 用隔离账号和假模型在真实浏览器走 A1、A2、A5、A6。另在窄屏选择日期，核对图例、每日数值与存稿天数都可读。连载目标未设时，仍能设置和查看本人的码字目标。
- [x] 从正常配置的测试环境运行两层门禁。后端在 `backend/` 运行 `uv run ruff check .` 和 `uv run pytest -n 4 --dist loadfile --durations=20`（SQLite 默认环境）。前端在 `frontend/` 运行 `npm run lint`、`npm test` 和 `npm run build`。将验收证据写入任务目录，更新 README 和规范，再按实际结果更新 TODOS 第 26 项。

## 发布与回退

实施时重新核对 Alembic head，再升级数据库并部署新服务。聚焦观察保存延迟、重复或漏计、统计异常对原保存事务的影响。若需回退，先停用新写入版本、保留新增表及其数据；空库降级与非空库保护在迁移测试中演练。回退期间的写作量无可靠来源，恢复计量时应标明该时间段的数据缺口。
