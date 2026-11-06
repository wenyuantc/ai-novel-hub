# 字段子任务完整后端独立检查

日期：2026-11-05–06。Active task：`.trellis/tasks/archive/2026-11/11-05-typed-setting-fields`。

本报告属于字段子任务的后端检查，不代表前端／浏览器或父任务验收通过。未提交、未改变任务状态，未操作 `pelican-cycling.html`、8000 用户服务或用户数据库。

最终后端结论：八类发现均已修复；全后端Ruff通过，原样完整pytest **2146 passed, 1 skipped, 1 warning in1006.15s（16:46）**，实际退出0。字段冻结版本的F-AC11后端扩展接口可交接，已向主会话明确释放全部后端产品／测试ownership（含获追加授权的docs修复）；其后本代理仅写本research报告。分类阶段由主会话另行启动和验证。

## 范围与依据

- 读取完整 hook 保存文件、check／implement manifests、PRD→design→implement，及父研究的 implementation-contracts、planning-gate、consistent-read-contract、runtime-environment、core-check。
- 已检查本任务全部后端差异，包括未跟踪的迁移、模型、schema、目录／字段服务、codec、格式器及测试。检查不局限于最后一个切片。
- 核对领域路径：模型／0035→codec／纯 resolver→配置／条目 API→旧时间线路由；人工／组合／向导／拆书创建、换类和删除；生成／改写／分析／规划上下文；project v4、cards v4；工具／effects v3。
- 先前 core-check 的 **117 passed** 真实 PG 核心证据已读取，未重复同一局部探索；本轮补实际 PostgreSQL 服务层并发与导入图证据。
- 所有后端 owner 明确释放后才修改；前端与 spec 由主会话收敛，本报告不代替其验收。

## Findings (fixed)

### 1. 旧备份仅夹带未来字段配置时被静默清空

- File：`backend/app/services/project_transfer/importer.py`、`backend/tests/test_project_transfer.py`。
- Issue：`_migrate_v3_to_v4` 无条件把三张新表重置为空；v1–v3 仅夹带定义／选项／句柄、没有共享绑定时，预览会成功并丢弃新配置。原回归只覆盖同时夹带绑定的情况。
- Fix：逐级迁移到 v4 时先拒绝非空未来字段表，再初始化真实旧格式的空表。真实 v1–v3 原有文本和历史兼容保持；新增回归覆盖三个旧版本与三张表。

### 2. 合法空绑定缩适用范围后无法备份往返

- File：`backend/app/services/setting_field_values.py`、`setting_field_service.py`、`project_transfer/importer.py`、`backend/tests/test_project_transfer.py`。
- Issue：正式服务允许无值、无事件的空绑定被缩范围排除，完整图验证也允许；导入 `_field_specs` 却无条件要求该绑定仍适用，拒绝自己合法导出的 v4。
- Fix：抽取共享纯判据 `has_field_content(value, has_events=...)`，服务与导入共同使用。空串／JSON null 且无事件允许保留绑定身份；任何保留事件（含 inactive／orphan 的空值事件）仍保护范围。
- RED→GREEN：前两项新增 **7 failed in 5.21s → 7 passed in 5.10s**。四字段均覆盖实际保存、缩范围、备份预览／导入／再导出；另验证 inactive+orphan 空值事件仍被拒绝。

### 3. 合法空绑定的卡片导出无法再导入

- File：`backend/app/services/card_service.py`、`backend/tests/test_cards.py`。
- Issue：卡片导出包含被缩范围排除的空绑定，严格的新建字段预览随即拒绝此卡片。
- Fix：经主会话明确确认，卡片是当前快照，导出省略合法“不适用且无资料”的空快照及无其他使用的定义；完整作品备份仍保留绑定身份。导出先运行统一全图验证，不能把仍有 inactive／orphan 历史的非法绑定隐藏。其他实体仍使用的定义和值保留；`_typed_plan` 的新建适用性闸门不放宽。
- RED→GREEN：**5 failed in 4.32s → 5 passed in 3.04s**。四类型执行导出→预览→导入，并核对源绑定未删除；额外覆盖其他实体资料保留和非法隐藏历史明确 409。

### 4. 新卡片字段排序未完整复用严格数据库边界

- File：`backend/app/schemas/card.py`、`backend/tests/test_cards.py`。
- Issue：选项排序接受超 int32，可在 PG 导入时溢出；定义／选项还会把 bool、数字字符串先强转为 int，从而绕过正式配置入口的严格整数约束。
- Fix：两者复用 `DbInt`，启用状态保持严格 bool；选项复用 `OptionCreate`，定义继续复用 `DefinitionCreate` 校验。仅收紧新 v4 字段配置的非法输入，不改变 DTO 形状或旧卡片兼容。

### 5. 撤销可把保留事件错绑到主键复用后的另一时间线

- File：`backend/app/services/setting_field_service.py`、`backend/tests/test_setting_field_agent.py`。
- Issue：`_stored_values` 通过字段找到其时间线，完整图校验没有再比较事件自身 `timeline_id`。原事件已删除后，合法删除空定义／绑定，SQLite 可将原字段主键复用于另一主体；随后撤销恢复 inactive／orphan 事件时，当前物化跳过它，错误恢复会成功。
- Fix：统一 `validate_project_field_graph` 查询所有相关保留 custom 事件（也检查跨作品的错误引用边），要求事件与绑定属于同一时间线。effects 既有事务把该失败转为 `undo_conflict` 并整体回滚，不改公共 schema／FK，不改变 legacy 字段主动删除的历史清理语义。
- 导入一致性：`_Importer.run` 在所有行／引用回填、获取既有项目锁之后，物化之前调用同一 `validate_project_field_graph`，保证导入后的真实重映射图也经过正式校验；源文件的预校验仍保留。
- 后两项 RED→GREEN：**9 failed, 2 passed in 2.51s → 11 passed in 2.15s**。包括排序八例，以及 inactive+planned、active+orphan、inactive+orphan 三类真实 undo 错绑与失败后全快照不变。

### 6. 卡片导出配置被并发删除时错误为500

- File：`backend/app/services/card_service.py`、`backend/tests/test_setting_fields_postgresql.py`。
- Issue：批量值投影完成后、读取导出配置前，第二 PG 事务可合法删除无资料定义与绑定；代码在末尾版本核对前访问已缺失的 definition ref，触发 KeyError／500。
- Fix：保存值投影对应的目录版本，并在配置读取后、消费元数据之前先核对；保留末尾核对。该竞争现在返回带 fields scope 的409，不能生成混版快照或内部错误。
- RED→GREEN：真实 PostgreSQL 两个 HTTP 事务与显式屏障 **1 failed in 5.15s → 全部3个PG服务回归3 passed in 7.53s**，无skip。

### 7. SQLite绑定身份仍可能复用到同主体的另一共享定义

- File：`backend/app/models/progressions.py`、`backend/alembic/versions/0035_typed_setting_fields.py`、`backend/tests/test_setting_field_agent.py`、`backend/tests/test_setting_fields_migration.py`。
- Issue：不同时间线的图一致性保护不足以区分同一主体的新定义。原绑定表未启用 AUTOINCREMENT，合法删除最高空绑定后，正式目录／基线服务创建另一同类型定义的绑定会复用旧主键；一个实际 snapshot／diff 产生的旧保留事件效果可因此恢复到新定义，原 undo 不报冲突。
- Fix：主会话明确按已批准稳定身份合同选择在尚未交付的0035中修正：模型与迁移共同设置 `sqlite_autoincrement=True`，不增加抢占分类的0036，不按类型猜身份、不吞撤销。升级保留原 ID／FK／原文／时间戳；PG保持原序列合同。downgrade显式还原0034表选项，既有有损降级保护仍先执行。
- RED→GREEN：同主体／同值类型误接、真实迁移删最高203后又分配203、升级／create_all缺AUTOINCREMENT，**3 failed in3.02s → 6 passed in8.28s**。六项包含保留的三类跨主体显式Core复用图防线；常规分配现在必须大于已删除最高ID，旧事件不能借新定义复活。
- 测试边界：误接效果使用正式 `effects.snapshot/diff/undo`，事件删除以Core模拟单个保留事件删除效果；后续删除空定义、创建另一line定义及空baseline均走正式字段服务。它验证通用效果恢复合同，不表示前端新增了单独硬删事件操作。

### 8. 每实例文档闭包被框架分类缓存保留整个应用图

- File：`backend/app/api/open/docs.py`、`backend/tests/test_open_api.py`。该处字段范围外根因修复由主会话明确扩大ownership并批准，未触碰共享任务引擎／测试helper。
- Issue：已结束fixture且funcargs清空后，独立8例的app、TaskEngine、DB engine及新Body schema仍全部存活，手工GC只回收168项。独立两例的冻结对象反向图证明全局根链：`fastapi.dependencies.models → _is_async_gen_callable_cached → _CallIdentity → install_docs.<locals>.openapi_schema → closure cell → FastAPI app`。该进程使用FastAPI0.159.1；没有据直接路由环猜根因。
- Fix：两个文档handler改为模块级函数，经 `Request.app` 读写每实例schema缓存，`install_docs`只登记GET路由。原路径／函数及route name／匿名文档／实际Key校验／root_path／cache行为保持；不清分类LRU、不改GC阈值、不增加helper超时。core owner另做只读独立复核，未发现合同变化。
- RED→GREEN：永久测试先执行两个实例的真实HTTP docs/schema、独立缓存标记、root_path及route name，关闭客户端与资源后检查三类弱引用。旧实现 **1 failed in1.67s → 修后1 passed in0.53s**，原失败精确发生在未释放弱引用断言。
- 剩余根核对：修后同8例独立弱引用诊断 **8 passed in5.81s**，一次窗口外GC0.113秒后所有app／两类engine／动态schema均释放；ModelField回到基线2505、Dependant回到4328，没有残留根。原stream插件未加载，排除了它的包装持有。
- 因果边界：有界前缀已直接记录4.054秒gen2与4.030秒stream loop lag重叠，最大gen2接近10秒；该保留修复有明确内存生命周期回归，但未重建旧两次超时取消前的完整轨迹，不能将每次超时的细节当作已单独证明。

## 新增真实 PostgreSQL 服务证据

文件：`backend/tests/test_setting_fields_postgresql.py`。只接受显式 `AINOVEL_FIELDS_POSTGRES_TEST_URL` 指向本机 `field_test/fields_verify`；每个测试使用随机 `setting_fields_api_test_*` schema，finally 释放 engine／会话并 DROP。应用只注入 FakeLLM。

实际定向结果：先两项 **2 passed in 4.75s**，补第6项真实竞争后 **3 passed in 7.53s**，均无 skip。最终导入 helper 集成也纳入完整门禁。

1. 两个真实 HTTP 请求使用独立 PG session 并发修改同一目录版本，仅一项成功，另一项为带 fields scope 的 409；同样并发首次写同一字段，仅一项 timeline CAS 成功且只留下一个绑定／句柄。
2. 引用已编码并 flush 后才遇坏章节锚，绑定、句柄、事件、目录和 timeline 版本一起回滚。
3. 用事件屏障在一次字段读取中间，让第二个真实 HTTP／PG 事务提交定义改名；读取返回明确 fields 冲突，刷新得到对应新元数据与版本。
4. 从非空实例导出 v4，再导入同实例的新作品，六张字段相关表全部分配新 ID；四类型、停用选项、inactive／orphan 引用闭包及第5／20／21章显示保持，新作品不继承源 fields_revision。
5. 资料图已插入到句柄／绑定之后，注入真实 PG 唯一键失败；项目、风格及全部注册表计数不变；移除故障后原预览 token 可成功重试。
6. 卡片导出配置读取中途由另一 PG 事务删除空字段，得到 fields scope 409，见上面的独立失败／通过证据。

普通沙箱的本机网络返回 PermissionError；经自动审批使用授权本机测试连接后成功，没有把失败改成 skip。

## 后端验收覆盖

| 标准 | 主要后端证据 |
|---|---|
| F-AC1／3 | 严格 DTO／codec、配置适用性、选项归属、四类值、append／clear／PATCH；API 与纯测试 |
| F-AC2／6 | resolver 的5／20／21章、planned／start/end；FakeLLM 实际生成、五种局部操作、建议整章改写、续写、批量、分析和规划消息；预算与无递归正文 |
| F-AC4 | 0034迁移原 ID／原文／时间戳／事件保留，legacy 显式关联身份及冲突、不按名合并 |
| F-AC5 | 独立 organization／career_entry、旧 membership／career 分离，全部创建原语及组合回滚 |
| F-AC7 | 全保留历史删除／换类／向导准入与落库、工具／effects、项目软删恢复及 purge；真实 PG NO ACTION／CASCADE |
| F-AC8 | 0035 真实迁移；project v4 全图重映射、缺核心引用拒绝、空绑定与旧格式、PG 中途失败回滚 |
| F-AC9 | cards v1–v3兼容、v4循环／自引用／包外映射、default skip不授权引用、定义／选项显式映射、版本冲突与回滚 |
| F-AC10 | HTTP跨用户／只读／真实actor审计、无autoflush、目录分页、目录／timeline CAS与跨查询读取守卫；浏览器交互由主会话验收 |
| F-AC11 | EntityRef／FieldSpec／批量 resolve_entry_fields／format_entry_fields 与独立fields_revision；分类仍等待整个字段门禁 |

## Verification

- **最终完整门禁通过**：第8项修复后的 `pytest -q --tb=short -ra`，**2146 passed, 1 skipped, 1 warning in1006.15s（16:46）**，session84196实际退出0。日志 `/tmp/ainovel-fields-backend-01a1ac16-final-docs-fix.log`，独立真实退出码 `final-docs-fix.exit-code` 为0；`start.json`、`environment.json`记录本地2026-11-06 00:31:53开始、诊断环境全空。实际field PG、原10秒／全部断言，未加载任何 `/tmp` 插件。以下旧失败轮与诊断全部保留为追溯证据，不冒充此最终结果。
- Lint：全后端 `UV_CACHE_DIR=/tmp/ainovel-fields-uv-cache uv run --no-sync ruff check .` 通过。
- TypeCheck：后端 pyproject 没有 mypy／pyright 配置或命令，不能冒报独立类型检查通过。实际 `uv run --no-sync python -m compileall -q app tests/test_open_api.py` 通过；0035也通过先前独立编译及实际迁移门禁，Pydantic／导入由测试覆盖。
- 空白：字段冻结时已跟踪后端 `git diff --check` 与全部16个未跟踪后端文件逐个 `git diff --no-index --check /dev/null <file>` 通过；docs修复后已跟踪差异复核也通过。
- 早期字段集成修复定向回归：`pytest tests/test_cards.py tests/test_project_transfer.py tests/test_setting_field_agent.py tests/test_setting_fields.py -q --tb=short`，**128 passed in 69.22s**。覆盖新增导入后 helper 集成、所有旧卡片／备份与字段／effects 兼容路径。
- 绑定身份修复前完整 pytest：**2143 passed, 1 skipped, 1 warning in1003.66s（16:43）**，退出0，日志 `/tmp/ainovel-fields-backend-01a1ac16-final.log`。随后发现第7项身份缺口，因此此结果不作为当前代码的最终完整门禁。
- 身份修复后受影响 core／迁移／effects／transfer／cards／字段PG门禁：**298 passed, 1 skipped in107.28s（1:47）**，日志 `/tmp/ainovel-fields-backend-01a1ac16-identity.log`。唯一skip仍为下面的旧0031专用库测试；0035与本任务全部PG服务回归实跑。
- 第8项修复前第一轮完整 pytest：**1 failed, 2144 passed, 1 skipped, 1 warning in1100.96s（18:20）**，已确认进程退出1，日志 `/tmp/ainovel-fields-backend-01a1ac16-final-identity.log`。这是修复前失败记录，不作为最终门禁。
- 唯一失败：`tests/test_relations_generate.py::test_preview_conflict_rolls_back_new_types_and_entire_batch`，在关系生成202之后的 `tests/helpers.py::stream_task` 原10秒 `asyncio.wait_for` 中超时；栈停于ASGI stream依赖的AnyIO线程池checkpoint，尚未进入后续批量409／回滚断言。该失败不涉及绑定ID预期。
- 不改任何产品、测试、断言或原超时，退出全量进程后按主会话要求顺序原样复现两次：**1 passed in0.63s**、**1 passed in0.75s**，均退出0；两次都完成batch409、全部表计数不变、已存在关系原文保留的断言。日志 `/tmp/ainovel-fields-backend-01a1ac16-stream-repro-1.log`、`/tmp/ainovel-fields-backend-01a1ac16-stream-repro-2.log`。当前只证明未独立复现，不能据此宣称环境根因已查明。
- 主会话确认关闭本任务IAB测试页、正常停止8014 FakeLLM与5179 Vite并检查两端口无监听，PG容器保留，全部前／后端产品冻结。按其明确授权在此环境中原样重跑：**1 failed, 2144 passed, 1 skipped, 1 warning in989.71s（16:29）**，实际退出1，独立日志 `/tmp/ainovel-fields-backend-01a1ac16-final-quiet.log` 与退出码 `/tmp/ainovel-fields-backend-01a1ac16-final-quiet.exit-code`；启动前全后端Ruff再次通过。没有继续盲目全量重试。
- quiet唯一失败改为 `tests/test_progression_writeback.py::test_early_analysis_writes_end_event_without_overwriting_later_state`：首个晚章分析202之后，同一个原10秒 `stream_task` 超时，此次栈已到Starlette／httpx的流响应等待。202时间23:46:30.585，teardown的engine519“任务结束状态写入失败”在23:46:46.822，晚16.237秒；不能据此把取消后的报错当作原始根因。
- quiet约90%时唯一进程采样：PID76935，elapsed15:02，CPU74.3%，RSS7,149,536 KiB（约6.82 GiB），5条线程记录，lsof46行／数字FD16；日志 `/tmp/ainovel-fields-backend-01a1ac16-quiet-process.log`。这些只是现象。完整汇总后曾有工具等待，之后工具确认实际退出1、ps确认PID和父进程已不存在；没有证据证明pytest汇总后常驻，未kill进程。
- quiet失败节点原样复现两次均 **1 passed in0.71s**、退出0，完整早／晚章事件、来源关联和revert保持晚章状态断言均执行；日志 `/tmp/ainovel-fields-backend-01a1ac16-writeback-repro-{1,2}.log`。
- 主会话授权仅 `/tmp` 插件记录monotonic的持久终态、订阅注册／复查、末事件入队／出队／关闭、loop延迟及GC；原helper、10秒和业务断言保持原样。两个失败节点同进程小例 **2 passed in3.50s**，三个流elapsed0.089／0.062／0.066秒，终态提交后约2–4ms取出done，随后约1ms流结束，最大loop lag约1.9ms；一次gen2 GC约0.093秒。未复现错误，也不能外推完整进程根因。日志 `/tmp/ainovel-fields-backend-01a1ac16-stream-diagnostics*`；尚无永久诊断代码或新的产品／测试修改。
- 两个受影响模块同进程带插件：**50 passed in32.19s**，退出0；插件覆盖其中49个带app夹具用例、46个流，最大stream0.286秒、最大loop lag0.195秒，8次gen2 GC最大0.684秒。`resource.getrusage(...).ru_maxrss` 高水位raw从234,962,944到631,963,648（本机macOS约603MiB峰值），不能当作结束时当前RSS。模块日志 `/tmp/ainovel-fields-backend-01a1ac16-stream-diagnostics-modules.log` 与summary.json。临时插件随后修正undo清理，避免留下原本继承的方法绑定到engine实例的额外自引用。
- 仅collect-only收集原顺序：全套2146节点，quiet目标位于第1246，含目标的前缀为1246节点／79文件，参数 `/tmp/ainovel-fields-backend-01a1ac16-quiet-prefix.args`。主会话批准一次上限15分钟的诊断，结果 **1245 passed, 1 skipped, 1 warning in572.71s**，退出0、未到限；外层runner644.534秒包含启动／退出，不据其与pytest计时差额断言退出阻塞。独立文件 `/tmp/ainovel-fields-backend-01a1ac16-prefix-diagnostics*`。
- 前缀采样边界：gen2回调只缓冲monotonic start／stop、generation和计数；重的 `len(gc.get_objects)` 与当前RSS只在每100用例／目标before-call边界取样，单独记probe耗时；maxrss高水位与currentRSS区分。无主动GC、阈值或任务逻辑修改。插件与有界runner均只在 `/tmp`。
- 前缀关键证据：最大gen2 **9.9074秒**、collected534926／uncollectable0，结束在目标test.start前23.61秒；目标两流0.0758／0.0454秒，snapshot→done，目标期间无gen2、maxlag0.000483秒。另一batch流里4.054秒gen2结束后0.000278秒记录4.030秒loop lag，已精确重叠。目标边界对象29,321,575、当前RSS4,754,864KiB、maxrss_raw5,103,566,848，probe2.126秒发生窗口外。数据用于定位保留，不把该绿色前缀冒充最终全量。
- 第8项修复后，原样 `test_open_api.py` 全文件22项及原50项 `test_progression_writeback.py + test_relations_generate.py`：**72 passed in50.26s**，退出0；不加载任何 `/tmp` 插件。全后端Ruff、compileall、差异空白检查通过。
- 主会话依据上述证据明确授权修后最终完整门禁，最终结果见本节首条。启动前核对PYTEST_PLUGINS／PYTEST_ADDOPTS／PYTEST_DISABLE_PLUGIN_AUTOLOAD／PYTHONPATH及两个诊断变量全为空；整个最终进程内产品冻结。取得真实退出0后才释放ownership，避免分类模型影响尚未退出的字段测试。
- 唯一skip：原有可选 PG 测试 `test_0031_postgresql_cascades_sources_and_subjects` 要求独立 `AINOVEL_POSTGRES_TEST_URL` 且硬编码仅允许 `ainovel_test` 数据库；本任务仅获授权 `fields_verify`，没有将本任务 URL 冒充该变量。本任务0035迁移与新增3项服务PG均在当前完整轮实际执行，无skip。
- 唯一warning：既有 `test_gemini_current_sdk_imagen_unavailable_is_chinese` 触发SDK `generate_images` deprecation；未过滤警告或修改依赖。

最终完整门禁命令（工作目录 `backend`；通过自动审批获得本机隔离测试网络权限）：

```sh
env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 \
  AINOVEL_FIELDS_POSTGRES_TEST_URL=postgresql+asyncpg://field_test:local-fields-test-only@127.0.0.1:32814/fields_verify \
  UV_CACHE_DIR=/tmp/ainovel-fields-uv-cache \
  uv run --no-sync pytest -q --tb=short -ra
```

## Findings (not fixed)

没有尚未修复的字段后端产品发现。八类发现已有回归，最终完整门禁实际退出0；以下为明确保留的验证边界和范围外事项。

- 已修复独立证明的全局app保留根，修后原样完整门禁通过。旧两个10秒超时取消前的完整因果轨迹未重建，不能把每次细节都声称为已单独证明；原失败日志、局部绿色、GC／lag重叠、弱引用与根链证据均保留。没有清缓存、调阈值、放宽原10秒、过滤警告、跳过或删除断言；临时插件和runner只在 `/tmp`，没有进入repo。
- 前端表单／草稿／权限失效、375px／主题／键盘、两页冲突及真实浏览器，由主会话负责。本检查没有据此宣称字段子任务全部完成。
- 字段spec、前端索引及文档寿命经验由主会话同步；已核对 `setting-fields.md` 的空绑定／event一致性／SQLite身份合同，以及后端quality、cross-layer指南和 `docs-retention-retrospective.md` 的缓存／回收合同与最终实现一致。
- 既有 Gemini SDK deprecation warning 不属于本次字段差异，不通过过滤警告或修改依赖消除。
