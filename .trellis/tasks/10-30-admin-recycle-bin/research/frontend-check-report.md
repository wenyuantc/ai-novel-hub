# 前端独立检查报告

日期：2026-10-31，Asia/Shanghai。检查角色：`trellis-check`；范围仅为本任务 `frontend/**` 全量变更，未修改后端、规划和规范文件，未提交或推送。

## 已核查范围

- 对照 PRD R1–R8 / AC1–AC8、design 前端/API契约、implement、check manifest 和实施报告检查本轮全量前端差异，而非仅最新提交片段。
- 管理员路由和导航：回收站使用 RequireAdmin + 懒加载；桌面、账户菜单、移动导航与页名一致；普通用户不渲染入口。
- 传输边界：后端字段/可空性、原始微秒 deleted_at 回传、POST确认操作、true布尔值、Cookie/401/结构化错误均与现有API客户端一致。
- 交互与状态：到期判断、逐字名称确认、忙碌防重复、旧弹窗/身份响应失效、失败不提前移除、分页回退、目标级Project/Task失效、明确404撤下不可访问项目且保留暂时网络失败下的旧项目。
- 原删除入口：书架复用统一名称确认，工作区和管理目录说明30天与取消任务规则；删号计数包含回收站，仍可禁用，并处理并发409安全计数。
- 审计：restore/purge/expire标签、系统空actor、生命周期摘要保持后端Literal镜像，没有正文/凭据的额外读取。
- 窄屏静态布局与主题：回收站为可断行移动卡片、桌面内部横滚；无新增写死主题色。真实浏览器交互、截图和375px实际溢出测量由主会话单独负责。

## Findings（已修复）

### 合法Unicode作品名无法完成purge名称确认

- 文件：`frontend/src/components/project/RecycleProjectModal.tsx`。
- 原因：确认输入写有 `maxLength={200}`。原生输入以UTF-16 code unit计数，后端 `title` 则允许200个Unicode字符；由API或备份导入的200个emoji标题合法，但占400个UTF-16 unit，用户不能在界面输入完整作品名，因而永远无法满足严格确认。
- 修复：移除这一冗余输入长度上限，仍由 `canPurgeProject` 严格逐字比较完整标题，且服务器再次校验名称和长度；没有改变接口或放宽危险操作确认。
- 回归：`frontend/src/components/project/recycleBinUI.test.ts` 新增200个emoji合法标题场景，确认完整值保留且没有200个UTF-16 unit截断限制。

## Findings（未修复）

没有发现其他需要修改的前端代码问题。以下是明确的环境/验收边界，不伪报通过：

- 原始 `npm run build` 的 `.tmp/*.tsbuildinfo` 和 `frontend/dist` 仍受既有root所有权/EACCES阻止，沿用主会话已经确认的可写临时产物方案；未使用sudo或改依赖/配置来掩盖问题。
- 后端全范围、真实PostgreSQL并发与浏览器实际交互不属于本轮前端检查的独立结论，需由主会话后续整体检查与验收覆盖。
- ProjectStore明确404失效、回收站和审计系统actor的规范同步由主会话负责，本检查未抢占规范文件所有权。

## Verification

工作目录：`/Users/wenyuan/IdeaProjects/my/ai-novel-hub/frontend`。

1. 定向检查：

   `HOME=/private/tmp/ainovel-recycle-test-home npm test -- --config /private/tmp/ainovel-recycle-frontend.config.mts --configLoader runner --fsModuleCachePath /private/tmp/ainovel-recycle-module-cache src/components/project/recycleBinUI.test.ts src/utils/recycleBin.test.ts src/api/recycleBin.test.ts src/stores/project.test.ts src/stores/tasks.test.ts`

   结果：5文件、30测试通过，exit 0。

2. `npm run lint`：通过，exit 0。

3. 等价TypeScript检查：

   - `node node_modules/typescript/bin/tsc -p tsconfig.app.json --incremental --tsBuildInfoFile /private/tmp/ainovel-recycle-check-tsconfig.app.tsbuildinfo`
   - `node node_modules/typescript/bin/tsc -p tsconfig.node.json --incremental --tsBuildInfoFile /private/tmp/ainovel-recycle-check-tsconfig.node.tsbuildinfo`

   两者均通过，exit 0。

4. 全量前端测试：

   `HOME=/private/tmp/ainovel-recycle-test-home npm test -- --config /private/tmp/ainovel-recycle-frontend.config.mts --configLoader runner --fsModuleCachePath /private/tmp/ainovel-recycle-module-cache`

   结果：**67文件、516测试通过**，exit 0；仅有既有Node `--localstorage-file` 警告。

5. 等价生产打包：

   `node node_modules/vite/bin/vite.js build --config /private/tmp/ainovel-recycle-frontend.config.mts --configLoader runner --outDir /private/tmp/ainovel-recycle-frontend-dist`

   结果：通过，exit 0。临时config仍只导入原配置并重定向cacheDir；原root-owned生成目录保持原状。回收站独立chunk `AdminRecycleBinPage-BvSNxH91.js` 9.59kB；入口 `index-C4LkJC9r.js` 182.71kB；最大vendor-antd 1,147.01kB，无1500kB超限。临时outDir保留旧hash资产的提示不是构建失败。

6. `git diff --check -- frontend`：通过。

已重建 `/private/tmp/ainovel-recycle-frontend-dist`，主会话应在继续浏览器验收前reload以使用修复后的构建。

## 最终全范围检查后的前端补充

后端Key项目ID投影改为隐藏回收站作品后，跨层继续追踪发现编辑Key原来整表提交scope：仅改名也会撤销混合授权中不可见的grant，全不可见时还会阻止仅改名/有效期。主会话已批准最小兼容修复，不变更后端API。

- `ApiKeyFormModal` / `utils/apiKeys`：编辑按all_projects与ID集合比较可见范围，未变省略scope并允许空投影的元数据更新；创建/明确更改范围仍执行原验证及完整替换；增加保留原授权/替换范围的说明。
- `utils/apiKeys.test.ts`补4项：只含回收站/混合授权仅改名、ID顺序/重复不算修改、显式变更及创建/编辑空范围拒绝。
- 最终重跑lint、app/node等价TS、完整Vitest（**67文件520测试**）、等价Vite全部通过；`git diff --check`通过。
- 最终dist：入口 `index-h2zMVLbJ.js`，回收站 `AdminRecycleBinPage-ct1m770m.js`（9.59kB），Key页 `ApiKeysPage-Da5vkxWL.js`（12.37kB）；主会话已通知新hash，原root-owned权限限制未改变。
- 最终全范围证据与后端发现见 `final-check-report.md`；本报告第一轮的516测试是修复时点的真实记录，不作最新最终计数。
