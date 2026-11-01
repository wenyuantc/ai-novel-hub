# 独立全范围检查

2026-11-01。审查基线97193269c4bfcf896163e49236866da7a9f83ca9，覆盖backend/frontend全部任务变更及新未跟踪产品文件，排除用户已有配置/忽略文件/两个source-command skill改动。

## 已修复并冻结

1. 设置controller在StrictMode setup→cleanup→setup时busy不清可能永久Skeleton：取消后清busy并补回归。
2. 根AntApp持有的本页confirm没有随离页/denied销毁：新增useSettingsConfirmation，仅销毁自己句柄，保护迟到afterClose身份，补4项生命周期/接线测试。未用destroyAll。
3. 开放API限额原允许超大整数持久保存，后续SQLite认证绑定溢出500：独立HTTP重现后，统一管理和部署技术上限2_147_483_647，持久加载/null恢复沿同一验证；不收窄并发上限。补超界整批422、fail-fast不回显、最大值和恢复后的真实读取。
4. 新并发测试错误把FakeLLM gate顺序当Task ID：按真实_on_done身份和完成之后的dispatch屏障验证；未改产品/timeout/skip。

## 已实际通过

- 最终后端本任务40项定向全部通过（33.11s）、全量Ruff及diff检查通过。
- 最终前端81文件634项、lint、app/node两份tsc、等价生产构建通过。
- 新七段system-settings规范、PRD/design/README与代码一致；无剩余确定产品缺陷。
- 主会话真实浏览器另见browser-verification.md；独立check未操作该浏览器/临时库。

## 完整门禁边界

后端最终full由implement_backend持有原始日志，完成后以verification.md为准。首轮full的既有writeback 10秒超时已隔离单项1 passed、整模块9 passed，未改代码/timeout。第二轮起跑后仅本任务测试的Gate身份屏障改变，backend/app及alembic产品哈希不变；最终若第二轮通过，必须同时附修正后40项通过及快照事实，不能声称collection已使用新测试定义。PostgreSQL未实测；前端标准build的既有缓存权限问题使用临时路径等价验证，无权限/配置放宽。
