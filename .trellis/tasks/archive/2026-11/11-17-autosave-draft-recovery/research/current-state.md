# 自动保存与异常草稿恢复：现状调查

调查日期：2026-11-17。创建任务时仓库位于 `main`，HEAD 为 `b329323`，原工作区干净。本记录来自源码检查，未执行产品测试；不把 TODO 中的旧描述或历史测试结果视为当前实现证据。

## 编辑与保存入口

- `frontend/src/pages/project/ChapterEditorPage.tsx:106–140`：按账号／授权身份和章节重建加载器；章节请求失败时显示“无法打开章节”，无本地草稿入口。
- `frontend/src/pages/project/ChapterEditorPage.tsx:187–201,305–313`：章节标题与场景控制器分开管理；正文来源是场景草稿，保存态与 AI 写作锁参与编辑状态。
- `frontend/src/pages/project/ChapterEditorPage.tsx:359–377`：先保存场景，再更新标题或重取章节，成功响应通过 `applyServerChapter` 更新标题；标题请求未携带预期版本。自动保存需要避免迟到的标题响应覆盖保存期间的新输入。
- `frontend/src/pages/project/ChapterEditorPage.tsx:379–445`：保存按钮、快捷键、刷新／离开拦截，以及 AI 操作前的保存已存在。尚无输入停顿触发的持续自动保存。

## 场景与富文档

- `frontend/src/components/scenes/useSceneEditor.ts:11–43`：控制器按账号、会话代次、作品及章节隔离；API 包括场景和富文档两条读写路径。
- `frontend/src/components/scenes/sceneDraftController.ts:79–105,141–152`：草稿和映射证据存于控制器内存；保存期间 `change` 不接受新修改。不能仅给现有手动保存外包一个定时器便宣称支持不间断写作。
- `frontend/src/components/scenes/sceneDraftController.ts:245–260`：检查场景目录、场景及富文档快照版本一致；保留脏草稿时不会直接换成服务端新内容。
- `frontend/src/components/scenes/sceneDraftController.ts:268–311`：组合输入、保存中、冲突或外部变更时不提交；普通保存携带 `expected_content_revision`；409 读取最新版本，网络结果未知时要求核对。
- `frontend/src/components/scenes/sceneDraftController.ts:316–363`：富文档保存依赖标记／区块的映射与显式增删／恢复证据；文档提交后可能还需保存场景标题与摘要。部分成功、第二次请求失败必须能够恢复并核对，不能把整个流程当作单次原子保存。
- `frontend/src/components/scenes/sceneDraftController.ts:376–404`：冲突比较及显式接纳新基线已存在；完整文档结构变化时拒绝用旧结构覆盖。持久化草稿需保存恢复该语义所必需的基线及证据。

## 服务端标题与历史

- `backend/app/schemas/chapter.py:20–29`：章更新请求已有可选 `expected_content_revision`。
- `backend/app/services/chapter_service.py:339–365,417–434`：人工写入有权限／锁门禁；标题更新直接赋值，预期内容版本仅传给正文替换路径；标题变更推进规划版本。不能认为现有内容版本参数已保护标题。
- `backend/app/services/chapter_version_service.py:191–236`：版本恢复通过场景服务校验并恢复结构，随后记录恢复版本；新自动保存必须与该链路协调。
- `backend/app/services/version_service.py:23,143–167`：手动版本按 10 分钟窗口合并，并保护清空边界；高频保存不应复制一套独立历史机制。

## 本地存储与离线启动

- 检查 `frontend/src`、`frontend/public`、`frontend/vite.config.ts` 和 `frontend/package.json` 中的 `localStorage`、`sessionStorage`、`indexedDB`、`serviceWorker`、`service-worker`、`workbox`、`registerSW`、`navigator.onLine` 使用。
- 匹配到的持久化主要是阅读／主题／书架等偏好和小说模板创建意图；未发现正文草稿持久化，也未发现 Service Worker 或离线启动配置。`frontend/public` 当前只有 `favicon.svg`。
- `frontend/src/stores/auth.ts:73–85`：初始化请求 `/me`，失败后设为匿名；`frontend/src/pages/project/ChapterEditorPage.tsx:116–140`：章节加载依赖服务端。这些证据表明完整离线重开超出现有加载链路，不能仅通过保存草稿实现。
- 待用户决定离线重开的范围，再研究对应技术方案；当前没有选定浏览器存储技术或更改认证行为。

## 后续设计必须遵守的现有契约

- `.trellis/spec/backend/scenes.md:99–110,143–167`：单一正文权威、历史边界、AI 快照和草稿冲突语义。
- `.trellis/spec/backend/rich-documents.md:29–39`：富格式／区块／标记身份、显式编辑证据、组合输入和会话隔离。
- 实施前继续查阅前后端质量、状态管理、类型安全及权限规范，并以当前代码核实；本次尚未进入技术设计、上下文配置或实施阶段。
