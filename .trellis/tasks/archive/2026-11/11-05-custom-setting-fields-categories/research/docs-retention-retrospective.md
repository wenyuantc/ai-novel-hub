# Bug Analysis: 文档路由闭包保留已关闭应用

## 1. Root Cause Category

- Category：E（隐式假设）＋D（测试覆盖缺口）。原实现假设关闭客户端、任务引擎和数据库后，应用内部引用环即可被GC回收，没有考虑框架对处理器的全局强引用。
- 已证实的引用链：`fastapi.dependencies.models._is_async_gen_callable_cached` → `_CallIdentity.call` → `install_docs.<locals>.openapi_schema` → closure cell → FastAPI app。独立8例无流包装诊断中，全部fixture结束且funcargs清空后，8/8 app、TaskEngine和DB engine仍存活；两例最小反向图确认上述全局根。
- 连续运行前缀中，最大gen2 GC为9.907s；另一次4.054s GC与流内4.030s loop lag重合。原两次完整测试的10秒超时没有相同插桩，不能将这组关联冒充逐次因果证明。修复后的完整门禁仍须单独验证。

## 2. Why Fixes Failed

1. 两个原失败节点分别单独重跑均通过，只排除独立稳定复现；它们无法覆盖完整进程中的对象累积。
2. 关闭临时浏览器和预览服务后，完整测试仍在另一节点超时，因此不能把测试外负载当作既定根因。
3. 50例整模块与1246节点前缀都通过，却保留了GC/loop时序证据；下一步转向弱引用和全局根，而非继续盲目全量。期间没有调超时、清框架缓存或删除断言。

## 3. Prevention Mechanisms

| 优先级 | 机制 | 具体行动 | 状态 |
| --- | --- | --- | --- |
| P0 | 结构 | 文档handler改为模块级，经Request.app使用每实例缓存，安装函数只注册路由 | DONE |
| P0 | 回归 | 正式两实例文档／root_path／名称／弱引用回收测试：旧实现1 failed/1.67s，修复1 passed/0.53s | DONE |
| P0 | 质量门禁 | 既有open_api与两相关模块72通过；最终完整后端2146通过／1旧可选PG跳过，真实退出0，无诊断插件 | DONE |
| P1 | 规范 | 后端质量规范补应用工厂／handler寿命契约，跨层指南补回调缓存边界检查 | DONE |

## 4. Systematic Expansion

- 相似风险：模块缓存持有捕获短寿命资源的闭包或绑定方法。内部app↔engine引用环、模块级router的直接include context都不能单凭形状判为泄漏，须追到全局根。
- 设计：路由handler通过Request取得当前实例；缓存放实例state，返回JSON时按请求添加root_path，不把请求前缀写回共享schema。
- 过程：原失败保留日志→独立复现→相关模块→带轻量时序日志的原顺序有界前缀→无包装弱引用诊断→全局根→永久RED/GREEN→完整门禁。重采样只放10秒流窗口外，并单独记录采样耗时。

## 5. Knowledge Capture

- [x] 更新`.trellis/spec/backend/quality-guidelines.md`。
- [x] 更新`.trellis/spec/guides/cross-layer-thinking-guide.md`。
- [x] 保存原始日志与独立检查范围，父implementation-log和field-backend-check报告维护最终结果。
- [x] 核对本应用仓库不存在`src/templates/markdown/spec/`；不创建无关Trellis模板副本。
- [ ] 通过最后完整门禁后，按父任务Phase3.4统一提交计划处理；当前不提前提交或归档。
