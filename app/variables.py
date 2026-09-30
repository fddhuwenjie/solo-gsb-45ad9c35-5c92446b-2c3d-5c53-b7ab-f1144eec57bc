"""var() 解析、替换与自定义属性（custom properties）求值。

求值顺序：自定义属性先继承（取祖先的计算值），本元素声明再按层叠规则覆盖；
var() 递归解析并支持 fallback；处于循环依赖中的自定义属性失效；
引用缺失或失效变量且没有 fallback 的声明标记为 invalid。
"""
from __future__ import annotations

# 本模块中 lookup 均为可调用对象：lookup(name) -> (status, value)，
# 其中 status ∈ {"ok", "missing", "invalid"}。


class VarSyntaxError(ValueError):
    pass


class Unresolvable(Exception):
    """var() 无法解析：变量缺失 / 已失效 / 语法错误，且没有可用 fallback。"""

    def __init__(self, name: str, status: str):
        super().__init__(f"var({name}) 无法解析：{status}")
        self.name = name
        self.status = status


def _is_ident_char(ch: str) -> bool:
    return ch.isalnum() or ch in "-_"


def parse_var_call(text: str, start: int) -> tuple[str, str | None, int]:
    """解析以 text[start] == 'v' 开头的 var( ... )。

    返回 (变量名, fallback 或 None, 结束下标)。fallback 可含嵌套 var() 与逗号。
    """
    assert text.startswith("var(", start)
    i, n = start + 4, len(text)
    while i < n and text[i].isspace():
        i += 1
    if not text.startswith("--", i):
        raise VarSyntaxError("var() 的第一个参数必须以 '--' 开头")
    j = i + 2
    while j < n and _is_ident_char(text[j]):
        j += 1
    name = text[i:j]
    while j < n and text[j].isspace():
        j += 1
    if j < n and text[j] == ",":
        j += 1
        depth, k = 1, j
        while k < n and depth > 0:
            if text[k] == "(":
                depth += 1
            elif text[k] == ")":
                depth -= 1
            k += 1
        if depth != 0:
            raise VarSyntaxError("var() 括号未闭合")
        return name, text[j : k - 1].strip(), k
    if j < n and text[j] == ")":
        return name, None, j + 1
    raise VarSyntaxError("var() 参数列表格式错误")


def substitute_vars(text: str, lookup, steps: list[str] | None = None) -> str:
    """把 text 中的 var() 全部替换为 lookup 给出的值，失败抛 Unresolvable。"""
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        if text.startswith("var(", i):
            try:
                name, fallback, end = parse_var_call(text, i)
            except VarSyntaxError as exc:
                raise Unresolvable("<syntax>", "syntax") from exc
            status, value = lookup(name)
            if status == "ok":
                if steps is not None:
                    steps.append(f"var({name}) → {value!r}")
                out.append(value)
            elif fallback is not None:
                sub = substitute_vars(fallback, lookup, steps)
                if steps is not None:
                    reason = "未定义" if status == "missing" else "已失效"
                    steps.append(f"var({name}) 引用的变量{reason}，使用 fallback → {sub!r}")
                out.append(sub)
            else:
                raise Unresolvable(name, status)
            i = end
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


def extract_var_names(text: str) -> list[str]:
    """提取所有 var() 引用的变量名（含 fallback 内部的，用于循环依赖检测）。"""
    names: list[str] = []
    i, n = 0, len(text)
    while i < n:
        if text.startswith("var(", i):
            try:
                name, fallback, end = parse_var_call(text, i)
            except VarSyntaxError:
                i += 4
                continue
            names.append(name)
            if fallback:
                names.extend(extract_var_names(fallback))
            i = end
        else:
            i += 1
    return names


def find_cyclic(deps: dict[str, list[str]]) -> set[str]:
    """在自定义属性依赖图中找出所有处于循环中的变量名。"""
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {name: WHITE for name in deps}
    cyclic: set[str] = set()
    stack: list[str] = []

    def dfs(u: str) -> None:
        color[u] = GRAY
        stack.append(u)
        for v in deps.get(u, ()):
            if v not in deps:  # 引用继承值或未定义变量，不构成图内边
                continue
            if color[v] == GRAY:
                cyclic.update(stack[stack.index(v) :])
            elif color[v] == WHITE:
                dfs(v)
        stack.pop()
        color[u] = BLACK

    for name in deps:
        if color[name] == WHITE:
            dfs(name)
    return cyclic


def resolve_custom_properties(
    own: dict[str, str],
    inherited: dict[str, tuple[str, str | None]],
) -> tuple[dict[str, tuple[str, str | None]], dict[str, str]]:
    """解析一个元素上的自定义属性。

    own: 本元素层叠胜出的自定义属性（原始值，可能含 var()）。
    inherited: 父元素已解析的自定义属性，name -> (status, value)。
    返回 (resolved, notes)：resolved 同 inherited 结构；notes 记录失效原因。
    """
    resolved: dict[str, tuple[str, str | None]] = dict(inherited)
    notes: dict[str, str] = {}

    # 1. 循环依赖检测：循环中的自定义属性全部失效
    deps = {name: extract_var_names(value) for name, value in own.items()}
    cyclic = find_cyclic(deps)
    if cyclic:
        note = "循环依赖失效：" + " ↔ ".join(sorted(cyclic))
        for name in cyclic:
            resolved[name] = ("invalid", None)
            notes[name] = note

    # 2. 对其余本元素声明做 var() 替换（此时图已无环，可安全递归）
    memo: dict[str, tuple[str, str | None]] = {}

    def lookup(name: str) -> tuple[str, str | None]:
        if name in resolved:
            return resolved[name]
        if name in own:
            return resolve_own(name)
        return ("missing", None)

    def resolve_own(name: str) -> tuple[str, str | None]:
        if name in memo:
            return memo[name]
        try:
            memo[name] = ("ok", substitute_vars(own[name], lookup))
        except Unresolvable:
            memo[name] = ("invalid", None)
        return memo[name]

    for name in own:
        if name in cyclic:
            continue
        status, value = resolve_own(name)
        resolved[name] = (status, value)
        if status == "invalid":
            notes[name] = "var() 解析失败：引用的自定义属性缺失或已失效，且无 fallback"
    return resolved, notes
