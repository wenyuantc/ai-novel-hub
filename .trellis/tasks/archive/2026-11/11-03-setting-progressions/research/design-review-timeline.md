# Research: 时间线设计的有界一致性核对

- Query: 只读本任务 design.md，基于已核实源码，检查存在性、区间约束、分析替代／撤销、旧 AnalysisChange 和引用约束。
- Scope: internal；最多 5 项，不扩展产品范围，不修改 design/prd/产品代码。
- Date: 2026-11-03（Asia/Shanghai）
- 审核基准：用户已选择 M1；不再要求自动重放旧历史。

## Findings

### 已确认可落地的部分

- 关系使用稳定人物对＋隐藏 Relationship 载体，能够保留原 RelationshipHistory 的 CASCADE 外键审计；职业／成员以稳定二元组为主体，当前挂接行删除不会清掉新时间线。`design.md:87`、`:89` 与现有 FK 形状相容；不需要改成大型事件框架。
- 章末独立 boundary、只读 DTO、按章重算 latest、停用原事件而非追加反向业务事件，都能解决已确认的乱序回写问题。
- M1 保留当前值并标记未校准是诚实且可执行的迁移策略。

### 必须修复／明确后再实施

#### 1. 旧分析兼容缺少“基础值已被作者重新确认”的保护

- **设计位置**：`design.md:114` 仅以“对应字段无新时间线变化＋旧冲突检查通过”允许旧确认／撤销；`:139` 又允许作者修改／确认 baseline。
- **具体反例**：M1 基线为 A；旧分析记录 after=A、before=B。作者在新基础面板校准为 C，再改回 A，并确认基线。这期间没有 progression。旧 `revert_change` 的值比较仍通过，于是旧 before=B 覆盖了作者已校准的 A。该行为违背 M1 的人工校准优先，也可能把 confirmed 基础再次变成 legacy_unverified。
- **源码依据**：`backend/app/services/writeback_service.py:489` 仅比较当前值与 after；职业、关系同样按值比较（`:495`、`:517`），没有可识别 ABA 的历史修订关联。`story_service.py:127` 的原字段编辑也无额外历史可依赖。
- **最小修正**：为兼容旧分析设置明确的“迁移后仍未人工改动／校准”的资格；基础确认或人工编辑后不可恢复该资格，即使值改回相同也不恢复。可用主体／字段的 sticky 标记或迁移以来的人工基线 revision，不能只看 baseline_status 或哈希。旧确认／撤销仍走统一基线写入口，不直接修改当前 ORM。
- **结构化特例**：旧 relationship_create、原 before=None 的 membership 创建等撤销涉及整个存在性，不是某个 field_key；必须检查该主体任何新事件／人工校准，禁止用仅 exists 字段无事件作为许可。旧关系撤销不能继续直接 `db.delete(rel)` 绕过隐藏载体规则。
- **验证**：校准 A→C→A 后旧撤销 409；旧关系创建的撤销不得删除带新 description/identity 事件的主体及审计。

#### 2. 职业 kind 修改也是历史区间约束的写入口

- **设计位置**：`design.md:52` 规定 role 由职业定义决定；`:89` 要校验所有受影响故事区间，但只明确变化写入和章节顺序，未覆盖职业定义更新。
- **具体反例**：角色在第 1–10 章有主职业 A 和副职业 B；B 在第 10 章末卸下，因此当前没有 B 的 CharacterCareer 行。此时把职业定义 B.kind 从 sub 改为 main。若沿用现有实现，因 B 没有当前持有人而直接成功，但回溯第 5 章会出现两个主职业。
- **源码依据**：`backend/app/services/career_service.py:287` 修改 kind 后只枚举当前 CharacterCareer 持有人（`:291`），每个持有人的 `_check_limit` 也只统计当前行（`:340`）。新设计的角色归属历史不会自动进入这次枚举。
- **最小修正**：职业 kind 变更必须通过时间线协调器枚举所有引用该 career 的主体，包括 latest.exists=false 的主体；在变更后的统一 kind 下校验受影响历史区间，然后同事务更新定义、相关快照／latest 投影。冲突整体回滚。既然职业定义不时间化，本期不需要另加 kind 事件。
- **验证**：已卸下的历史副职业改为 main，如果造成旧区间超过主职业上限必须 409 且定义和投影均不变化；无冲突时全部回溯位置使用一致 kind。

#### 3. 自动事件替代必须先完成来源转换，再决定“值相同不写”

- **设计位置**：`design.md:106` 使用目标位置 before==after 判断，`:111` 要成功重跑替代自己的旧事件，但未明确 no-op 分支和替代事务的先后。
- **具体反例**：章初 A，旧自动章末事件 B。新一轮明确给出 after=A 时，若相对于排除旧事件的章初值判无变化便直接返回，旧 B 永远留存；若新结果仍是 B 而相对于含旧事件的章末值判无变化，则旧来源仍活跃，新分析没有接管，之后点击旧分析撤销会撤销本轮仍认可的事实。
- **源码依据**：`backend/app/services/writeback_service.py:169` 的第一个分支是 before==after 立即 return，连 AnalysisChange 都不创建。新实现若只替换 before 的来源并沿用这条控制流，会跳过设计所需的 supersede／来源绑定。
- **最小修正**：定义独立的自动写回原语：定位旧“未受人工保护”的同章／主体／字段事件组 → 以排除该组的投影计算新效果 → 验证新结果与区间约束 → 在一个 SAVEPOINT 中停用旧组并记录新来源。新值等于排除旧组后的值时可不保留冗余有效事件，但仍要完成旧组停用／来源处理；值等于旧组效果时也必须明确转移来源或声明稳定复用协议，不能无条件 return。字段失败时旧组保持不变。
- **验证**：A→旧 B 后重跑明确 A，结果为 A；重跑同 B 后旧分析按钮不得撤销新一轮采用的事件；新字段结果校验失败不移除旧有效组。

### 可在实施时细化（无需扩大范围）

#### 4. 关系 identity 的端点必须等于稳定主体的人物对

- **设计位置**：`design.md:51` 允许替换 `{a_id,b_id,type_id}`；目前只写同项目引用校验，未把身份组和 timeline 的 pair 两端相等写成约束。
- **风险**：同项目第三人 C 是合法外键，但不应能把 A/B timeline 的身份改为 A/C；否则 carrier 的 pair、timeline 的 pair 与历史读结果分裂，存在性／唯一性检查各有不同身份。
- **现有契约**：`backend/app/services/relationship_service.py:508` 只允许在关系原两端之间交换方向。
- **落实**：identity 校验要求 `a_id != b_id` 且 `{a_id,b_id} == {timeline.pair_low,timeline.pair_high}`；类型仍可用全局预置或本项目类型。此约束同时用于 baseline、事件、备份校验与 materialize。

#### 5. 人工保护状态必须与来源 provenance 分开表示

- **设计位置**：`design.md:18`、`:111`、`:112` 区分自动结果、作者编辑和作者确认，并承诺后两者不会被自动重跑覆盖。事件数据形状目前只有 source、revision、active／替代状态，尚未说明如何持久化这个区别。
- **风险**：作者编辑 source=analysis 的一条事件后，如果仍只按 source 分类，重跑会再次把它当自动组替代；若直接把 source 改成 manual 又会丢掉原分析关联／撤销含义。单看 revision>1 也会把自动替代的版本递增误当作者修改。
- **落实**：定义独立的 ownership/protection 状态（例如 automatic、confirmed、manual），来源 ID 保留不变。人工内容编辑／确认在同一事务改变保护状态；排序、自动替代候选与撤销冲突均读这一个状态。具体命名可由实现决定，无需新增产品操作。
- **验证**：手工编辑自动事件后重跑不覆盖；作者确认的生命／新关系事件不被自动组替代；自动重试不误晋升为人工保护。

## Related Specs

- `.trellis/spec/backend/story-entities.md`：现有关系方向、职业 kind、分析回写和撤销契约。
- `.trellis/spec/backend/database-guidelines.md`：父项目写屏障及 SAVEPOINT 原子性。
- 本任务 `research/timeline-and-legacy.md`：以上源码证据和存量边界的完整研究。

## External References

无外部依赖或文档引用；这是对本地设计与已核实源码的有限一致性核对。

## Caveats / Not Found

- 本轮只读取 design.md，没有再扩大代码搜索，没有运行测试。
- 共提出 5 项；前三项应在实施契约中明确，后两项可在实现 schema／service 时细化并补对应测试。
- 未发现隐藏关系载体／稳定职业与成员主体方案本身需要推倒重做的问题。
