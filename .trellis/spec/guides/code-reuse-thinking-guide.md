# 代码复用思考指南

> **目的**：在编写新代码前停下来想一想——它是否已经存在？

---

## 问题所在

**重复代码是不一致类 bug 的头号来源。**

当你复制粘贴或重写已有逻辑时：
- bug 修复无法同步传播
- 行为随时间逐渐分化
- 代码库变得更难理解

---

## 编写新代码之前

### 第 1 步：先搜索

```bash
# 搜索相似的函数名
grep -r "functionName" .

# 搜索相似的逻辑
grep -r "keyword" .
```

### 第 2 步：问自己这些问题

| 问题 | 如果是…… |
|----------|-----------|
| 是否已有相似的函数？ | 直接使用或扩展它 |
| 这个模式在别处用过吗？ | 遵循已有模式 |
| 这能否成为共享工具？ | 在合适的位置创建它 |
| 我是否在从另一个文件复制代码？ | **停下**——抽取为共享代码 |

---

## 常见重复模式

### 模式 1：复制粘贴函数

**错误做法**：把一个校验函数复制到另一个文件

**正确做法**：抽取到共享工具中，在需要的地方导入

### 模式 2：相似组件

**错误做法**：新建一个与现有组件 80% 相似的组件

**正确做法**：通过 props/变体扩展现有组件

### 模式 3：重复的常量

**错误做法**：在多个文件中定义同一个常量

**正确做法**：单一事实来源，各处导入

### 模式 4：重复提取 payload 字段

**错误做法**：多个消费方各自在本地对同一 JSON/事件字段做强转：

```typescript
const description = (ev as { description?: string }).description;
const context = (ev as { context?: ContextEntry[] }).context;
```

即使只有两行代码，这也是重复的契约逻辑。每个消费方都拥有了自己对"合法
payload"的定义。

**正确做法**：把解码器、类型守卫或投影放在数据所有者旁边：

```typescript
if (isThreadEvent(ev)) {
  renderThreadEvent(ev);
}
```

**规则**：如果同一个无类型 payload 字段在 2 处及以上被读取，在添加第三个读取方
之前，先创建共享的类型守卫 / 规范化函数 / 投影。

---

## 何时抽象

**应当抽象的情况**：
- 同样的代码出现 3 次及以上
- 逻辑复杂到可能出 bug
- 多人可能需要用到

**不应抽象的情况**：
- 只用一次
- 简单的一行代码
- 抽象本身会比重复更复杂

---

## 批量修改之后

当你在多个文件中做了相似的修改后：

1. **复查**：是否覆盖了所有实例？
2. **搜索**：运行 grep 查找遗漏之处
3. **考虑**：这是否应该被抽象？

### Reducer 应采用穷尽式结构

当状态由类 action 的值（`action`、`kind`、`status`、`phase`）派生时，
优先使用带单个 `switch` 的 reducer，而不是分散的 `if/else` 更新。

```typescript
// 错误 - 按 action 分散的状态转换难以审计
if (action === "opened") { ... }
else if (action === "comment") { ... }
else if (action === "status") { ... }

// 正确 - 由一个 reducer 统一持有转换表
switch (event.action) {
  case "opened":
    ...
    return;
  case "comment":
    ...
    return;
}
```

当事件日志是事实来源时，这一点尤为重要。reducer 就是有文档约定的重放模型；
展示代码和命令不应重复实现该重放模型的任何部分。

---

## 提交前检查清单

- [ ] 已搜索现有的相似代码
- [ ] 没有本应共享却被复制粘贴的逻辑
- [ ] 共享解码器之外没有重复提取无类型 payload 字段
- [ ] 常量只在一处定义
- [ ] 相似模式遵循相同结构
- [ ] reducer/action 状态转换集中在一个 reducer 或命令分发器中

---

## 陷阱：Python if/elif/else 的穷尽性检查

**问题**：Python 的 if/elif/else 链没有编译期穷尽性检查。当你向 `Literal` 类型（例如 `Platform`）新增一个值时，已有的 if/elif/else 链会悄无声息地落入 `else`，返回错误的默认值。

**症状**：新平台只能部分工作——某些方法返回的是 Claude 的默认值而不是该平台特定的值，并且不会抛出任何错误。

**示例**（`cli_adapter.py`）：
```python
# 错误："gemini" 落入 else，返回 "claude"
@property
def cli_name(self) -> str:
    if self.platform == "opencode":
        return "opencode"
    else:
        return "claude"  # gemini 悄无声息地得到了 "claude"！

# 正确：为每个平台提供显式分支
@property
def cli_name(self) -> str:
    if self.platform == "opencode":
        return "opencode"
    elif self.platform == "gemini":
        return "gemini"
    else:
        return "claude"
```

**预防**：向 Python `Literal` 类型新增值时，搜索所有基于该类型分支的 if/elif/else 链并添加显式分支。不要指望 `else` 对新值也是正确的。

---

## 陷阱：不对称机制产出相同结果

**问题**：当两套不同机制必须产出同一组文件时（例如 init 使用递归目录复制，而 update 使用手动 `files.set()`），结构性变更（重命名、移动、新增子目录）只会通过自动机制传播，手动机制则会悄悄偏离。

**症状**：init 完全正常，但 update 会在错误路径上创建文件，或完全遗漏某些文件。

**预防**：
- **最佳**：消除不对称——让手动路径调用自动路径（例如 `collectTemplateFiles()` 调用 `getAllScripts()`，而不是自己维护一份列表）
- **如果不对称无法避免**：添加回归测试，比较两套机制的输出
- 迁移目录结构时，搜索所有引用旧结构的代码路径

**真实案例**：`trellis update` 中有一份手动 `files.set()` 列表，包含 11 个已由 `getAllScripts()` 跟踪的脚本。修复方式：用 `for..of getAllScripts()` 循环替换手动列表。参见 v0.4.0-beta.3 中的 `update.ts` 重构。

---

## 模板文件注册（Trellis 专属）

向 `src/templates/trellis/scripts/` 新增文件时：

**唯一注册点**：`src/templates/trellis/index.ts`

1. 添加 `export const xxxScript = readTemplate("scripts/path/file.py");`
2. 加入 `getAllScripts()` Map

就这些。`commands/update.ts` 直接使用 `getAllScripts()`——无需手动同步。

**为什么重要**：如果没有在 `getAllScripts()` 中注册，`trellis update` 就不会把该文件同步到用户项目中，bug 修复和新功能都无法传播。

**历史**：在 v0.4.0-beta.3 之前，`update.ts` 有一份手工维护的文件列表，经常与 `getAllScripts()` 不同步，导致 11 个 Python 文件在 `trellis update` 时被悄悄跳过。修复方式是删除这份重复列表，以 `getAllScripts()` 作为单一事实来源。

### 新增脚本速查清单

```bash
# 新增 .py 文件后，确认它已在 getAllScripts() 中：
grep -l "newFileName" src/templates/trellis/index.ts  # 应当有匹配
```

### 模板同步约定

`.trellis/scripts/`（自用版本）与 `packages/cli/src/templates/trellis/scripts/`（模板）必须保持一致。编辑 `.trellis/scripts/` 之后，务必同步：

```bash
rsync -av --delete --exclude='__pycache__' .trellis/scripts/ packages/cli/src/templates/trellis/scripts/
```

**陷阱**：rsync 的源/目标路径写错时，可能生成嵌套的垃圾目录（例如 `.trellis/scripts/packages/cli/...`）。执行前务必反复核对路径。
