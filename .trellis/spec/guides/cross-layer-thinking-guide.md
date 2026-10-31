# 跨层思考指南

> **目的**：在实现之前，先梳理数据在各层之间的流动。

---

## 问题所在

**大多数 bug 发生在层与层的边界上**，而不是层的内部。

常见的跨层 bug：

- API 返回格式 A，前端期望格式 B
- 数据库存储 X，service 转换为 Y，却丢失了数据
- 多个层以不同方式实现同一套逻辑

---

## 实现跨层功能之前

### 第 1 步：绘制数据流

画出数据如何流动：

```
Source → Transform → Store → Retrieve → Transform → Display
```

对每个箭头，问自己：

- 数据是什么格式？
- 可能出什么问题？
- 谁负责校验？

### 第 2 步：识别边界

| 边界                  | 常见问题                          |
| --------------------- | --------------------------------- |
| API ↔ Service         | 类型不匹配、缺少字段              |
| Service ↔ Database    | 格式转换、null 处理               |
| Backend ↔ Frontend    | 序列化、日期格式                  |
| Component ↔ Component | props 结构变化                    |

### 第 3 步：定义契约

对每个边界：

- 确切的输入格式是什么？
- 确切的输出格式是什么？
- 可能发生哪些错误？

---

## 常见的跨层错误

### 错误 1：隐式的格式假设

**错误做法**：不加检查就假定日期格式

**正确做法**：在边界处进行显式的格式转换

### 错误 2：分散的校验

**错误做法**：在多个层中校验同一件事

**正确做法**：在入口处只校验一次

### 错误 3：抽象泄漏

**错误做法**：组件了解数据库 schema

**正确做法**：每一层只了解与其相邻的层

### 错误 4：每个消费方都解析同一份 payload

**错误做法**：某个命令读取 JSONL 事件，并在行内对字段做强转：

```typescript
const thread = (ev as { thread?: string }).thread;
const labels = (ev as { labels?: string[] }).labels;
```

这看起来只是局部代码，但意味着每个消费方都持有一份私有版本的事件契约。
下一次字段变更时，就会改了一个命令而漏掉另一个。

**正确做法**：在事件边界处统一解码一次，然后导出带类型的投影：

```typescript
if (!isThreadEvent(ev)) return false;
return ev.thread === filter.thread;
```

**规则**：对于 append-only 日志、JSON 流、RPC payload 或配置文件，
为以下内容指定唯一的所有者：

- event / payload 类型定义
- 从 `unknown` 出发的类型守卫与规范化
- UI 命令使用的元数据投影
- 从事实来源重放状态的 reducer

渲染代码可以格式化字段，但不得重新定义 payload 契约。

---

## 跨层功能检查清单

实现之前：

- [ ] 已绘制完整的数据流
- [ ] 已识别所有层边界
- [ ] 已定义每个边界的格式
- [ ] 已确定在哪里进行校验
- [ ] 资源整数ID若可能被删除后复用，跨事务对象、缓存、异步执行体及清理回调是否携带同一不可复用身份，并在实际写入/刷新/内存收尾时核对，而非只在await之前检查？任务实例见[管理员首页执行身份契约](../backend/admin-dashboard.md)。

实现之后：

- [ ] 已用边界情况测试（null、空值、非法值）
- [ ] 已验证每个边界的错误处理
- [ ] 已确认数据经过往返后保持不变
- [ ] 已确认消费方导入共享的解码器 / 投影，而不是在本地对
      payload 字段做强转
- [ ] 已确认派生状态指回源事件标识符
      （`seq`、`id`、`version`），而不是另造第二个游标

---

## 跨平台模板一致性

在 Trellis 中，命令模板（例如 `record-session.md`）存在于**多个平台**中，内容完全相同或几乎相同。这是一个跨层边界。

### 检查清单：修改任何命令模板之后

- [ ] 找出拥有同一命令的所有平台：`find src/templates/*/commands/trellis/ -name "<command>.*"`
- [ ] 更新所有平台副本（Markdown `.md` 与 TOML `.toml`）
- [ ] 对于 Gemini TOML：适配续行符（`\\` 与 `\`）以及三引号字符串
- [ ] 运行 `/trellis:check-cross-layer` 确认没有遗漏

**真实案例**：在 Claude 中更新了 `record-session.md` 以使用 `--mode record`，但忘了 iFlow、Kilo、OpenCode 和 Gemini——被跨层检查发现。

---

## 生成的运行时模板的升级一致性

有些生成的文件既是文档也是运行时输入。在 Trellis 中，
`.trellis/workflow.md` 会被 `get_context.py`、`workflow_phase.py`、
SessionStart 过滤器以及每轮 hook 解析。模板变更必须同时针对
全新 init 与升级两条路径进行验证。

### 检查清单：修改运行时解析的模板之后

- [ ] 找出读取该模板的每一个运行时解析器，而不仅仅是安装它的
      文件写入方
- [ ] 检查相关语法是否位于明显的托管区域（例如标签块）
      之外
- [ ] 验证全新 `init` 的输出，以及一个写入旧版
      `.trellis/.version` 的带版本 `update` 场景
- [ ] 使用旧版原始模板 fixture 添加升级回归测试，然后
      断言安装后的文件达到当前打包版本的形态
- [ ] 更新持有该运行时契约的后端 spec

---

## 版本化文档边界

版本化文档是一个跨层边界：源路径、`docs.json`
版本路由以及渲染出的版本选择器必须描述同一条
发布线。

### 检查清单：编辑版本化文档之前

- [ ] 确定目标发布线：stable、beta 或 RC
- [ ] 确认所编辑的 MDX 路径与该发布线匹配：
  - stable：`docs-site/{start,advanced,...}` 与 `docs-site/zh/{start,advanced,...}`
  - beta：`docs-site/beta/**` 与 `docs-site/zh/beta/**`
  - RC：`docs-site/rc/**` 与 `docs-site/zh/rc/**`
- [ ] 确认 `docs.json` 导航将版本标签指向相同的路径
- [ ] 提交前在另一棵目录树中 grep 发布线特有的术语
- [ ] 把出现在根发布路径下的 beta 内容视为源路径 bug，
      而不是渲染 bug

**真实案例**：一次仅限 beta 的任务工作流变更，把
`prd.md` + `design.md` + `implement.md`、任务创建确认以及 Codex
模式横幅写到了根目录的 `start/` 和 `advanced/` 路径下。结果文档站在 Release
选择器下展示了 0.6 beta 的行为。修复方式是恢复根目录的正式发布文档，
把 0.6 内容移到 `beta/` 和 `zh/beta/`，并针对根发布目录树添加一次
beta 标记的 grep 审计。

**真实案例**：Codex inline 模式把工作流平台标记从
`[Codex]` / `[Kilo, Antigravity, Windsurf]` 改为 `[codex-sub-agent]` /
`[codex-inline, Kilo, Antigravity, Windsurf]`。全新 init 是正确的，但
`trellis update` 只合并了 `[workflow-state:*]` 块，保留了这些块之外的过时
标记。结果：升级后的项目拿到了新的 hook 脚本，
却仍是旧的工作流路由，因此 `get_context.py --mode phase --platform codex`
可能返回空的 Phase 2.1 详情。

---

## 模式探测检查清单

当 CLI 通过探测远程资源来自动判断模式时（例如检查 `index.json` 是否存在，以决定走 marketplace 还是直接下载）：

### 实现之前：

- [ ] 探测在使用其结果的**所有**代码路径中都会运行（交互模式、`-y`、`--flag` 组合）
- [ ] 区分 404 与临时性错误——不要把两者都当作"未找到"
- [ ] 临时性错误应**中止或重试**，绝不悄悄切换模式
- [ ] 上下文变化时（例如用户切换来源），共享状态（缓存、预取数据）要被**重置**
- [ ] **快捷路径**（例如跳过选择器的 `--template`）必须与探测路径具有同等质量的错误处理——检查下游函数没有调用兜底式包装器

### 实现之后：

- [ ] 追踪从探测结果到模式判定分支的每一条路径——不存在落空情况
- [ ] 外部格式契约（giget URI、raw URL）已被测试，或至少以注释形式记录
- [ ] 元数据读取要么消费完整响应，要么使用流式解析器——绝不把固定长度的前缀当作完整 JSON 解析
- [ ] 从解析出的各部分重建复合标识符时，确认**所有**字段都已包含且位于**正确位置**（例如 `provider:repo/path#ref` 而非 `provider:repo#ref/path`）
- [ ] 确认快捷路径之后调用的**动作函数**内部没有使用旧的兜底式 fetch——在需要区分错误时，它们必须使用探测级质量的变体

**真实案例**：自定义 registry 流程在 3 轮评审中出现了 8 个 bug：(1) 探测只在交互模式下运行，(2) 临时性错误落入了错误的模式，(3) giget URI 中 `#ref` 位置错误，(4) 预取的模板在切换来源时泄漏，(5) `--template` 快捷路径绕过了探测，但 `downloadTemplateById` 内部使用了兜底式的 `fetchTemplateIndex`，把超时变成了 "Template not found"。

**真实案例**：Agent 会话的更新提示通过 `response.read(4096)` 获取 npm `latest` 元数据，然后将其当作完整 JSON 解析。`@mindfoldhq/trellis` 包的元数据超过 4 KB，导致 JSON 被截断、解析静默失败，首次会话注入时没有显示更新提示。修复：先读取完整响应再解析，并添加一个 `version` 后跟 8 KB 元数据尾部的回归测试。

---

## 跨平台模板一致性

在 Trellis 中，命令模板（例如 `record-session.md`）存在于**多个平台**中，内容完全相同或几乎相同。这是一个跨层边界。

### 检查清单：修改任何命令模板之后

- [ ] 找出拥有同一命令的所有平台：`find src/templates/*/commands/trellis/ -name "<command>.*"`
- [ ] 更新所有平台副本（Markdown `.md` 与 TOML `.toml`）
- [ ] 对于 Gemini TOML：适配续行符（`\\` 与 `\`）以及三引号字符串
- [ ] 运行 `/trellis:check-cross-layer` 确认没有遗漏

**真实案例**：在 Claude 中更新了 `record-session.md` 以使用 `--mode record`，但忘了 iFlow、Kilo、OpenCode 和 Gemini——被跨层检查发现。

---

## 生成的运行时模板的升级一致性

有些生成的文件既是文档也是运行时输入。在 Trellis 中，
`.trellis/workflow.md` 会被 `get_context.py`、`workflow_phase.py`、
SessionStart 过滤器以及每轮 hook 解析。模板变更必须同时针对
全新 init 与升级两条路径进行验证。

### 检查清单：修改运行时解析的模板之后

- [ ] 找出读取该模板的每一个运行时解析器，而不仅仅是安装它的
  文件写入方
- [ ] 检查相关语法是否位于明显的托管区域（例如标签块）
  之外
- [ ] 验证全新 `init` 的输出，以及一个写入旧版
  `.trellis/.version` 的带版本 `update` 场景
- [ ] 使用旧版原始模板 fixture 添加升级回归测试，然后
  断言安装后的文件达到当前打包版本的形态
- [ ] 更新持有该运行时契约的后端 spec

**真实案例**：Codex inline 模式把工作流平台标记从
`[Codex]` / `[Kilo, Antigravity, Windsurf]` 改为 `[codex-sub-agent]` /
`[codex-inline, Kilo, Antigravity, Windsurf]`。全新 init 是正确的，但
`trellis update` 只合并了 `[workflow-state:*]` 块，保留了这些块之外的过时
标记。结果：升级后的项目拿到了新的 hook 脚本，
却仍是旧的工作流路由，因此 `get_context.py --mode phase --platform codex`
可能返回空的 Phase 2.1 详情。

---

## 模式探测检查清单

当 CLI 通过探测远程资源来自动判断模式时（例如检查 `index.json` 是否存在，以决定走 marketplace 还是直接下载）：

### 实现之前：
- [ ] 探测在使用其结果的**所有**代码路径中都会运行（交互模式、`-y`、`--flag` 组合）
- [ ] 区分 404 与临时性错误——不要把两者都当作"未找到"
- [ ] 临时性错误应**中止或重试**，绝不悄悄切换模式
- [ ] 上下文变化时（例如用户切换来源），共享状态（缓存、预取数据）要被**重置**
- [ ] **快捷路径**（例如跳过选择器的 `--template`）必须与探测路径具有同等质量的错误处理——检查下游函数没有调用兜底式包装器

### 实现之后：
- [ ] 追踪从探测结果到模式判定分支的每一条路径——不存在落空情况
- [ ] 外部格式契约（giget URI、raw URL）已被测试，或至少以注释形式记录
- [ ] 元数据读取要么消费完整响应，要么使用流式解析器——绝不把固定长度的前缀当作完整 JSON 解析
- [ ] 从解析出的各部分重建复合标识符时，确认**所有**字段都已包含且位于**正确位置**（例如 `provider:repo/path#ref` 而非 `provider:repo#ref/path`）
- [ ] 确认快捷路径之后调用的**动作函数**内部没有使用旧的兜底式 fetch——在需要区分错误时，它们必须使用探测级质量的变体

**真实案例**：自定义 registry 流程在 3 轮评审中出现了 8 个 bug：(1) 探测只在交互模式下运行，(2) 临时性错误落入了错误的模式，(3) giget URI 中 `#ref` 位置错误，(4) 预取的模板在切换来源时泄漏，(5) `--template` 快捷路径绕过了探测，但 `downloadTemplateById` 内部使用了兜底式的 `fetchTemplateIndex`，把超时变成了 "Template not found"。

**真实案例**：Agent 会话的更新提示通过 `response.read(4096)` 获取 npm `latest` 元数据，然后将其当作完整 JSON 解析。`@mindfoldhq/trellis` 包的元数据超过 4 KB，导致 JSON 被截断、解析静默失败，首次会话注入时没有显示更新提示。修复：先读取完整响应再解析，并添加一个 `version` 后跟 8 KB 元数据尾部的回归测试。

---

## 何时编写流程文档

在以下情况下编写详细的流程文档：

- 功能跨越 3 个及以上的层
- 涉及多个团队
- 数据格式复杂
- 该功能曾经引发过 bug

---

## 事件日志 / 投影边界

append-only 日志是跨层契约。一个事件会依次经过：

```
CLI input → event writer → events.jsonl → reader → filter → reducer → display
```

### 检查清单：新增事件 kind 或字段之后

- [ ] 把该事件 kind 加入集中的事件分类表
- [ ] 在事件层添加带类型的事件变体或类型守卫
- [ ] 为来自用户输入或 JSON 的数组/对象字段添加
      规范化辅助函数
- [ ] `seq` / `id` 的分配只放在事件写入方
- [ ] 让过滤器和 reducer 使用带类型的事件守卫，而不是本地强转
- [ ] 让展示代码使用 reducer 输出或带类型的事件，而不是原始 JSON
- [ ] 至少添加一个回归测试，证明历史重放与实时过滤
      使用同一套过滤模型

**真实案例**：Thread 频道新增了 `kind: "thread"`、`description`、
`context`、labels 和 `lastSeq`。最初的实现能正确重放 thread
状态，但若干命令仍然通过本地强转重新解析事件 payload 字段。
修复方式是让核心事件层持有 `ThreadChannelEvent`
和 `isThreadEvent`，让 `reduceChannelMetadata` 成为唯一的频道元数据
投影，并让 `reduceThreads` 成为唯一的 thread 重放 reducer。
