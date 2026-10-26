"""提示词模板引擎：沙箱化 Jinja2 + AST 白名单校验。

- 环境：SandboxedEnvironment + StrictUndefined（未定义变量一律报错）；清空全部全局函数（range / lipsum / cycler /
  joiner / namespace / dict），只保留少量安全过滤器。
- 校验（validate）在保存、导入、预览时执行，返回带位置的问题列表：语法、长度、未知变量、必需变量、锁定变量
  （必须出现且只出现一次，只能以 `{{ name }}` 形式直接写在顶层，不能放进 if / for、不能加过滤器或参与运算）、
  for 只能遍历声明为 list 的变量（不能嵌套、不能 recursive），以及禁止的语法（函数 / 方法调用、下划线属性、
  乘法 / 乘方 / 取模、self、set / macro / include 等）。这些限制用来杜绝沙箱逃逸与资源耗尽写法。
- 渲染（render_messages）：每段结果超过 RENDER_LIMIT 报错；锁定内容没有完整出现在结果里时
  追加到 user 段末尾并记 warning。
"""

import logging
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Literal

from jinja2 import StrictUndefined, TemplateSyntaxError, meta, nodes
from jinja2.exceptions import SecurityError
from jinja2.sandbox import SandboxedEnvironment

from app.ai.providers.base import ChatMessage
from app.core.errors import BadRequestError

logger = logging.getLogger(__name__)

PART_LIMIT = 20000
RENDER_LIMIT = 200000
MAX_NAME_REFS = 200

VarKind = Literal["text", "int", "bool", "list"]
Part = Literal["system", "user"]

ALLOWED_FILTERS = ("join", "length", "count", "trim", "default", "d", "upper", "lower", "first", "last", "string")
ALLOWED_TESTS = (
    "defined",
    "undefined",
    "none",
    "number",
    "string",
    "sequence",
    "true",
    "false",
    "eq",
    "ne",
    "equalto",
    "in",
    "odd",
    "even",
)
# 允许出现的语句节点；其余（set / macro / call / filter / include / import / extends / block / with …）一律拒绝
_ALLOWED_STMTS = (nodes.Output, nodes.If, nodes.For)
_STMT_NAMES = {
    nodes.Assign: "set",
    nodes.AssignBlock: "set",
    nodes.Macro: "macro",
    nodes.CallBlock: "call",
    nodes.FilterBlock: "filter",
    nodes.Include: "include",
    nodes.Import: "import",
    nodes.FromImport: "import",
    nodes.Extends: "extends",
    nodes.Block: "block",
    nodes.With: "with",
}


@dataclass(frozen=True)
class TemplateVar:
    name: str
    label: str
    kind: VarKind = "text"
    required: bool = False
    locked: bool = False
    sample: Any = ""


@dataclass(frozen=True)
class PromptOverride:
    """用户覆盖（运行时只传启用的覆盖）。version_id 为当前内容对应的历史版本，写进生成记录。"""

    system: str
    user: str
    version_id: int | None = None


@dataclass
class TemplateIssue:
    part: Part | None
    line: int | None
    message: str

    def as_dict(self) -> dict[str, Any]:
        return {"part": self.part, "line": self.line, "message": self.message}


class TemplateRenderError(BadRequestError):
    """渲染失败（运行时错误或结果过长），detail 为可展示的中文；请求内 → 400，任务内 → failed。"""


@dataclass
class RenderResult:
    messages: list[ChatMessage]
    warnings: list[str] = field(default_factory=list)


class _PromptSandbox(SandboxedEnvironment):
    """运行时兜底：即使模板绕过了校验（例如系统默认升级前保存的旧覆盖），
    乘法 / 乘方 / 取模（`'a' * 1e9`、`'%0999999999d' % 1`）也不会执行。"""

    intercepted_binops = frozenset(["*", "**", "%"])

    def call_binop(self, context, operator, left, right):  # noqa: ANN001
        raise SecurityError(f"不支持 {operator} 运算")


def _make_env() -> SandboxedEnvironment:
    env = _PromptSandbox(
        undefined=StrictUndefined,
        autoescape=False,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )
    env.globals.clear()
    env.filters = {name: env.filters[name] for name in ALLOWED_FILTERS}
    env.tests = {name: env.tests[name] for name in ALLOWED_TESTS if name in env.tests}
    return env


ENV = _make_env()


# ---------------------------------------------------------------- 校验


def _walk(node: nodes.Node, parents: tuple[nodes.Node, ...] = ()):
    yield node, parents
    for child in node.iter_child_nodes():
        yield from _walk(child, (*parents, node))


def _check_part(part: Part, source: str, decls: dict[str, TemplateVar], issues: list[TemplateIssue]):
    """单段校验；返回 (ast, 锁定变量引用 {name: [(合法位置?, line)]})，语法错误时 ast 为 None。"""
    if len(source) > PART_LIMIT:
        issues.append(TemplateIssue(part, None, f"长度超过 {PART_LIMIT} 字"))
        return None, {}
    try:
        ast = ENV.parse(source)
    except TemplateSyntaxError as exc:
        issues.append(TemplateIssue(part, exc.lineno, f"语法错误：{exc.message}"))
        return None, {}

    def add(line: int | None, message: str) -> None:
        issues.append(TemplateIssue(part, line, message))

    locked_refs: dict[str, list[tuple[bool, int]]] = {}
    name_refs = 0
    first_line: dict[str, int] = {}
    for node, parents in _walk(ast):
        line = getattr(node, "lineno", None)
        if isinstance(node, nodes.Stmt) and not isinstance(node, _ALLOWED_STMTS):
            label = _STMT_NAMES.get(type(node), type(node).__name__.lower())
            add(line, f"不支持 {{% {label} %}} 语句")
        elif isinstance(node, nodes.Call):
            add(line, "不支持函数或方法调用")
        elif isinstance(node, nodes.Getattr) and node.attr.startswith("_"):
            add(line, "不允许访问以下划线开头的属性")
        elif (
            isinstance(node, nodes.Getitem)
            and isinstance(node.arg, nodes.Const)
            and str(node.arg.value).startswith("_")
        ):
            add(line, "不允许访问以下划线开头的属性")
        elif isinstance(node, (nodes.Mul, nodes.Pow)):
            add(line, "不支持乘法与乘方运算")
        elif isinstance(node, nodes.Mod):
            add(line, "不支持 % 取模 / 格式化运算")
        elif isinstance(node, nodes.Filter) and node.name not in ENV.filters:
            add(line, f"不支持的过滤器：{node.name}")
        elif isinstance(node, nodes.Test) and node.name not in ENV.tests:
            add(line, f"不支持的判断：{node.name}")
        elif isinstance(node, nodes.CondExpr) and node.expr2 is None:
            add(line, "条件表达式必须带 else 分支")
        elif isinstance(node, nodes.For):
            if node.recursive:
                add(line, "for 循环不支持 recursive")
            if any(isinstance(p, nodes.For) for p in parents):
                add(line, "for 循环不能嵌套")
            it = node.iter
            if not isinstance(it, nodes.Name) or decls.get(it.name) is None or decls[it.name].kind != "list":
                add(line, "for 只能遍历声明为列表的变量")
        elif isinstance(node, nodes.Name) and node.ctx == "load":
            name_refs += 1
            first_line.setdefault(node.name, line or 0)
            var = decls.get(node.name)
            if var is not None and var.locked:
                direct = (
                    len(parents) == 2
                    and isinstance(parents[0], nodes.Template)
                    and isinstance(parents[1], nodes.Output)
                )
                locked_refs.setdefault(node.name, []).append((direct, line or 0))
    if name_refs > MAX_NAME_REFS:
        add(None, f"变量引用过多（上限 {MAX_NAME_REFS} 处）")
    try:
        undeclared = meta.find_undeclared_variables(ast)
    except TemplateSyntaxError as exc:  # 编译期断言（例如未知过滤器），上面通常已经报过
        if not issues:
            add(exc.lineno, f"语法错误：{exc.message}")
        undeclared = set()
    if "self" in first_line and "self" not in decls:
        undeclared = {*undeclared, "self"}  # Jinja 内置的模板引用，不计入 undeclared，这里按未知变量拒绝
    for name in sorted(undeclared):
        if name not in decls:
            add(first_line.get(name), f"未知变量：{name}")
    return ast, locked_refs


def validate(decls: tuple[TemplateVar, ...], system: str, user: str) -> list[TemplateIssue]:
    """保存 / 导入 / 预览前的校验；返回空列表表示合法。"""
    by_name = {v.name: v for v in decls}
    issues: list[TemplateIssue] = []
    asts: dict[Part, nodes.Template | None] = {}
    locked: dict[str, list[tuple[Part, bool, int]]] = {}
    for part, source in (("system", system), ("user", user)):
        ast, refs = _check_part(part, source, by_name, issues)
        asts[part] = ast
        for name, items in refs.items():
            locked.setdefault(name, []).extend((part, direct, line) for direct, line in items)
    if any(ast is None for ast in asts.values()):
        return issues  # 语法错误时其余检查没有意义

    used: set[str] = set()
    for ast in asts.values():
        assert ast is not None
        used |= {n.name for n in ast.find_all(nodes.Name) if n.ctx == "load"}
    for var in decls:
        refs = locked.get(var.name, [])
        if var.locked:
            if not refs:
                issues.append(TemplateIssue("user", None, f"锁定变量 {{{{ {var.name} }}}} 必须保留"))
                continue
            if len(refs) > 1:
                part, _, line = refs[1]
                issues.append(TemplateIssue(part, line, f"锁定变量 {{{{ {var.name} }}}} 只能出现一次"))
            for part, direct, line in refs:
                if not direct:
                    issues.append(
                        TemplateIssue(
                            part,
                            line,
                            f"锁定变量 {{{{ {var.name} }}}} 只能以 {{{{ {var.name} }}}} 的形式写在顶层，"
                            "不能放进 if / for，也不能加过滤器或参与运算",
                        )
                    )
        elif var.required and var.name not in used:
            issues.append(TemplateIssue("user", None, f"缺少必需变量 {{{{ {var.name} }}}}"))
    return issues


# ---------------------------------------------------------------- 渲染


@lru_cache(maxsize=256)
def _compile(source: str):
    return ENV.from_string(source)


def render_text(source: str, values: dict[str, Any]) -> str:
    if not source:
        return ""
    chunks: list[str] = []
    size = 0
    try:
        # 边生成边计数：超过上限立即停止，不先在内存里拼出完整的超长结果（循环 × 大变量）
        for chunk in _compile(source).generate(**values):
            size += len(chunk)
            if size > RENDER_LIMIT:
                raise TemplateRenderError(f"模板渲染结果超过 {RENDER_LIMIT} 字")
            chunks.append(chunk)
    except TemplateRenderError:
        raise
    except Exception as exc:  # noqa: BLE001 — 沙箱 / 未定义变量 / 类型错误等都转成可展示的中文
        raise TemplateRenderError(f"模板渲染失败：{exc}") from exc
    return "".join(chunks)


def render_messages(
    decls: tuple[TemplateVar, ...], system: str, user: str, values: dict[str, Any], *, key: str = ""
) -> RenderResult:
    """渲染 system / user 两段为 messages；system 渲染为空白时不产出 system 消息。"""
    system_text = render_text(system, values)
    user_text = render_text(user, values)
    warnings: list[str] = []
    for var in decls:
        if not var.locked:
            continue
        value = str(values.get(var.name, ""))
        if value and value not in system_text and value not in user_text:
            # 兜底：校验保证锁定变量出现，这里只防止意外（例如旧数据绕过了校验）
            user_text = f"{user_text}\n\n{value}" if user_text else value
            warnings.append(f"锁定变量 {var.name} 缺失，已追加到末尾")
            logger.warning("提示词模板 %s 缺少锁定变量 %s，已追加到 user 段末尾", key, var.name)
    messages: list[ChatMessage] = []
    if system_text.strip():
        messages.append({"role": "system", "content": system_text})
    messages.append({"role": "user", "content": user_text})
    return RenderResult(messages=messages, warnings=warnings)
