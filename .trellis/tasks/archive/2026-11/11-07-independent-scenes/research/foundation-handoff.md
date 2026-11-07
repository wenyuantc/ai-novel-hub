# E0/E1 实现交接

## 已落地

- `models/scenes.py`、`schemas/scene.py`、`services/scene_service.py`、`api/v1/scenes.py`：稳定身份、内部派生字数、分页轻量规划视图、CRUD、同章CAS、无损排序／拆分／带签名相邻合并／软退场、可靠码点解析、设定预览路由。精确 wire 见 foundation-contract.md。split 右片复制 raw 意图，摘要留左，offset 非正文内部为422。
- Chapter.content_revision 与 ChapterVersion.scene_snapshot／has_scene_snapshot，模型导出；唯一0039（parent0038）。SQLite 原位加章列，重建 planning父表时关闭迁移连接FK、比较全部旧表行数并恢复序列高水位，重开FK；旧章逐字符单初始场景，历史null不改。PG safe downgrade 的约束名用op.f，已被真实PG回归覆盖。
- chapter_service／plan_service／book_import/service 的全部实际Chapter构造入口显式初始化；旧单Scene PATCH委托权威存储，多Scene整段拒绝。单Scene章有结构前缀时旧整章稿仅消费一次，不重复追加。
- 版本保存结构与原始本地意图；普通正文才按10分钟合并。结构、清空、恢复保留边界。新结构恢复预检并验证同章保留身份／规划epoch；旧纯文本多Scene只能预览并签名提取到指定Scene。结构退场若有独立事实或本地覆盖先拒绝。
- project_transfer v7／APP_SCHEMA0039：登记Scenes（含退场）、本地角色与新FK；旧v1–v6逐级真实转换为单Scene，先拒绝非法新资料夹带；校验正文投影／字数／真实历史身份／完整raw意图及继承不夹本地值。历史JSON在全部映射建立后延迟重映射unit/tag/ref/Scene，运行内容版本从0开始。流式导出内容／规划变化时中止，不生成混合伪完整文件。卡片格式未改，仍v6。
- 管理员写入复用chapter.update安全审计字段与真实结构版本引用，同事务提交，不记录正文。

## 只flush原语

均在 `scene_service`，签名见 foundation-contract.md：initialize_chapter、list_active_scenes、require_scene、project_scenes、content_fingerprint、check_revision、touch_revision、refresh_projection、replace_scene_content、replace_chapter_contents（完整活动ID集合）、replace_legacy_content（整章输入、仅单Scene）、resolve_chapter_range、capture_snapshot、restore_snapshot。

低层调用者先鉴权与拿项目／章屏障，自行负责baseline、审计、版本、外层commit/rollback。capture_snapshot 主动核验场景投影与Chapter.content逐字符一致，阻止未接入旧写入口生成矛盾历史；不会在GET补Scene。`version_service.add_initial` 现为 async，唯一book import调用已await。

## 已运行证据

均在backend；pytest显式 `env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8`。

- `uv run pytest tests/test_scenes.py tests/test_scene_versions.py tests/test_scene_transfer.py -q -x --tb=short`：22 passed（后续另加管理员审计与inherit非法夹带各1例，分别通过；final核心另重跑）。
- `uv run pytest tests/test_scenes_migration.py -q -x --tb=short`：3 passed，包含0038旧图全部原列逐行一致、FK ON、空章／回收站、原时间／空白及已删除planning id高水位。
- `uv run pytest tests/test_scene_versions.py -q -x --tb=short`：3 passed。
- `uv run pytest tests/test_project_transfer.py -q -x --tb=short`：37 passed。
- 原versions非AI相关与新core/version选组：16 passed、11 deselected；未冒充E3已过。
- reader/export/epub＋手写章／两条plan建章／两条book开始入口选组：23 passed。
- `test_migrations.py::test_upgrade_and_downgrade`、`::test_0031_setting_progressions_preserves_legacy_and_detaches_deleted_sources`、`test_planning_migration.py`：3 passed。旧迁移模型比较只排除0039新增列／FK，原历史约束断言保留。
- 私有connection.json注入`AINOVEL_SCENES_POSTGRES_TEST_URL`后 `uv run pytest tests/test_scenes_postgresql.py -q -x --tb=short`：2 passed。实际fresh链、0038存量升级、子行、CAS竞争／rank互换／级联／安全downgrade；各例UUID schema finally清理，未打印凭据、未改公共schema。
- foundation所涉全部Python文件ruff通过；git diff --check通过；12个新增源码／测试／迁移／合同以no-index逐文件空白检查通过。

## 明确交给E3与主会话

尚未切换、不得发布的旧正文写点：generation_service.py:421、rewrite_service.py:256、agent/effects.py:1081、agent/tools/write_text.py:67。请经上述正文原语再record，不能直接改Chapter投影。AI／Agent／effects／task／prompt没有被foundation改动。

测试fixture已显式适配本轮所有的test_project_transfer与test_chapter_versions，不做读时修复。其他直接ORM种子由main按新不变量适配。项目备份v6/0038硬编码断言剩余名单已发main；卡片v6与0038历史迁移不能机械替换。

未运行全量后端／前端或真实浏览器（按主会话资源分工）；README／TODO／spec、E3、跨层完整门禁由主会话完成。未提交、未暂存、未start/finish/archive任务。

最终核心复跑：`uv run pytest tests/test_scenes.py -q -x --tb=short` → **11 passed (10.10s)**，含最新422边界、插首默认分隔、缺场景只读拒绝与管理员安全审计。继承非法夹带追加例单跑 **1 passed (1.58s)**。
