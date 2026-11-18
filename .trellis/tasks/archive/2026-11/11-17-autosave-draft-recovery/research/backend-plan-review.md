# Research: 后端保存规划复核

- Query: 复核收敛后 PRD、design.md、implement.md 中标题条件、HTTP／内部调用、权限、未知结果和测试范围是否存在不安全或不可执行的矛盾。
- Scope: internal；只读复核规划及此前已核实的源码，不修改产品代码或主规划。
- Date: 2026-11-17

## Findings

### 结论

后端标题方案可按现有分层实施，不需要迁移。复核曾发现 B1（未知请求与采用／舍弃转换）及 B2（标题值条件的 ABA）；最终主规划已同时修正，且包含 Agent effects 纯标题撤销、混合写入去重、前端本实例 revision 同步与必要回归。当前后端计划范围内未发现剩余阻塞矛盾。以下保留发现与修正理由，状态均为“规划已修正，实施待验证”。

### B1（规划已修正）：原请求仍可能迟到时，不能把“采用服务器版本”当作已完成

**初次复核定位**：当时 `design.md:173` 已正确指出“遗留 S，服务器仍 B”时原请求可能迟到；但当时 `design.md:184` 的“采用服务器版本”以及本地精确舍弃规则没有明确排除该状态。

**可复现时序**：

1. 原标题为 A，客户端发送受保护的 A→B 请求 S；网络超时，服务器请求尚未拿到写锁。
2. 另一个 GET 仍读到原基线 A；这不能证明 S 永远不会提交。
3. 作者此时选择“采用服务器版本 A”，界面安装 A 并把 S 丢弃／认为已完成。
4. 原请求之后拿到锁，A、原内容 revision、场景锚点仍匹配，于是合法提交 B。作者的采用选择被迟到写入推翻。

正文路径也存在同类时序。取消浏览器请求、结束页面或删除本地记录均不能撤销已经到服务端的事务。

**最小修正**：在未知结果状态机补一条明确规则：

- `unknown + remote == B` 时保留 S，不允许把采用服务端、舍弃此未决 attempt、AI／版本恢复／结构替换等改变编辑权威的动作标记为完成。
- 允许导出副本、继续本地编辑、重复读取核对，或作者选择按原条件重试同一 S。重试仍走现有条件写，不是取消原请求后无条件覆盖。
- 只有原请求已明确结束，或者已确认的写入**不可逆地推进修订号**使原请求条件失效后，才进入普通比较／选择状态。仅改变标题值不充分，见 B2。重试确认 S 已存在且修订号已推进后，若作者仍要 A，再作为新的显式选择提交。
- 显式离页仍可沿现有规则进行，但持久记录保留 `unknown`，不能称之为已撤销服务器保存。
- 不要求新增服务端取消／撤销接口或幂等表，只收紧客户端状态转换与提示。

**补充验证**：在 S4／A4 或 A12 增加受控请求案例：旧请求被阻塞在写锁前，GET 看到 B，尝试采用／舍弃不得清掉 S 或报告完成；随后释放旧请求，确认客户端核对其结果并保留尾部输入。标题和正文各覆盖一次。此用例不同于“响应已丢失但服务端早已提交”的已有计划。

**最终复核**：`design.md:185,189` 明确保留未知 S、禁用最终采用／舍弃／AI，允许本地编辑／副本／按原条件显式重试，并要求“确证未提交”或原修订号不可逆消耗；同值 no-op 不算消耗。`implement.md:24,92` 已列入该状态的测试。此项已在规划层关闭。

### B2（规划已修正）：标题-only 不推进 revision 会让迟到请求在 A→B→A 后再次生效

**复核时规划定位**：`design.md:9,135`、`implement.md:21` 明确保留标题-only 不推进内容修订号；这与未知请求可重试的目标存在冲突。此前调研对单纯标题值 CAS 的推荐也已在 `backend-save-contract.md` 修正。

**时序**：原请求 S 持有 `(expected_title=A, expected_content_revision=17)` 并延迟；重试先成功 A→B；用户随后把 B→A；若两次标题更新都不推进 revision，旧 S 的全部条件又成立，迟到时将 A 覆写为 B。稳定 scene 锚点无法解决同一目标内的这个问题。

**必要性质**：每次真正改变章标题必须消费不可逆的条件。最小方案为推进现有 `Chapter.content_revision`，而不是新增标题列或幂等表。

| 方案 | 本期评估 |
| --- | --- |
| 实际标题变化同事务推进现有 content_revision | 推荐；无 DDL，复用 `scene_service.touch_revision:124–133` 的原子条件 UPDATE；代价是更多保守场景冲突。 |
| 独立 title_revision | 同样必须覆盖所有标题写入／撤销，另需迁移与全链路字段维护；当前无必要。 |
| 保存收据／持久取消 | 可避免同 request ID 再执行，但需要表、身份／payload 校验、清理与未执行请求取消规则；超出最小修改。 |

**实施边界**：

1. 共用 `chapter_service.update_chapter` 的实际标题变更推进版本，覆盖两个人工 HTTP 入口及内部 Agent 执行；原标题／预期版本必须在全部写副作用前检查。
2. 标题-only `R→R+1`；同一次旧 PATCH 含正文时先检查原 R，正文路径已推进则不重复推进；同值 no-op不推进。标题自身仍不产生正文历史记录。
3. `agent/effects.py:221–222,1205–1208` 已排除并禁止回灌 content_revision，可以复用。但 `:1386–1407` 的恢复投影集合漏掉纯章标题 undo；为实际 `before.title != after.title` 的章推进，和正文／场景／重建章恢复集合去重，单事务回滚失败仍不得保留推进。
4. Agent 预览在保存点中回滚，不持久消耗 revision；实际执行、撤销各推进。旧 effects 继续排除修订号，不增加备份／effects 格式。
5. 标题成功后的新 revision 必须同步到同一编辑实例的场景／文档基线和目录版本，同时保留当前草稿、尾部输入及证据。不能把自己的标题写入误判为外部变化，或令下一次正文保存立即 409。
6. 标题本来就在 AI 快照签名中（`scene_ai_context.py:64–80`），且已推进规划版本；扩展内容版本带来保守冲突，但不削弱原保护。

**补充测试**：阻塞原 S→重试成功→反向改标题→释放原 S 必须 409；同场景锚点且原值回到 A 也不能绕过。另验 Agent 改名＋撤销后的 revision 单调、预览不持久推进、混合正文／标题仅消耗一次以及标题 ACK 后继续正文输入可正常保存。定向清单须追加现存 `test_agent_effects.py`、`test_scene_agent.py`。

**最终复核**：`design.md:137,141,151,153` 已采用实际标题变更经 `touch_revision` 推进现有 content_revision，覆盖共享 Agent 执行及 effects 纯标题撤销、同批正文去重、前端自身 ACK 只推进基线而保留 D／证据，并在 AI 前刷新规划版本。`implement.md:16,21,23–24,64,93,111` 已同步责任文件、ABA／undo 用例与现存测试文件。此项已在规划层关闭；没有引入 DDL 或幂等收据表。

### 已通过的计划级核对

- 新 `/editor-title` 四字段严格输入与旧 HTTP title 条件校验互相一致；`title=null`、status-only、style-only 保留原语义。内部 Agent 与 HTTP schema 分离，避免把 `manual=True` 误认为浏览器请求。
- 所有 guard 在原权限／写屏障内比较，并从 `audit.track` 排除；前置条件先于同值 no-op，没有允许相等新值绕过旧基线。
- `expected_title` 仍用于明确标题基线，实际标题变化的单调 revision 已按 B2 写入最终规划；正文已确认后，标题可用本次新内容版本而保留原标题基线。
- 稳定 scene 锚点由服务端在锁内校验归属／活动状态，覆盖章整数 ID 被重新使用；没有将 created_at 宣称为强身份。整体数据库回档／切换已明确不保证自动识别。
- 旧后端缺少新路由时失败可见，禁止 fallback 裸写；回滚前端保留后端 guard，旧前端＋旧后端不被包装成仍有新功能保护。
- 401／403／404 在已发出写请求后保留未知 S，正确覆盖 `chapter_service` 提交后 `manual_chapter_views` 还会重新校验权限的现状。
- 分字段、分步骤回执保护部分成功；完整文档比较不降为纯文本；原 CAS、历史合并和显式区块／标记证据仍保留。
- S1 测试包括标题-only 冲突、同值旧条件、缺条件、锚点失效、权限和真实竞争；已有定向文件当前存在，`test_chapter_title_save.py` 被明确标为拟新增。
- 后端最终门禁采用当前 README／CI 的 SQLite 四进程套件，未把已删除的 PostgreSQL 套件写为必须执行的现存命令。

## Related Sources

- `.trellis/tasks/archive/2026-11/11-17-autosave-draft-recovery/prd.md`
- `.trellis/tasks/archive/2026-11/11-17-autosave-draft-recovery/design.md`
- `.trellis/tasks/archive/2026-11/11-17-autosave-draft-recovery/implement.md`
- `.trellis/tasks/archive/2026-11/11-17-autosave-draft-recovery/research/backend-save-contract.md`
- `backend/app/services/chapter_service.py:111–155,339–435`
- `backend/app/api/v1/chapters.py:89–94`
- `backend/app/services/scene_service.py:115–163`
- `backend/app/services/agent/effects.py:221–222,1205–1208,1386–1407`
- `backend/app/services/scene_ai_context.py:64–80`
- `frontend/src/utils/writeResult.ts:3–5`

## Caveats / Not Found

- 这是设计审查，不是实现验收；本次未运行产品测试。
- B1／B2 已在规划中修正；实现验收仍须执行受控迟到请求、ABA、Agent 预览／撤销、混合写入去重及自身标题 ACK 后正文续存测试，文档一致不等于运行时已通过。其它证据重定位、浏览器持久化和 UI 细节由前端专项复核负责，本次不重复声称已验证。
