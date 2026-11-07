# 首轮完整后端：E3 七项修复交接

本轮由主会话授权诊断实际七个AI／任务失败；没有改其他迁移、规划fixture、README、spec、依赖或提交。产品源码与测试在下列focused完成后冻结，第二次完整后端由主会话统一运行。

## 分类与最小修复

| 首轮失败 | 分类 | 本轮处理及保留边界 |
| --- | --- | --- |
| test_ai_config_params::test_chapter_context_budget_from_config | 新锁定Scene范围块的必要预算预留，旧测试只比较原普通块预算 | 改为两个模型配置，捕获实际范围块、普通块预算／实际长度和planning预留，证明预留总和恰等于配置预算、实际总资料不超限；没有硬编码19796或204。产品不回退范围块。 |
| test_agent_tools::test_replacement_records_agent_version_and_stales_analysis | 旧fixture直接修改Chapter.content破坏权威Scene投影 | preview源码的公开content_hash一直保留。仅把原“人工改稿且不写版本”的种子换成replace_legacy_content，保留两次content_hash不同、Agent版本、旧稿及分析过期全部原断言；不做读时修复，不放宽invariant。 |
| test_admin_task_adversarial::test_private_title_hub_stage_error_and_content_are_neither_projected_searched_nor_audited | 真产品回归：to_out新增target解析对历史request字符串先调用.get | 提取只读_request_dict形状守卫，复用普通ID投影和Scene target解码。损坏target不作为合法Scene输出，原Task.input不变，写入／重试仍严格验证。原隐私哨兵及管理投影断言不变；新增13种历史JSON形状，直接序列化＋HTTP均不500且不修改原载荷。 |
| test_cast_context::test_chapter_context_cast | 真产品回归：当前Scene素材替换掉原章／outline梗概，丢自动选角 | prepare_chapter_settings把普通章任务梗概与当前Scene素材拼为自动匹配输入；手动集合仍只取统一有效规划，不union被override的章手动cast，也不加载未来Scene正文。原苏晚／张三／主角／手动角色顺序及全部角色断言未修改；新的never／hidden／场景覆盖／staged隔离focused同步通过。 |
| test_revise::test_revise_versions_logs_and_reanalysis | 旧章级单Scene默认300下限丢失 | 按主会话批准：仅旧chapter目标且活动Scene总数为1时，默认max(原字数,300)；显式Scene、多Scene仍按各景原字数，指定整章总目标仍均分，空景仍不调用模型。params.target_words记录真实总目标，每景日志与实际提示词一致。原版本／建议／自动重分析断言不变；新增5种默认／显式／多景／空景／分配与日志合同。 |
| test_styles::test_legacy_style_field_creates_original_style | 旧全messages逐字比较未排除必要的新锁定范围块 | 只从最后user段分离新增锁定Scene范围，其余全部消息仍与旧实现逐字符相等；继续逐字核对旧风格字段，并独立断言新范围块恰一次、目标字数及单Scene限定仍存在。没有修改产品模板或取消范围保护。 |
| test_user_quotas::test_explicit_task_limit_replaces_default_and_inherit_uses_latest_default | 测试职责交叉：交错建章改变全局planning，先前Task构建期安全失效，使FakeLLM Gate消费错位 | 先原node独立复现，再临时进程内观测终态：Task1 failed“场景规划读取期间已变化”，Task2/3 running且admitted=[2,3]，实际系统值3；并非慢或第三额度不生效。按批准仅预先建齐3章再按原顺序提交3任务。原5秒屏障、limited2→系统改3仍2→inherit3、缩额度不取消、三任务success全部保留。没有扩大超时、改成功为失败、重签旧载荷或修改调度／配额代码。 |

## 修改文件

产品仅三处：

- backend/app/services/task_service.py
- backend/app/services/context_builder.py
- backend/app/services/generation_service.py

原失败测试文件及新合同仅在这些文件内：

- backend/tests/test_ai_config_params.py
- backend/tests/test_agent_tools.py
- backend/tests/test_admin_task_adversarial.py
- backend/tests/test_revise.py
- backend/tests/test_styles.py
- backend/tests/test_user_quotas.py

`test_cast_context.py`原角色断言未改。Scene数据层／迁移、其余规划fixture、模型／API／依赖没有本轮更改。

## 实际证据

所有pytest在backend目录，显式使用`env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8`。

1. 原quota节点在未改测试／产品前单独稳定复现：**1 failed in 6.65s**；临时诊断进程仍复现上述真实终态，证明不是放大5秒超时的环境问题。诊断没有写磁盘源码。
2. 原七项精确节点（预算扩为两配置）修复后：**8 passed in 7.94s**。
3. 完整相关文件＋场景／安全回归：

   ```text
   uv run pytest tests/test_ai_config_params.py tests/test_agent_tools.py tests/test_admin_task_adversarial.py tests/test_cast_context.py tests/test_revise.py tests/test_styles.py tests/test_user_quotas.py tests/test_scene_generation.py tests/test_scene_context.py tests/test_scene_dispatch.py -q -x --tb=short
   194 passed in 155.12s
   ```

   包含原角色断言、13种坏历史JSON不修写、五种revise目标与日志、原quota运行中调整，以及场景手动覆盖／never／hidden／未来状态／staged和真实三SDK发送门禁。
4. 私有connection.json仅进程内注入专用URL，UUID schema隔离：

   ```text
   uv run pytest tests/test_scene_ai_postgresql.py -q -x --tb=short
   5 passed in 9.28s
   ```

   原主失败／取消／规划／时间线竞争和全部提案统一提交仍通过；没有输出凭据、改公共schema或真实LLM。
5. `uv run ruff check .`全backend通过；`git diff --check`通过。途中test_revise新增行W291已移除，最终门禁没有该问题。

## 冻结与后续

本轮七项已处理，没有已知遗留功能缺陷。没有跑第二次全量，没有把focused或PG当作浏览器／全任务通过；主会话统一全量和最终验收。若后续全量有新证据，先由主会话明确交回再改源码或测试。
