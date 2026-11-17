# 第 13 项现状与规划输入

核对日期：2026-11-16。起始分支 `main`，提交 `2a8b499`，开始时工作区干净。本记录是规划证据，不是实现或测试完成证明。

## 需求来源

`TODOS.md:156–164` 明确要求笔名组织和筛选、个人小说模板保存复用、角色和地点等设定缩略图。

已读取待办引用的 [Novelcrafter 功能总览](https://www.novelcrafter.com/features)：页面分别列出这三项能力，说明笔名用于身份分组、个人模板用于启动新作品、缩略图用于辨认角色和地点。页面未明确模板保存哪些数据，不能据此替用户选择本项目的模板内容边界。

## 当前作品入口

- `frontend/src/pages/ProjectListPage.tsx:51` 通过 `filterAndSort` 在当前可见作品上筛选；`:55`、`:70` 显示现有条件为搜索和作品状态。
- `frontend/src/pages/ProjectListPage.tsx:101` 使用 `CreateProjectModal`，成功后进入新书向导。
- `frontend/src/components/shelf/CreateProjectModal.tsx:24` 当前只提交书名、题材、一句话设定、默认风格及立项信息；`:36` 使用个人写作风格选择器。
- `backend/app/services/project_service.py:421` 的列表读取当前用户可见作品；`:525` 创建入口检查新建额度和风格可分配性。
- `backend/app/services/project_service.py:554` 更新接口使用人工内容编辑权限，并对他人作品的私有风格和 AI 设置作额外限制。
- `backend/app/models/entities.py:236` 的作品模型无笔名字段；项目已具备协作者与系列关系，笔名管理需要明确与这两类关系的交互。

## 备份与模板的差异

- `backend/app/services/project_transfer/exporter.py` 当前格式为 `ainovel.project` v9，迁移参考值 `0052`。
- `backend/app/services/project_transfer/registry.py:86` 的可选分组是正文版本、大纲快照、生成日志、灵感会话；不是模板内容选择器。
- 同文件 `:194` 包含作品笔记，`:286` 包含大纲，`:288` 包含章节，`:301` 包含场景，`:416`、`:432` 包含设定时间线和演变。
- `backend/app/schemas/transfer.py:8` 的备份默认包含正文版本，不默认包含大纲快照、生成日志和灵感会话。
- 注册表 `:90` 明确排除协作授权、系列成员和采用绑定、API Key 授权、后台任务和智能体会话；封面走独立二进制处理。
- 因此，模板若仅复用设定和大纲，需要明确筛选范围和引用闭包，不能直接把完整备份换个名称；整书复用则与备份机制存在更多重合。
- 模板如果成为新建入口，需要延续 `check_project_creation`，不能绕过作品额度。

## 缩略图的现有落点

- `backend/app/models/entities.py:315` 的世界观条目以 `category` 区分 `place / faction / item / rule / history / other`；地点不是独立实体表。
- `backend/app/models/entities.py:334` 的角色与 `:315` 的世界观条目均无图片字段。
- `frontend/src/pages/project/WorldPanel.tsx:45` 使用世界观分类分组，条目列表和编辑区可作为后续展示入口。
- `frontend/src/components/entities/CharacterCard.tsx` 为现有角色卡片落点。
- `backend/app/services/cover_service.py:47`、`:55` 分别使用 `content.read` 和 `content.edit`；`:58` 上传调用共享 `validate_image`。
- `backend/app/ai/images.py:6`、`:23` 定义当前封面 10MB 上限与图片校验。缩略图可评估复用校验，但不能据此默认采用同样的尺寸、存储或生成范围。
- `.trellis/spec/backend/project-cover.md` 可提供版本 URL、上传失败保持原图和备份图片处理的现有契约。
- `.trellis/spec/backend/series-settings.md` 区分共享条目和本书投影。缩略图属于共享基础还是本书附属，需要在最终设计前明确。

## 模板内容决策的影响

用户已确认的 D13.1 见父任务 PRD：配置＋可选设定／大纲，新书正文为空。设计时需要处理所选资料的引用依赖，并明确如何重建可用的初始设定；不能通过复制完整备份后只清空一个正文字段来假定满足范围。

## 笔名的协作现状与决策影响

- `backend/app/services/project_service.py:421` 的作品列表通过所有者或已接受的协作关系选书；`:348` 的最终投影会再次复核当前权限，系列摘要另按系列权限决定是否返回。
- `backend/app/schemas/project.py` 的 `ProjectOwner` 返回当前账号 ID 和用户名，尚无笔名语义。
- `frontend/src/components/shelf/LibraryBookCard.tsx:26` 的文字封面使用用户名，`:31` 显示「作者：用户名」，`:30` 单独显示系列；书架同时呈现本人及协作作品。
- `frontend/src/utils/shelf.ts` 的 `ShelfPrefs` 当前只保存查询、排序、状态和视图。增加笔名筛选时需要设计当前用户的作用域及账号切换行为。

用户已确认的 D13.2 见父任务 PRD。设计时需分别处理个人笔名库的所有者权限与作品响应中的署名投影，不能开放一个可枚举作者完整笔名库的协作者入口；已有作品访问权也不能授权修改其笔名。

## 缩略图范围的补充证据与待选方案

- `backend/app/schemas/setting_field_value.py:8` 定义四类实体身份 `character / world / organization / career`；`backend/app/schemas/setting_fields.py:32` 的条目类型进一步列出世界观六个分类。
- `frontend/src/components/entities/CardReferenceMappings.tsx:19` 已按角色、世界观、组织和职业展示四类设定名称，与 D13.3 确认的支持类型一致。
- `frontend/src/pages/project/CareersPanel.tsx:67` 渲染职业卡片，并使用 `CareerEditDrawer` 展示详情；`frontend/src/pages/project/OrganizationsPanel.tsx:312` 起的组织详情可作为组织图片展示落点。
- `backend/app/schemas/series.py:10` 当前系列共享仅支持角色与世界观。不能据「四类设定都加缩略图」推导出组织／职业也新增系列共享功能。
- `backend/app/services/cover_service.py:58` 已有图片上传，`:78` 提供移除；前端 `CoverPanel.tsx` 维护上传状态及作品权限代次，并只合并图片版本以保留未保存表单。
- `backend/app/api/v1/project_cover.py` 区分图片上传和 AI 生成预览；AI 路径还使用 `ai.use`、图片模型归属及返回时授权保护。新增设定生图不能只复制上传接口，仍需单独验证提示词内容、模型权限和取消行为。

用户已确认本地上传方案（D13.3）。复用已有图片上传与权限机制的可行性继续研究，AI 生图不进入本轮实施范围；外链图库、自动生成全部条目和批量生图也不因本调查自动进入范围。

## 尚未执行

本轮仅创建规划文档；未运行功能测试、数据库迁移或浏览器验收，不据现有规范宣称新增功能可用。
