# E5 旧测试兼容适配记录

## 本轮变更边界

旧 HEAD 在合法 UTF-8 locale 下2517项通过；本轮只适配新场景权威／项目备份v7的合法测试种子与明确格式断言，不改产品源码、不改新E3文件、不放宽正文一致性／权限／旧格式夹带拒绝断言。

独占范围为既有 `backend/tests`：首批检查 `test_mention_compat.py`、`test_setting_ai_visibility_formats.py`、`test_agent_api.py`、`test_category_transfer.py`、`test_planning_lifecycle.py`、`test_series_transfer.py`、`test_project_cover.py`、`test_setting_fields_categories_roundtrip.py`，必要时对同根因的旧直接ORM种子作最小适配。foundation拥有的test_project_transfer、test_reader和迁移文件不动；卡片保持v6、历史迁移保持0038等原指定版本。

数据流变化发生在测试构造点：Chapter持久化后显式initialize_chapter；已存在场景的正文变化使用场景权威原语。真实旧项目文件必须剥离scenes／本地角色／scene_snapshot／场景来源与锚点／覆盖模式；不以改version数字代替合法旧文件。AI／Agent四条旧正文写入口尚在E3切换中，相关产品失败只报告owner，不能改成允许矛盾章投影。

## 适配原因与检查证据

所有pytest显式 `LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8`；不输出专用PG凭据。

| 文件 | 适配原因／方式 | 保留的原合同 |
| --- | --- | --- |
| test_mention_compat.py | 当前导出schema改0039；缺笔记／aliases的旧文件明确构造v2／0031，并复用foundation helper剥离新场景／AI数据 | aliases新旧往返、笔记缺省、恶意输入和旧v2 effects断言原样保留；0033真实迁移不改 |
| test_setting_ai_visibility_formats.py | 当前完整项目导出改v7／0039 | 旧v4 effects转换、never／字段隐藏、旧版夹带AI资料拒绝不变 |
| test_agent_api.py | 项目备份中的当前schema断言改0039 | Agent改稿／历史／撤销及日志安全断言不变；旧正文旁路问题不在测试放宽 |
| test_category_transfer.py | 当前完整项目导出与导入重导出改v7／0039；旧v1–v4构造已有helper剥离新场景资料 | 原有旧类别夹带拒绝、真实PG事务失败、回滚与token重试不变 |
| test_planning_lifecycle.py | 直接ORM建章后flush并initialize_chapter，覆盖built种子、已有章、复用ID章、旧effect章、撤销建章种子；当前备份版本改7 | 旧章ID复用、规划epoch、节点／交汇、cast和撤销原断言不变 |
| test_series_transfer.py | 单书导出版本／schema改7／0039 | 本书采用边界、独立副本及发送保护不变 |
| test_project_cover.py | 完整项目备份版本／schema改7／0039 | 图片字节、EPUB、导入和封面限制不变 |
| test_setting_fields_categories_roundtrip.py | 当前父级完整备份断言改7／0039 | 原20章时间真值表、四类型字段及引用重映射和负控制不变 |
| test_setting_fields_context.py | 真实生成／重写测试的直接ORM章节集合在flush后逐章初始化Scene | 四类字段、旧模板、选择范围、章初时点和预算原断言不变 |
| test_setting_ai_context.py | 实际多章生成投影测试的三章种子显式初始化Scene；不为了统一形式改动其余仅章级只读种子 | never、字段隐藏、单向一跳及出站守卫原断言不变 |
| test_rewrite.py | 模拟“已有正文但未写版本”的单章原稿修改改用replace_legacy_content，不再留下Chapter投影与Scene不一致的非法种子 | 未保存版本的baseline、外章log忽略及Unicode选区断言不变 |
| test_agent_effects.py | 实际调用version_service的正文效果种子显式初始化Scene；后续模拟改稿通过replace_legacy_content维护单一正文事实。E3实测删除空章恢复用例因before缺Scene而快照不等，该直接ORM种子也显式初始化 | 后改冲突、原始版本保留、scope／hash／generating／原子性、删除恢复整快照相等全部断言不变 |
| test_progression_agent_effects.py | 安全旧纯文本撤销用例初始化当前Scene、通过权威原语改稿；旧历史明确scene_snapshot=null，旧chapter_text载荷只含当年的版本身份／hash | 单Scene旧纯文本能安全撤销，旧章位置影响时间线仍应拒绝；不是把新效果改版本号冒充旧格式 |
| test_setting_field_agent.py／test_setting_category_agent.py | E3发布effects v6后，两个当前diff格式断言由5改6 | 所有手写旧v1–v4载荷、字段／分类／可见性保护仍保留旧格式，不升级冒充当前格式 |

## 命令与结果

首轮八文件focused（84例，启用私有PG）：**14 failed、70 passed，71.48秒**。其中12处为预期需要改写的当前v6／0038断言，已只改成v7／0039；另2处为Agent正文旧旁路尚未切换导致合法替换失败，已交E3 owner，不改断言。

相同八文件适配后重跑：**82 passed、2 failed，74.67秒**；失败仅上述Agent两例。该轮包含真实PG分类备份往返与延迟约束失败回滚，零跳过。私有connection.json提供的URL仅由原UUID schema fixtures使用，未输出凭据。未运行全量pytest。

旧effects两文件在合法种子适配后：**39 passed、2 failed，10.88秒**；剩余精确命中effects._restore_text旧正文旁路，交E3 owner。

三文件AI兼容组（fields_context／setting_ai_context／rewrite）在E3模块切换期间先后出现过task失败与旧模板块断言失败，各因maxfail=8提前停止；没有修改任何提示词／时点／可见性预期。单例重跑已过，不据单例认定全组通过；当前正在全部三文件新进程重跑。

E3 owner已确认effects升6、可见性旧保护阈值仍保留5；`test_setting_ai_visibility_formats`仅当前diff格式断言改6，真实_v4转换与历史限制保护不变。

E3新源码后重新检查visibility／Agent效果／旧纯文本／API相关组：**61 passed、1 failed，40.92秒**。原四处产品正文旁路失败已实际通过；唯一失败为progression_agent_effects的“当前diff=5”断言，已精确改6并补跑。未因此改变旧效果版本。

三文件AI组完整重跑：**73 passed、1 failed，74.93秒**。唯一失败是下面经主会话裁定的可见性快照撤回行为，不是字段／旧模板／普通续写网络失败回归。

## 主会话批准的行为预期更新（单列，非fixture适配）

主会话依据已批准design§8与implement E3确认：生成首轮真实发送后作者收紧never，会撤回本次上下文快照；最终保存必须失败，章和Scene原稿、内容版本及正文历史均不改变。不能沿用旧“首轮成功＋续写warning”语义。

因此只更新 `test_setting_ai_context.py::test_continuation_does_not_send_first_round_context_after_tightening` 为终态error／failed，并增加章／Scene原稿为空、内容版本不增、无正文版本的精确断言。保留真实首轮哨兵与 `len(fake_llm.calls)==1`，明确注释区分一般续写网络失败（仍可保留主结果＋warning），不扩为所有续写失败都主失败。已通知E3 owner，与删除空章effects合法种子一起单跑：**2 passed，2.27秒**。

## 产品源码失败与处理记录（不在测试放宽）

- 初始 `test_agent_api.py::test_text_edit_records_agent_version_and_undo_restore`／`::test_agent_versions_and_success_error_logs_survive_backup` 的合法单Scene替换失败已交E3修复，并在后续61 passed组实际通过；原Agent日志与历史断言未改。
- 初始 `test_agent_effects.py::test_text_restores_by_version_and_rejects_later_content_change[False]`／`test_progression_agent_effects.py::test_legacy_chapter_position_rejected_but_pure_text_undo_still_works` 的_restore_text旧正文旁路已由E3修复，在同组实际通过；未删除旧兼容成功断言。

`test_planning_lifecycle.py::test_legacy_effect_cannot_delete_newly_added_planning` 原注释称v2但fixture用当前diff；现明确移除Scene effects和chapter.content_revision并标v2，保留“后加PlanningUnit阻止旧效果删章”的安全语义。不删除旧格式保护、不机械把旧效果升级v6。

## 最后补充门禁

- `test_setting_field_agent.py test_setting_category_agent.py` 加当前演变效果格式断言用例：**103 passed、1 skipped，76.85秒**。该命令只带UTF-8 locale，没有注入专用PG，所以旧snapshot_database的PG参数被跳过；不是产品失败，也不能记为PG通过。随后从私有connection.json注入URL只补跑 `test_setting_category_agent.py::test_real_database_deferred_rule_restore_and_legacy_preflight[postgresql]`：**1 passed，1.29秒**，真实PG延迟规则恢复／旧预检已验证。
- 本轮15个既有测试文件的focused Ruff：**All checks passed**；相同路径 `git diff --check`：通过。新增本报告另以no-index检查空白。
- 全库剩余0038／卡片v6断言已检索：均为历史迁移／安全降级／卡片格式合同，未机械替换。
- 本轮未运行全量pytest、不宣称全部AC通过；统一全量与独立检查由主会话执行。
