# E3 场景 AI 对外冻结合同

2026-11-07。由主会话冻结供前端先行对接，后续 AI 实现代理独占实现；复用 foundation／planning 合同，不另造 Scene／投影／继承。

## 路由与请求

前缀：/api/v1/projects/{pid}/chapters/{cid}/scenes/{sid}。

- POST /generate：现有 GenerateChapterRequest 的模型／instruction／target_words／auto_continue／style／skill_id，加必填 expected_content_revision、expected_planning_revision。
- POST /polish：现有 PolishChapterRequest 参数，加上述两版本。
- POST /revise：现有 ReviseChapterRequest 参数（analysis_id、suggestion_ids、instruction、preserve、target_words、模型／style／skill），加上述两版本；analysis 必须属于本章。
- 三者返回 202 Task。场景目标由路径固定，不接受请求体里另一个 scene_id 或 target。
- POST /rewrite：现有 RewriteRequest（op/start/end/instruction/style/skill/模型），加必填两版本。start/end 是当前 Scene 内码点，不是章偏移。返回原同步 SSE。
- POST /rewrite/apply：现有 RewriteApply（start/end/original/before/replacement/log_id），加必填两版本；目标由路径决定。成功返回 SceneOut，章统计／投影可按原 onChanged 刷新。
- 所有新 wrapper extra forbid、ID／revision 严格非 bool 整数；归属／owner／批量／章生成状态在实际入口校验。

原整章 API 保留路径和原参数；可增加可选 expected_content_revision／expected_planning_revision 供新版页面提交，不使旧请求突然必填。多个场景自动走有序整章编排，只有一个初始场景也不再直接写 Chapter.content。

## Task 和事件

- 不新增 TaskKind。仍为 chapter.generate／chapter.polish／chapter.revise，共用 chapter:{cid} 资源键。
- TaskOut 新增 scene_id: int|null，缺失／null 表示整章；前端按兼容可选 nullable 字段处理。后端从可信 task.input.request.target 解码，不为输出额外查 Scene，不把完整 request 暴露给 UI。
- 内部可重放 request.target 为 {kind:chapter} 或 {kind:scene,scene_id:SID}；没有 target 的旧请求按 chapter。保存创建时请求参数，但重试重新读取／核验当前目标与快照，绝不复用旧场景提案或自动改认同序号新 Scene。
- SSE 保持 snapshot／delta／continue／progress／done|error。进度 stage 显示“场景 i/n · …”，不把标题或阶段文字混进正文 delta。
- 单场景任务的输出 text 为该场景提案；整章任务 text 为整章顺序提案。整章流式预览放独立只读 AI／通读区，不能写进当前 Scene textarea 冒充它的正文。
- 成功 done 沿用引擎 result 包装，result.chapter 为更新后的 ChapterOut；单场景另有 result.scene（SceneOut），整章提供 result.scene_ids（按提交顺序）。旧客户端只取 chapter 仍可读。
- 单 Scene 任务重新打开时只自动接回 TaskOut.scene_id 匹配的场景；整章任务单独显示，不把其他 Scene 的流当当前草稿。任何 done 都不能覆盖已切换目标的未保存输入。

## 局部改写 done 与采纳

done 保留 text/start/end/original/before/log_id，追加 scene_id、chapter_id、content_revision、planning_revision。两个版本来自预览快照，采纳请求按 done 中的版本提交，不拿当前最新版本自动覆盖。

只允许同一 Scene 的原文／before／日志及快照匹配。旧章级局部请求若能唯一映射一个 Scene，可桥接；跨 Scene／分隔／歧义在预览前拒绝。采纳后返回 SceneOut，其他场景保持。

## 错误与已确认边界

复用 stale_scene_revision／stale_planning_revision／scene_context_changed／scene_target_required 及原 owner／生成／批量错误；保留草稿，核对后显式重试。

整章所有主场景结果通过最终快照核验后统一提交，不能逐 Scene commit 半章；polish/revise 空 Scene 原样保留。独立 Scene 改稿标记原章分析过期，不触发伪全章自动分析；整章完整成功才走原一次章分析。

本文件只冻结 wire 与安全语义，不扩大已批准产品范围。AI 代理发现无法满足时先协调主会话，不自行省略版本／私有配置门禁或退回无目标整章写入。

## E3 实际重试与预览采纳补充

- 仅用户显式调用 `/tasks/{id}/retry` 时，重新在原 owner／项目屏障内核验持久化 target 的真实 Scene 仍属于原章且活动，读取当前两版本创建新任务；旧 Scene 退场即拒绝，不以序号或标题认领替代场景。重试保留原 Skill 快照，不保留旧提案／预览日志。新任务排队、真实发送及最终保存继续核验这次新快照；普通409不被自动重签／重试。
- 新 scoped `/rewrite/apply` 必须携有效成功预览的 `log_id` 才能核验时间线／字段／可见性 stamp；缺失或失效返回409 `scene_context_changed`。body维持可选类型兼容，前端正常使用 done.log_id。真实旧章级无日志采纳仍仅保留原文／before的单Scene乐观兼容，不扩成跨Scene覆盖。
- `test_scene_generation.py` 的整章确定性 Gate 竞争和 staged 成功用例同时在 `test_scene_ai_postgresql.py` 运行；后者复用 foundation 的专用本机 scenes_test/scenes_verify UUID schema隔离，不使用公共schema或真实LLM。

- 批量生成仍保留范围外任务／人工维护及“开始某章前发现已删除则跳过”的原行为。但当前章模型调用期间删除其他章或修改会推进项目 `planning_revision` 的元数据，会令该章在飞行的快照失效，当前未提交章失败且原稿不变；不等同于给旧 messages 重签。已提交前章不回退。旧batch两条精确竞态断言按主会话批准更新；另以提交后、下章构建前的确定性Gate单独验证缺章仍skip。

- 内容快照明确以目标章为边界：保留目标章正文／位置与项目规划、时间线／字段／分类／可见性戳；前章普通正文只作为构建时背景，不额外绑定其他章的正文revision，避免破坏既有同项目多章并行。全局结构／改号仍有规划屏障，同章逐场景staged内容由目标章版本统一保护。
- scoped 路径及模型／Skill／分析／日志／临时风格ID均是严格正整数并受PG int32身份范围约束；零、负数、超大数不进入内部target或数据库。错章／失活Scene先做404归属校验，再检查内容／规划版本和本人私有配置。长章标题保留场景ID后缀。
- effects v6按结构签名及真实旧迁移默认校验；保留结构历史JSON的引用同样进入raw Core undo依赖图，复用备份remap的精确引用语义。只有本批真正一起删除、或其before已不再引用目标的资料才能随目标一起撤回；不无条件调用普通delete guard，也不忽略作者后加本地模式／节点／关联。
