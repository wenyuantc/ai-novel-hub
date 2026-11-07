# 独立场景：交付验证记录

状态：实现与必需验收完成，工作提交89e6da9，任务已归档为completed。用户已明确授权收尾、合并main并推送。首轮失败、修复与最终全量证据分别保留；未人工穷举的边界不记为人工通过。Git后续状态以会话日志及实际远端核验为准，不宣称已部署。

## 环境与证据边界

- 分支 `codex/independent-scenes`，原 HEAD `ae2eb72`。用户已批准完整闭环、统一初始景、保留结构、旧稿明确提取、七项继承覆盖及同章拆并排序。
- 后端显式使用 `LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8`。macOS 默认 C.UTF-8 曾使 native 编码为 US-ASCII；无效环境失败保留，不改中文 ini 或移除测试掩盖。
- 专用 PostgreSQL 16.14、端口 32814，四组专用库与 UUID schema、finally DROP；未连接用户数据库，不输出凭据。SQLite 实际 Alembic 链与真实 PG 分开验证，不以 create_all 替代迁移。
- 浏览器使用专用 headed Chrome、私有 SQLite/FakeLLM 与合成账号，验证实际 HTTP/SSE/UI、模型 messages 和服务端写入范围；不代表商业模型创作质量保证。
- 私有 QA 根：`/private/var/folders/02/8zs7r3tx38sb90lkttcw585c0000gn/T/ainovel-scenes-ui-l7y3_blv`。完整日志在 verification/，逐案 evidence.json、实际 messages 和截图保留；凭据、数据库和截图不纳入源码提交。

## 完整门禁

| 范围 | 实际结果 | 证据 |
|---|---|---|
| 原 HEAD 隔离后端基线 | 2517 passed、1 warning、962.61s；合法 locale、旧 PG 启用 | 隔离 baseline 目录，见 execution-log.md |
| 第一轮本次完整后端 | 18 failed / 2640 passed、1 warning、691.83s；604份源码 cut 未变 | verification/backend-full.log、backend-run.json |
| 首轮修复复验 | 历史迁移34 passed；人工/API 79 passed；AI 194 passed、真实PG 5 passed | research/full-suite-*-fixes.md 三份报告 |
| 第二轮本次完整后端 | 2665 passed、1既有SDK warning，635.15s（runner640.13s），exit0；604份源码cut全程未变，四组PG启用，无跳过 | verification/backend-full-2.log、backend-run-2.json、backend-source-cut-2.json |
| C12无历史纯空白清空 | 永久四参数先1 failed/3 passed，再4 passed；章节／两类版本／场景／审计44 passed，保留逐字符原稿和空白快照且可恢复 | research/c12-whitespace-history.md |
| 第三轮最终完整后端 | **2669 passed、1既有SDK warning，693.90s（runner698.74s），exit0**；604份源码cut全程未变，四组PG启用，无跳过 | verification/backend-full-3.log、backend-run-3.json、backend-source-cut-3.json |
| 全 backend Ruff | 通过 | 主会话运行及各 owner 交接 |
| 主会话独立前端（C11前） | lint/build/test 全部 exit0；145文件1042测试，642份源码 cut 未变，合计50.38s | verification/frontend-{lint,build,test}.log、frontend-run.json |
| C11首轮后前端 | owner及main独立lint/build/test均通过；main 147文件1052测试、644份源码cut稳定、90.4s | verification/frontend-*-final.log、frontend-run-final.json |
| C11最终完整前端 | 真实effect RED→GREEN、focused32通过；owner及main独立lint/build/test全部exit0；main **147文件1054测试、644份源码cut稳定、47.6s** | frontend-*-final-2.log、frontend-run-final-2.json、frontend-source-cut-final-2.json |
| 上下文清单 | implement/check 各31条，validate 全通过；9份摘录逐字符、行号、SHA匹配且均小于32768B | 主会话独立脚本、task.py validate |

首轮18项不是原 HEAD 基线失败。真实回归与必要 fixture 兼容分别修复，保留原日志；未删权限、安全、角色、配额断言，未扩大5秒屏障、改坏历史载荷冒充好 fixture，未将历史0035/0036迁移直接升 head。

## AC 到实际证据

自动化文件均在 backend/tests/；浏览器 case 名对应私有 evidence.json。下表不把 focused 或浏览器通过误称最终完整套件通过。

| AC | 自动化入口与关键断言 | 实际界面／补充证据 |
|---|---|---|
| 1 三景正文／摘要／身份 | test_scenes、test_scene_versions | browser_three_scenes_keyboard_save_reorder，刷新后 HTTP 核对 |
| 2 POV／标签／全类型隔离 | test_scene_planning 的 all_reference_types、test_scene_context | browser_pov_override、browser_matrix_clear_and_chapter_inheritance；全引用类型由自动化穷举 |
| 3 同序目录／阅读／导出与 CAS | test_scenes 的 pagination_reorder；test_reader、test_export、test_epub | browser_native_drag_reorder_and_txt，原生 drag 事件和 TXT 实际顺序 |
| 4 v7独立书与映射 | test_scene_transfer 的 v7_roundtrip，含事件、退场身份和历史 | browser_v7_backup_restore，实际文件上传／预览／导入，3景和22结构历史重映射 |
| 5 各景规划／时点真实输入 | test_scene_generation 的 target_preserves_siblings[mode] | browser_scene_ai_generate，实际 messages 和只改目标正文 |
| 6 矩阵／网格／批量同一规划 | test_scene_planning 的 batch CAS、matrix/weave；test_scene_context | browser_matrix_clear_and_chapter_inheritance、browser_restore_tag_inheritance |
| 7 Scene end 不提前生效 | test_scene_progressions 的 resolver/HTTP/reorder/职业限额回滚 | browser_scene_end_progression_and_preview，早景／本景 start 空，end 和下一景生效；C10文案已实看 |
| 8 旧稿精确保留／全部建章入口 | test_scenes_migration、test_scenes_postgresql、test_scene_transfer 的 real_v6_upgrade；旧 plan/book 测试 | 实际0038升级逐列／逐行核对；浏览器合成资料不冒充迁移 |
| 9 整章原子／主失败／取消／批量 | test_scene_generation 的 whole 三 mode、failure/stale/cancel、batch 前章 checkpoint；test_scene_ai_postgresql | browser_whole_chapter_ai_atomic、browser_whole_chapter_cancel_at_second_scene，第二景主调用 Gate 实际到达 |
| 10 新结构与旧稿提取 | test_scene_versions、test_scene_review_regressions 的历史引用 | browser_structure_restore、browser_legacy_text_explicit_extraction；旧历史明确为私有 QA seed |
| 11 七项继承／覆盖／明确空 | test_scene_planning 全七项；test_scene_context 的 manual cast/never/hidden | 矩阵清空、章默认联动、恢复继承；实际 AI 不重新 union 被覆盖的章手动 cast |
| 12 Unicode 拆并及事实阻塞 | test_scenes 的 unicode_split_merge；test_scene_progressions | browser_unicode_split（UTF16=9、码点=8）、adjacent_merge_conflict_choice、anchored_merge_blocked |
| 13 权限／系列／发送门禁 | test_scene_generation 的 scoped_routes；test_scene_review_regressions 三SDK真实HTTP拒发；series 测试 | browser_readonly_scene_editor、same_actor_permission_revocation；最新非法ID422／错章404且零新增出站 |
| 14 五局部／过期／跨景不误写 | test_scene_generation 的 five_local_operations、old_cross_scene、apply-log/timeline；test_scene_dispatch | browser_five_local_ops_emoji、two_tab_stale_save，保留输入和显式核对重试 |
| 15 实际 SQLite 与 PG | test_scenes_migration 3；test_scenes_postgresql 2；test_scene_ai_postgresql 5 | 专用 schema；真实 CAS、rank互换、FK／级联、安全降级与拒绝有损降级，无跳过替代 |
| 16 旧入口／Agent／effects | test_scene_agent、test_agent_tools、test_agent_effects、旧 chapters/plans | 真v5默认兼容／后来作者数据冲突、v6 undo、仅历史JSON依赖与同批合法赢家 |
| 17 一次整章分析／单景 stale | test_scene_generation 的 exactly_one_full_analysis[mode]；test_revise | browser_scene_ai_generate 无伪全章分析；C11补 UI 不虚等全章评分 |
| 18 提及／统计／阅读各格式 | test_scenes 位置解析；test_mentions、test_mention_compat、reader/export/epub | browser_mentions_scene_mapping_and_invalidity：valid 真实选区[0,2]，stale 可见，cross_scene 只读通读 |
| 19 非法备份／失败整书回滚 | test_scene_transfer 的 bad_core_graph、inherit夹带；test_scene_review_regressions 错章unit；旧project_transfer | 预览拒绝核心引用／投影矛盾及历史所属错误；成功备份经真实文件 UI |
| 20 浏览器闭环／有限加载 | test_scenes 的 no-autoflush／不选正文／分页版本；前端草稿／偏移／排序／权限 | 22类以上真实 case；375px 文档及 viewport 均375；明暗截图实看、键盘保存／选项、按钮替代拖动 |

## 独立审查与限制

- C1–C12全部修复并独立复验，见research/check-report.md。C11覆盖同景和未订阅异景任务两入口，独立32focused及最终147/1054完整前端通过；C12独立原RED复跑1通过、永久四参数另4通过，并被第三轮最终完整2669通过覆盖。
- 最新后端重启后实际完成单 Scene 自动续写：2次调用、一次 revision、兄弟景和元数据不变。非法 Scene ID 均422、错章404且不发模型请求。
- 实际批量 UI 选择两章（3景／2景）并开启逐章审核：第一章一次CAS后暂停，第二章正文及版本未动；点击继续后总计5次调用、每章一次revision，身份／顺序／摘要／规划不变。自动分析在这组私有作品中明确关闭，不混入额外分析响应。
- 第二轮完整后端虽全绿，随后已证实C12需修；不能把该轮作为C12后最终源码全量证据，修复后重新完整验证。
- 最新QA实际经versions=false备份导出／合法导入产生无历史单景章（不手工改数据库），纯空白旧PATCH后保留精确原稿与空白两份结构版本，同一Scene可恢复原稿。
- C11已在最新dist真实界面验证：抽屉显示稳定Scene#1、实际主调用Gate运行提示也为该景，空目标使用21字而非章级300；只改目标正文、章分析变stale且分析任务总数仍1，完成后无伪全章评分或“正在重分析”。
- C11异景最终两tab验证：预先打开Scene3并保留未保存emoji稿，另一页发起Scene1润色；本页明确显示另一景后台写作、不接其流，结束后自动刷新“已过期”，本地稿和Scene3服务端原稿均保持。主Gate实际到达，分析任务数前后2／2，一次内容版本，逐案evidence.json共29条均通过，无待验字段。
- 浏览器覆盖主要闭环、双页冲突和同 actor 撤权；不声称任意延迟顺序、故障、全类型和格式组合已人工穷举。其余以自动化可控屏障和契约测试覆盖。
- 不包含跨章移动、自动拆并、富文本、完整自动保存或全书搜索；自由文本剧透和模型创作遵从不是本任务保证。
- 独立最终全范围检查通过，无未解决实质性发现。自有两个Chrome会话、自有QAserver及专用PG实例均已停止，29项UI／HTTP证据和日志留存；没有停止他人资源。
- 用户已确认Phase3.4完整提交计划及后续收尾／合并／推送。两个原有source-command skill目录始终排除；仅对已验收的同一源码执行Git流程，不擅自部署应用。
