# E3 场景 AI／Agent 实现交接

状态：本代理产品源码与测试已冻结；全量后端、浏览器及最终文档由主会话拥有。没有提交、暂存、finish 或归档；没有修改 frontend、Scene 存储、0039 或 project_transfer 核心。

## 已落地行为

- 新 scoped generate／polish／revise／rewrite／rewrite/apply 均已注册，严格请求体与两版本、owner／真实同章 Scene 校验在私有配置及版本操作之前。输入身份是正整数且受PG int32范围保护。Task 类型及章资源锁不变；TaskOut.scene_id只从严格的持久化 request.target 解码。
- Scene正文唯一权威。独立目标只保存该Scene；整章按照原稳定身份／顺序生成提案，仅全部主结果成功、最终快照一致后一次CAS提交，保留标题、摘要、分隔及原始本地规划。polish／revise跳过空Scene但不丢结构。日志、版本及章状态同事务；极晚取消以真实提交为准。
- 后续Scene只能用内存暂存的最新前景构建衔接，不临时写DB，也不把旧前Scene正文重新混入。两层builder贯穿preceding_text；None走原前景，空串显式无前景。场景有效规划／POV／引用继续走AiScope和精确scene start投影；锁定scene_context不读取作者未过滤POV／refs。
- 新范围块为j2资源，默认及旧覆盖都通过锁定变量兜底；与规划／普通设定共用预算，日志保留按景目标字数和截断情况。显式整章总目标均分，不重复使用总字数。
- 原guard_provider新增可选快照verify回调，真实SDK每次发送lease内检查目标章内容／位置、项目规划及timeline／字段／分类／可见性。发送头后释放，不持锁消费正文流。前章普通正文是构建时背景，不额外绑定其他章content_revision；保留原合法多章并行。
- 五局部操作为Scene本地码点。旧章级预览／采纳仅唯一映射单Scene，跨景／分隔／歧义预先拒绝；新采纳须真实成功log_id＋done两版本，复核原文／before／精确目标／快照。失效409 scene_context_changed，不为旧提案重签。
- 显式retry重新校验原owner／章屏障内的稳定Scene及当前两版本，不按sort认领替代Scene，原Scene退场则404；保留Skill快照，不复用旧提案。独立Scene成功不排伪全章分析，整章完整成功只排一次原章分析。批量仍逐章checkpoint。
- Agent唯一匹配替换通过同一Scene范围及权威正文原语；跨景提案预览即拒绝。effects v6登记Scene／本地角色，结构与规划签名检测后改；运行内容版本不回灌。真正旧PlanningUnit／progression／version行只桥接0039已知默认，并按旧表集计算旧规划签名；有新场景来源／模式／本地角色或后来节点仍冲突。旧多Scene文本撤销明确拒绝，安全单Scene旧纯文本及无关旧操作保留。
- effects raw Core undo的依赖图新增ChapterVersion.scene_snapshot，复用备份remap精确收集实体／时间线／场景／规划／标签身份。流式只读历史快照列、只保留待删目标交集，不加载整书历史正文。保留同批一起删除或before已不再引用目标的合法赢家。

## 本代理修改文件

### 产品

- app/services/generation_service.py、rewrite_service.py、scene_ai_context.py（新增）。
- app/services/ai_dispatch.py、context_builder.py、planning_context_service.py（后两文件在E2正式交接后追加staging支持）。
- app/services/task_engine/handlers/chapter.py、task_service.py、batch_service.py。
- app/services/agent/effects.py、agent/tools/write_text.py。
- app/api/v1/scene_ai.py（新增）、api/v1/__init__.py（保留基础scenes router）。
- app/schemas/scene_ai.py（新增）、chapter.py、chapter_version.py、task.py。
- app/ai/prompts/chapter.py、rewrite.py、registry.py，四个chapter.*.user.j2；新增prompts/scene.py及templates/scene.scope.user.j2。

### 验证／任务记录

- 新tests/test_scene_generation.py、test_scene_agent.py、test_scene_dispatch.py、test_scene_ai_postgresql.py。
- 仅主会话逐例批准的test_batch_generate.py两条在飞行快照用例、test_planning_context.py一条续写旧快照用例；原范围锁、配置及安全哨兵不放宽。
- research/ai-contract.md、本文。其他旧tests fixture适配来自scene_planning／foundation，不是本代理编辑。

## 实际验证

全部pytest命令在backend下显式使用`env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8`，没有真实DNS／LLM。

1. 最终主要focused：

   ```text
   uv run pytest tests/test_tasks.py::test_per_user_limit_and_queue_position tests/test_scene_generation.py tests/test_scene_agent.py tests/test_scene_dispatch.py tests/test_batch_generate.py tests/test_planning_context.py -q -x --tb=short
   110 passed in 96.47s
   ```

   覆盖不同POV、手动cast替代、never／hidden／未来场景设定、staged前景、三模式、五局部Unicode、普通续写warning、旧模板兜底、重试／退场／Skill、整章分析一次、批量checkpoint、缺章skip与在飞行失效边界、错章／跨用户／非法路径、log及timeline过期、Agent新旧effects和历史引用安全。

2. 真正PG最终复跑：私有connection.json仅进程内注入AINOVEL_SCENES_POSTGRES_TEST_URL，`uv run pytest tests/test_scene_ai_postgresql.py -q -x --tb=short` → **5 passed in 8.64s**。每例使用foundation的专用本机scenes_test/scenes_verify UUID schema fixture，覆盖四类Gate主失败／取消／规划／timeline变化和staged全部一次提交；不输出凭据、不改公共schema。

3. `test_scene_dispatch.py`三个真实SDK＋本地HTTP证明：目标正文变化而visibility不变时也在真实发送点拒绝，服务器收到零请求。`test_scene_agent.py`最后单组**14 passed**，含5类实体历史保留和同批依赖一起删除成功。

4. C8真实负控制：只在独立pytest进程内把chapter_versions的semantic collector替换为空集，同一`[character-False]`真实Agent创建→作者引用→恢复inherit→undo回归为**DID NOT RAISE ConflictError**（pytest退出1为预期RED）；正常源码该例GREEN。未为负控制改任何磁盘源码。

5. 早期大组选组先后跑到**184 passed**（提示词／旧生成／重写／Skill等）及另一组**48 passed, 2 skipped**后遇到明确旧语义差；不是最终全量通过。三类精确旧假设已按main批准修正：规划／visibility stamp变化后不再续发／提交旧首轮；生成中的未来章删除／改元数据使当前项目规划快照过期；误加的跨章普通正文依赖已移除，原合法并行断言保持并重新通过。普通额外续写网络失败仍warning保留主稿。两项未配置PG环境的skip不能记作PG通过；本项新增PG五例已另外真实运行。

6. 最后`uv run ruff check .`全backend通过；`git diff --check`通过。新增文件单独no-index空白门禁见主会话最终记录。

## 剩余检查／边界

- 本代理没有跑全量后端、前端或真实浏览器，不把静态／API／本地SDK替身测试称为浏览器通过。主会话最终全量与QA结果优先。
- 当前没有已知未接入的旧AI／Agent正文写旁路：检索原四点已无直接Chapter.content赋值。剩余Chapter Core UPDATE是状态抢占／回退和scene_service原子CAS；effects._values只在恢复被删空章／空Scene时成组恢复已保存空白，再统一重建投影，不是无目标整段改稿入口。
- 同章Scene任务仍锁整章；其他章普通背景不要求追赶构建后的独立正文改稿，但全局规划／位置或相关设定变化仍使在飞行载荷失效。多个Scene会增加模型调用；不新增场景断点或自动场景分析。
- README／spec／TODO、提交／归档及完整AC验收由主会话最终处理。
