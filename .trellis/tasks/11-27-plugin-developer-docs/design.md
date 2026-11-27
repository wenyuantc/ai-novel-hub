# 开发文档（插件开发指南）：技术设计

> 需求见同目录 `prd.md`（W1–W6）；决定编号见父任务 `prd.md`（D4、D15）。

## 1. 设计原则

1. **文档写事实，事实来自代码。** 指南里出现的每个字段名、路径、命令都必须能在仓库里找到；用测试把最容易漂移的部分钉住。
2. **不动现有内容。** `/developers` 现在是面向第三方客户的开放 API 指南；插件指南作为并列的第二部分加入，现有章节、锚点、示例保持原样。
3. **公开页面只放可公开的内容。** 页面无需登录（`frontend/src/router.tsx:24`），不写任何实例信息、密钥或内部地址。

## 2. 现状

- 页面骨架 `frontend/src/pages/DeveloperDocsPage.tsx`：页眉、导语、左侧目录、正文区依次渲染八个章节组件。
- 目录来自 `frontend/src/components/developerDocs/content.ts` 的 `DOC_SECTIONS`（八项，平铺）。
- 页眉导语与「接口地址」条是针对开放 API 写的。
- 已有测试 `frontend/src/utils/developerDocs.test.ts`。

## 3. 页面结构调整

- `DOC_SECTIONS` 由平铺改为分组：

```ts
export const DOC_GROUPS = [
  { id: 'open-api', title: '开放接口', sections: [ /* 现有八项，id 不变 */ ] },
  { id: 'feature-plugins', title: '功能插件开发', sections: [ /* 新增，见第 4 节 */ ] },
]
export const DOC_SECTIONS = DOC_GROUPS.flatMap((group) => group.sections)   // 保留原导出，现有引用不受影响
```

- 目录按组显示小标题，组内继续编号；移动端折叠目录同样分组。
- 正文区在现有八个组件之后加入分隔与第二部分的导语，再渲染插件指南的各章节组件。
- 页眉导语不改；第二部分自带一段说明，写明它面向的读者与开放接口部分不同。
- 每个新章节的锚点以 `plugin-` 开头，避免与现有锚点冲突。

新增文件：

```
frontend/src/components/developerDocs/
  pluginContent.ts          # 结构化内容：清单字段表、接入点表、检查清单、示例代码字符串
  PluginOverview.tsx        # 概念、边界、与 MCP 插件的区别
  PluginAnatomy.tsx         # 目录结构、清单字段
  PluginExtensionPoints.tsx # 各接入点
  PluginLifecycle.tsx       # 启停行为与数据
  PluginWalkthrough.tsx     # 最小示例 + 短剧插件走读
  PluginChecklist.tsx       # 发布前检查清单
```

代码示例复用现有的 `CodeBlock` 组件。

## 4. 内容大纲（W2、W3、W6）

| 章节（锚点） | 内容 |
| --- | --- |
| 功能插件是什么（`plugin-overview`） | 随版本内置的可选功能模块；由管理员在后台启用 / 停用；不支持运行时上传安装，也不加载外部代码。与「MCP 插件」的区别：后者是作者自己接入的外部工具，两者互不相关（D4）。 |
| 目录结构（`plugin-anatomy`） | 后端 `backend/app/plugins/<标识>/`、前端 `frontend/src/plugins/<标识>/` 的推荐布局；两处登记点（后端 `REGISTERED`、前端 `PLUGINS`）。 |
| 清单字段（`plugin-manifest`） | 后端 `PluginManifest` 与前端 `FeaturePlugin`、`PluginPage` 的逐字段说明表；标识的格式与唯一性；接口前缀、任务类型前缀、页面路径首段都必须是插件标识。 |
| 后端接口（`plugin-api`） | 路由如何声明；框架在挂载处统一加的「已启用」门禁；作品级鉴权仍由插件自己做；未启用时的响应。 |
| 后台任务（`plugin-tasks`） | 任务类型命名；处理器需要实现的方法；受理、重试、领取三处的启用判定；停用时运行中的任务会被取消。 |
| 页面与导航（`plugin-frontend`） | 作品内页面与全站页面；按需加载；导航分组；未启用时的页面表现；如何得知启用状态。 |
| 数据与备份（`plugin-data`） | 表名前缀与迁移放在哪；作品相关的表必须声明备份规则；随作品删除的做法；媒体文件用媒体存储并带上插件标识。 |
| 启停与数据（`plugin-lifecycle`） | 默认停用；停用只关入口、数据保留；重新启用后恢复；插件不能假设自己一直处于启用状态。 |
| 最小示例（`plugin-quickstart`） | 一个只有一个接口和一个页面的插件，从新建目录到在后台看到并启用它的完整步骤。 |
| 完整示例：短剧插件（`plugin-example-drama`） | 以短剧插件为例走读清单、表、任务、页面、备份规则各在哪个文件。 |
| 发布前检查清单（`plugin-checklist`） | 标识与前缀、鉴权、停用后的行为、备份规则、测试、README 与规范同步。 |

「数据与备份」「上传体积限制」等接入点由短剧子任务加入框架；本子任务动手时以当时代码里真实存在的接入点为准，
没有的不写，不为将来可能有的接入点预留说明。

## 5. 一致性保障（W4）

漂移最容易发生在三处，各用一个测试钉住：

1. **前端类型的字段**：`pluginContent.ts` 里的字段说明表用类型约束写成

   ```ts
   export const PLUGIN_PAGE_FIELDS = { path: '…', load: '…', nav: '…' } satisfies Record<keyof PluginPage, string>
   ```

   类型新增或改名字段而文档未跟上时，`tsc` 直接报错（构建门禁的一部分）。`FeaturePlugin` 同理。
2. **后端清单的字段**：后端测试读取 `dataclasses.fields(PluginManifest)` 的名字集合，与 `pluginContent.ts` 里后端字段表的键集合比较
   （测试读取该文件并用正则取出键名；文件里用固定的标记注释包住这张表，便于稳定提取）。
3. **文档里提到的路径**：`pluginContent.ts` 导出一个 `REFERENCED_PATHS` 数组，后端测试断言每个路径在仓库里存在。

文案本身（解释性的句子）不做自动校验，靠评审。

## 6. 可访问性与样式

- 沿用页面现有的样式类与排版（`DeveloperDocsPage.css`），新增内容不引入新的视觉语言；表格在窄屏下可横向滚动。
- 目录分组后保持键盘可达与现有的「跳到正文」链接；锚点跳转后标题不被页眉遮挡（沿用现有偏移）。
- 页面仍不加载任何私有数据。

## 7. 权衡记录

| 问题 | 选择 | 放弃的方案与原因 |
| --- | --- | --- |
| 放在哪 | 同一页面的第二部分 | 新开 `/developers/plugins` 路由：用户明确要求加在 `/developers` 页面 |
| 内容形式 | 静态 React 组件 + 结构化内容文件 | Markdown 渲染：项目里这个页面没有 Markdown 管线，为一处文档引入不值得 |
| 一致性 | 类型约束 + 两个后端测试 | 从代码自动生成文档：只能生成字段表，解释仍要手写，而且要新增生成步骤 |
| 管理员操作说明 | 不写在这里 | 按项目惯例在 README；此页面向开发者 |

## 8. 规范与文档

- 前端规范 `component-guidelines.md` 增补「开发文档页的两部分结构与一致性测试」。
- README 的「功能插件」部分加一句指向 `/developers` 的插件开发指南。
