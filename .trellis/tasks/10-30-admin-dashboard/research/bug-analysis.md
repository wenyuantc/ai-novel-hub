# Bug Analysis：可复用整数ID不是任务执行身份

## 1. Root Cause Category

- **类别**：E 隐含假设、B 跨层契约、C 变更传播遗漏、D 测试覆盖缺口。
- **具体原因**：SQLite Task整数ID在删除后可复用。初版统计虽然引入独立UUID并保护finalize，但部分取消、刷新、清理和内存Hub/执行体仍按整数ID识别任务。
- **可重复证据**：两个真实session交错“旧请求持有Task→删掉无作品用户及其Task→新用户新Task复用整数ID→旧取消继续”。检查代理实测新任务变cancelled、旧已移除UUID被补终态、新UUID统计仍queued。不是假想输入或字符串测试。

## 2. Why Fixes Failed

1. 只防持久化统计主键碰撞和finalize：没有防其他DML与按id刷新，旧对象仍可影响新行。
2. 只给SQL增加UUID：旧输出flush、stream、Hub收尾和执行体取消仍能跨到新身份。
3. 只在一个await之前校验身份：校验后await prune/恢复/清理时仍可能换行或换Hub，必须把捕获身份带到实际动作处，而不是依赖早先检查。

## 3. Prevention Mechanisms

| 优先级 | 机制 | 具体动作 | 状态 |
| --- | --- | --- | --- |
| P0 | 结构性身份 | Task、统计、Hub、调度领取与运行记录共享不可复用statistics_key | 已实现 |
| P0 | 事务守卫 | 状态DML与刷新核对原UUID，批量删除使用标记阶段捕获的UUID集合 | 已实现 |
| P0 | 内存收尾 | close_hub/cancel_running/恢复/项目forget与旧flush/stream核对原UUID | 已实现 |
| P0 | 行为回归 | 17个真实双session身份交错回归，连现有任务/统计/回收站82项通过 | 已验证；最终全量另见check-report |
| P1 | 规范 | admin-dashboard七段契约及跨层思考指南增加执行身份/await后动作检查 | 已记录 |

## 4. Systematic Expansion

- 同一错误贯穿数据库对象、缓存、输出流和清理回调，不能把它看成单个UPDATE漏条件。
- 对资源的授权快照不等于资源执行身份；任何允许主键复用的长期异步资源都要分别核实这两层。
- 保持原调度和公开HTTP契约，不为修复引入新的调度框架；其他资源的身份体系应在各自范围内另行评估，不在本任务顺手重写。
- 覆盖正常任务ID互不相同的用例不能证明复用隔离；测试必须构造相同整数ID、不同UUID和真实await交错。

## 5. Knowledge Capture

- [x] `.trellis/spec/backend/admin-dashboard.md`：统一统计/执行身份、原UUID刷新、捕获集合删除、Hub/输出保护。
- [x] `.trellis/spec/guides/cross-layer-thinking-guide.md`：跨事务/异步对象不可复用身份检查。
- [x] `test_task_statistics_identity.py`：真实复现与竞争防回归。
- [ ] 规范随本任务文档提交，遵守最终提交计划的一次性用户确认；本项目无Trellis源码模板目录，不创建无关模板副本。

# Bug Analysis：移动图表文字随固定画布缩小

## 1. Root Cause Category

E 隐含假设与D 测试覆盖缺口：桌面固定600px画布在约300px容器整体缩放，轴字体也被缩小。桌面截图和坐标纯函数测试未暴露375px实际文字尺寸。

## 2. Why Fixes Failed

仅让SVG宽度100%或提供数据表，并不能保证图表刻度本身可读。问题在画布坐标与实际容器脱节，不是增加一个overflow规则即可解决。

## 3. Prevention Mechanisms

复用useElementWidth，以实测宽度计算viewBox和坐标，保持210px图高及12px轴字体；补窄/宽容器几何回归；真实375px测量所有SVG文字包围盒不低于9px并目视复验。

## 4. Systematic Expansion

折线与任务柱图同时修复，不为本页面建立全站图表抽象。以后新增SVG需把文字大小与布局缩放共同验证，而非只证明容器没有横向溢出。

## 5. Knowledge Capture

已记录于管理员首页跨层契约和组件指南；截图与复验见 `browser-verification.md`，响应式回归已纳入前端测试。

# Bug Analysis：取消中的RETURNING游标持有SQLite写锁

## 1. Root Cause Category

E 隐含假设、B 数据库适配层/取消契约、D 竞争覆盖缺口。最终全量曾出现1208通过/1失败：章节生成取消与完成竞争后卡在running。不能用立即单例复跑通过把它归为无害波动。

两条独立证据一致：取消请求连接本身 `in_transaction=False`；真正持锁的是生成结果保存中被取消的 `INSERT generation_logs ... RETURNING`。SQLAlchemy处理CancelledError并关闭连接后，仍活跃的返回游标可继续持锁，后续独立回退/终态事务都被阻塞。Python sqlite3原生复现与真实SQLAlchemy/aiosqlite UDF Gate复现均证明释放游标后下一次写立即成功。

## 2. Why Fixes Failed

只检查HTTP请求事务、立即重跑失败用例或放宽锁超时，均不能证明活游标已经释放；只修Task终态统计也无法解除之前结果保存留下的锁。此前小批回归未稳定命中SQL进行中的取消窗口。

## 3. Prevention Mechanisms

用有界结果保存协程保护正文、版本、生成日志原子事务，不取消正在执行的SQL；提交前收到取消先完整回滚，提交已开始则等待确定保存并以成功结果收尾。执行体收尾前释放请求事务；不添加全局驱动hook、不更改WAL或增加重试。增加RETURNING和已commit两种真实屏障回归，检查底层SQL不被打断、数据全成全败、唯一统计终态及随后独立写成功。

## 4. Systematic Expansion

取消异常不等于数据库资源已清理。应追踪实际锁持有者及游标生命周期，不能把看见的第一个HTTP会话当作根因。相同驱动机制也提醒后续后台任务写入审查有界事务的取消安全，但本次不重构全站会话或调度框架。

## 5. Knowledge Capture

已记录于 `backend/admin-dashboard.md` 的取消/原子保存契约和真实屏障回归。最终门禁必须在修复后重新全量运行，结果以 `check-report.md` 为准，不能沿用先前1208/1或单例通过。
