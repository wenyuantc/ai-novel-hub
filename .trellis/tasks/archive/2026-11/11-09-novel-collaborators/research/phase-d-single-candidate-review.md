# D 阶段单项设定候选与 AI 可见性复核

## 结论与范围

- 覆盖角色、世界观、职业、组织的单项人工创建、名称／标题或别名更新及删除。此前真实 TCP HTTP 红灯表明：新建竞争候选、修改名称或删除目标后，旧唯一命中已失效，但项目 `ai_visibility_revision` 与 `ai_history_epoch` 仍保持原值；世界观创建也未进入发送写门禁。
- 四类创建均按原 actor 的权限写屏障 → 发送写门禁 → 项目父行与锁后权限读取执行。候选快照在屏障内读取，复用卡片导入的大小写折叠、子串和部分重叠竞争判断；只在新候选与旧候选可能竞争时同事务递增 revision 和 epoch。新增无竞争候选不递增。
- 角色／世界观名称和别名、职业／组织名称更新先取得同一发送写门禁。已追踪的旧词消失或新增词可能遮蔽其他候选时递增 revision/epoch；原词仍保留且新词无竞争、相等更新均不递增。世界观旧分类切换追踪资格和角色旧注入开关继续按原合同处理，合并变化只递增一次。
- 删除四类条目可能移除先前的显式引用或唯一候选，故同事务使旧载荷和历史失效；若清理关联时已递增代次则不重复递增。仍调用现有字段引用、系列绑定和规划清理，真实 actor 投影与编辑权限不扩大。
- 授权验证处禁用 autoflush；取得授权后的实体写入、竞争判断或代次更新异常/取消时回滚，内部调用方即使捕获错误再提交也不能保存未更新代次的候选。已成功提交后投影阶段的失败不声称可撤销已提交结果。
- `new_character`、`new_world`、`new_career`、`new_org` 仍是只 flush 的组合原语；各调用方须自行遵守物理门禁与候选代次合同。向导替换发现的独立缺口已移交对应负责人，本切片不改向导、卡片、系列或 Agent 的服务。

## 验证

在 `backend/` 下，均使用隔离 SQLite 数据：

```sh
env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest -q tests/test_collaboration_single_candidate_visibility.py --tb=short
# 47 passed；运行中新增的 Agent 历史封存用例另跑，1 passed。

env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest -q tests/test_collaboration_story_access.py \
  tests/test_collaboration_story_dispatch.py tests/test_careers.py tests/test_organizations.py \
  tests/test_setting_ai_visibility.py tests/test_collaboration_bulk_creates.py --tb=short
# 65 passed；覆盖原编辑者、管理员、跨书、链接只加一次、批量创建和既有真实发送门禁。

uv run ruff check app/services/story_service.py app/services/career_service.py \
  app/services/organization_service.py tests/test_collaboration_single_candidate_visibility.py \
  tests/test_collaboration_single_candidate_visibility_postgresql.py
# All checks passed!
```

专项包括实际 TCP API 和本机模型 HTTP：竞争后旧 provider 零请求、非竞争旧载荷可用、真实发送持锁时写入限时失败；四类删除、旧词移除、跨项目隔离、8 项 viewer 拒绝无 autoflush、8 项故障后内部再次提交无残留，以及实际旧 Agent 历史可读但禁止续用。项目未配置独立 Python 类型检查器；修改文件已做编译检查。

主会话在隔离 PostgreSQL 环境错峰执行：

```sh
env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest -q tests/test_collaboration_single_candidate_visibility_postgresql.py --tb=short
# 11 passed in 100.76s
```

该组包含真实模型 HTTP 响应头前的共享 advisory 锁、四类创建及名称／别名更新等待、锁后旧载荷零发送、不同连接并发创建候选竞争，以及异常后另连接确认无残留。集成后仍需按最终工作区重跑。该组不代表线上模型服务已验证，也未部署。
