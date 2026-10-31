# Bug Analysis：异步取消的受理、执行身份与提交事实

## 1. Root Cause Category

- **B 跨层契约**：HTTP 202/审计成功描述受理，真实取消跨独立执行体及清理事务；原普通helper内部提交不能被外层审计“包成原子”。
- **C 变更传播遗漏**：提交确认丢失最初只在finalize调用者回读，drain/recover共用的状态提交边界仍可能漏登记completed/回放Hub。
- **E 隐式假设**：UUID稳定不等于业务执行轮次稳定；同Task暂停后继续仍同UUID，completed/winners若只按UUID会影响新轮。commit抛错也不证明未提交。
- **D 覆盖缺口**：只测正常取消/202不能覆盖真实UPDATE前插入受理、实际commit后异常、SQLite主键复用、601个缓存键等边界。

## 2. Why Fixes Failed / Review Findings

1. 研究阶段已否决“旧取消后补审计”“终态释放资源后重试旧abort”，避免把事务与资源问题拖成上线缺陷。
2. 实施检查发现仅修finalize的commit确认丢失不完整：drain直接调用complete_state同样可能实际已提交后抛错；应在共享提交边界统一读取真实事实，恢复本轮completed，不能重放旧abort误伤新资源。
3. completed最初按UUID，late paused获胜后继续的新轮可能误跳过abort修复；改为UUID+可信queued_at本轮，恢复入队时间严格递增，并扫描回收已移除/旧轮缓存。
4. 缓存生命周期扫描的IN参数会随现存完成量增长；601-key实测证明需与pending核心扫描分离并≤500分批，而不是用增大数据库限额掩盖。
5. 真实浏览器发现成功旧error被误显示失败、窄屏页头拥挤和桌面操作列不可达；错误摘要改为不读取Task.error，移动页头上下排，操作列固定右侧。

## 3. Prevention Mechanisms

| 优先级 | 机制 | 具体行动 | 状态 |
| --- | --- | --- | --- |
| P0 | 事务与语义 | 持久意图+受理审计同事务，202不冒称取消；共享状态提交事实回读 | 已落地，最终1292项后端全量通过 |
| P0 | 条件写 | UUID/原状态/观察marker真实CAS，owner与finalize/recover/drain各入口覆盖 | 已落地并有事件屏障回归 |
| P0 | 资源代次 | 先清理后终态；本轮queued_at区分同Task新执行，completed/winner随生命周期回收 | 已落地并有真实旧abort/新资源回归 |
| P1 | 集成测试 | 真commit后异常、HTTP断开、RETURNING游标、ID复用、601-key分批 | checker新增回归实测通过 |
| P1 | 隐私投影 | 不选Task.title/error/input/output/checkpoint，固定标签/安全阶段与错误 | API与浏览器注入标记检查通过 |
| P1 | 知识固化 | 新7段式任务监控规范、既有审计/生命周期/状态/类型索引与跨层检查清单 | 已写入本任务spec变更 |

## 4. Systematic Expansion

- **类似入口**：普通cancel/resume/finish、拆书skip、启动recover、独立drain、项目stop/purge、clear/prune/delete、无作品删号；逐个核实后以同任务范围的屏障修复，不扩大为新调度系统。
- **架构改进**：把真实状态/统计/清意图的条件提交与提交事实处理放在共享complete_state，而不是每个调用者补一次相似catch。仍保留既有普通行为边界和单进程模型。
- **流程改进**：研究阶段先证明资源不变量；实现与独立check分开所有权；发现新修复后停止跑已导入旧代码的全量，最终冻结后只跑一份完整测试。未完成/被中止/旧断言失败均单独记录，不用局部绿色冒充门禁。
- **验收限制**：任意强杀后正文保存与Task finalize间的既有边界不承诺精确赢家重建；PostgreSQL实际运行验证缺环境，不能用SQLite替代。

## 5. Knowledge Capture

- [x] `.trellis/spec/backend/admin-task-monitoring.md`：API/受理/资源/轮次/确认丢失/分批/前端/回归完整契约。
- [x] admin-audit、admin-dashboard、auth-admin、admin-projects、recycle-bin、database/error规范与前端索引/组件/状态/类型同步。
- [x] cross-layer-thinking-guide增加简短检查项并指向专门契约，不复制详细实现。
- [x] 本仓库不是Trellis CLI源码，没有`src/templates/markdown/spec`，模板同步不适用，不创建虚构模板树。
- [x] 规范与产品同一任务提交批次；最终门禁通过、用户已于2026-11-01明确授权提交/收尾/合并main推送，按3.4及finish-work顺序执行。
