# 类型化字段 S1–S2 核心复核

日期：2026-11-05。Active task：`.trellis/tasks/11-05-typed-setting-fields`。

本记录仅为阶段 2.2 的 S1–S2 局部完整复核，不代表字段子任务最终验收通过。未提交 Git，未修改任务状态，未接触 8000 用户服务。

## 范围和结论

已读取原生 hook 保存的完整上下文、PRD／design／implement、check manifest、设定时间线和相关数据库／质量规范。逐项审阅：

- `models/setting_fields.py`、`models/progressions.py`、`models/entities.py` 的 `fields_revision`、模型注册。
- `0035_typed_setting_fields.py`，及 `test_migrations.py` 中 0031 的历史版本适配。
- `setting_field_values.py`、`setting_progression_resolver.py`、`schemas/setting_field_value.py`。
- 三个核心测试文件；`schemas/progression.py` 按约定只读。

没有发现需要修改本范围产品代码的缺陷。发现并补齐了迁移约束一致性及若干兼容边界的回归缺口。

## 已核实的契约

- 旧字段物理 ID、逐字基础文本、时间戳、事件 FK／active 状态／孤儿记录及规划关系引用保留；SQLite FK 开／关两种升级路径和安全降级均覆盖，`foreign_key_check` 为空。
- Text→JSON 保持 JSON 字符串；原文 `null` 和空串不变。共享绑定的本地 label 为 SQL NULL、legacy label 非空，唯一约束和形状 CHECK 保持；基础值和事件的空选择为 JSON null，SQL NULL 被 NOT NULL 拒绝。
- `organization` 和 `career_entry` 的主体形状／部分唯一索引与原 `membership`／`career` 分离；存量组织／职业仅新增空基线，不改成员、授职或关系事实。
- 四类稳定句柄有真实目标 FK NO ACTION、项目 FK CASCADE 和唯一索引；SQLite 句柄 ID 不复用。真实 PG 覆盖四类目标单删拒绝、定义单删拒绝和整作品级联，另一作品保留。
- `FieldSpec`／codec 拒绝错误类型、额外键、跨字段选项及伪整数 ID；只允许 text append，line 限 500 Unicode 码点且拒绝行分隔符。EntityRef 仅在公共边界编码为 handle，canonical handle 对象不能再次作为公共引用编码。
- 纯 resolver 深复制基础值和事件对象；四类字段在第 5／20／21 章与章初末、计划章、显式清空、人工优先、inactive／orphan 规则下正确回放；旧字符串事件和 `custom_fields: dict[int,str]` 投影保留。
- 旧五参数 `EffectiveSnapshot` 构造继续可用，新 `field_values` 默认容器不会跨实例共享。
- 降级预检拒绝所有不可表示的新配置／值／主体资料及非字符串 custom 事件，含 inactive、orphan 和 inactive+orphan JSON null；失败前不变更版本或删除历史。
- 0031 保留旧 Text、非空 label、原外键／索引及原约束行为；当前模型新增列／索引不会被错误要求出现在旧版本。0035 另在 SQLite 和真实 PG 比较升级与 `create_all` 的完整结构。

## 本轮修复

1. `backend/tests/test_setting_fields_migration.py`：原断言只核对五张表的列名／索引名。新增 SQLite 与 PG 的升级／`create_all` 结构比对，覆盖 projects 和全部六张字段／时间线表的列类型、nullable、默认值、主键、FK 目标／选项、唯一约束、CHECK 内容、索引列／唯一性／条件。对比在同方言内进行，只正规化 SQL 空白，不跳过真实约束。
2. 同文件：补 orphan 对象值、inactive+orphan JSON null 的降级拒绝；补 SQLite 事件 JSON null 和基础值／事件 SQL NULL 拒绝断言。
3. `backend/tests/test_setting_field_values.py`：补 U+001C／U+001D／U+001E 单行分隔符回归。
4. `backend/tests/test_setting_progression_resolver.py`：补旧构造签名与 `field_values` 默认容器隔离回归。

未修改模型、迁移或 codec／resolver 的产品实现。

## 实际验证

最终命令在 `backend/` 运行：

```bash
LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 \
AINOVEL_FIELDS_POSTGRES_TEST_URL='<本任务 fields_verify 专用连接串>' \
UV_CACHE_DIR=/tmp/ainovel-fields-uv-cache uv run --no-sync pytest \
  tests/test_setting_field_values.py \
  tests/test_setting_progression_resolver.py \
  tests/test_setting_fields_migration.py \
  tests/test_migrations.py::test_0031_setting_progressions_preserves_legacy_and_detaches_deleted_sources \
  -q --tb=short
```

结果：**117 passed in 15.13s**，无 skip。PG16 使用本任务 `127.0.0.1:32814/fields_verify`，随机测试 schema 和临时模型 schema 均清理；本地网络经安全复核后获准运行。

- Lint：授权范围的模型、schema、service 和测试文件 `uv run --no-sync ruff check ...` 通过。Alembic 文件按项目配置不在 Ruff 门禁内，已静态审阅并执行真实迁移。
- TypeCheck：后端未配置独立 mypy／pyright 命令，不冒报类型检查通过；授权范围 Python 文件的 `python -m compileall -q ...` 通过，测试导入与 Pydantic 验证通过。
- 空白检查：已跟踪核心文件 `git diff --check` 通过；六个未跟踪核心文件逐一 `git diff --no-index --check /dev/null <file>` 无空白错误。

初次未带 UTF-8 locale 的迁移测试因 Alembic 读取中文 ini 失败；按任务规定的 LC_ALL／LANG 重跑即消除，无产品代码改动。首轮全仓 Ruff 看到并行切片的进行中错误，已通知主会话，按主会话指示不越界修复或重复跑全仓门禁。

## 未修问题与交接

本范围无未修产品缺陷，无需要产品决策的设计问题。

- API／配置服务／旧事件公共投影／写入口事务、删除与向导保护、上下文／transfer／card／agent／effects／前端仍由各切片负责人完成；本检查不宣称这些路径已通过。
- 最终字段 spec 与前端索引需由主会话在接口冻结后统一同步，尤其补充新主体、FieldSpec／codec、目录版本和备份版本合同。本轮未越过文件 ownership 修改 spec。
- 完整后端／前端门禁、CAS 并发与真实浏览器验收由主会话统一收敛；不得凭本报告启动后续分类或勾选父任务验收。
