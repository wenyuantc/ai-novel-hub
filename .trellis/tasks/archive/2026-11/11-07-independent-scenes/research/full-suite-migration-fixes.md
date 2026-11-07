# 完整门禁：0035／0036 历史迁移测试修复

## 变更边界（写入前）

完整后端首轮有5个本组失败：0036 SQLite保留图2例、0036 PostgreSQL往返1例、0035 schema比较与PostgreSQL各1例。最小缺口在测试：使用当前ORM整表选择历史0035/0036物理表，引入不存在的0039 scene_id；用当前模型直接作为历史结构期待，也混入0039 FK、索引与扩展锚点CHECK。不是旧迁移需要执行新结构。

本轮只改 `backend/tests/test_setting_categories_migration.py` 与 `test_setting_fields_migration.py`，以及本记录。不改产品、模型、0039/旧迁移、其他测试、README/spec或任务状态。旧修订读写改用既有 `_historical_table` 反射；schema期待明确投影0039增量，并以冻结旧锚点DDL在同方言期望库生成CHECK文本，避免靠字符串删改PostgreSQL反射的强制类型转换或照抄实际库断言。所有旧原值、有效位置、FK、序列、CHECK、降级与PG提交断言保留；真实迁移目标仍0034/0035/0036。

## 实际修改

1. 分类迁移的事件种子、字段有效值读取和整事件读取均使用当时物理表反射，不再选择当前ORM新增scene_id。
2. 字段迁移中的事件JSON null写入也使用历史表，保留JSON null而非SQL NULL的原断言。
3. 历史schema期待只移除0039新增的scene_id/FK/index。旧anchor_shape不删除、不忽略：在独立期望库创建仅含旧锚点三列与冻结CHECK的测试表，读取同数据库方言标准化DDL，再与真实0035的旧约束逐项相等比较。PostgreSQL与SQLite都保留全部其他列、默认值、主键、外键、唯一约束、CHECK和索引比较；PG期望建库继续调用create_isolated_schema，不污染共享ORM metadata。

## 验证结果

工作目录backend；所有pytest显式使用 `env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8`，通过私有connection.json只在子进程环境注入 `AINOVEL_FIELDS_POSTGRES_TEST_URL`，不输出URL或凭据；每个既有PG用例继续使用随机schema并finally清理。

- 原5失败focused：选择 `test_0036_preserves_every_0035_value_anchor_and_revision_then_empty_downgrade`（False/True）、`test_0036_postgresql_0035_roundtrip_and_no_action_rules_project_cascade`、`test_0035_schema_matches_version_contract`、`test_0035_postgresql_preserves_text_json_null_and_no_action_project_cascade`，命令尾 `-q --tb=short` → **5 passed in 11.77s**。
- `uv run pytest tests/test_setting_categories_migration.py tests/test_setting_fields_migration.py -q --tb=short` → **34 passed in 31.05s**，无跳过，含真实SQLite/PG升级、原值、完整有效位置、FK与序列、无损往返、JSON null、保留引用、真正提交和拒绝有损降级断言。
- `uv run ruff check tests/test_setting_categories_migration.py tests/test_setting_fields_migration.py` → **All checks passed**。
- `git diff --check -- backend/tests/test_setting_categories_migration.py backend/tests/test_setting_fields_migration.py` → **通过，无输出**。

私有原始日志目录：`/private/var/folders/02/8zs7r3tx38sb90lkttcw585c0000gn/T/ainovel-scenes-ui-l7y3_blv/verification/`，文件 `foundation-migration-focused.log` 与 `foundation-migration-files.log`。

未运行全量套件；主会话统一重跑。没有发现或改动0039产品缺陷，没有改任何生产代码、迁移、其他测试、README/spec、任务状态或提交。以下两个文件已冻结，后续全量门禁由main负责。

## 冻结SHA-256

- `backend/tests/test_setting_categories_migration.py`：`8267b7ba60882d34f6356aee78af389523899fb22faab3f2c170f4653ca35609`
- `backend/tests/test_setting_fields_migration.py`：`45001b5978558c31a6686b30a3b3b8fa275c1b8b958535f8bc8c7a373a517497`
