"""计算引擎：收集候选声明 -> 层叠 -> 自定义属性继承与 var() 解析 -> 生成证据。

样式预演（patches）在同一编排上各跑一次「基线快照」与「补丁快照」，快照除
响应模型外还记录变量依赖边，供 preview.py 做差异归因与因果轨迹。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .cascade import Candidate, cascade
from .models import (
    ComputeRequest,
    ComputeResponse,
    CustomPropTraceOut,
    Declaration,
    ElementInfo,
    InvalidDeclOut,
    PreviewComputeResponse,
    PropertyEvidenceOut,
    Rule,
)
from .selector import parse_selector, selector_matches, selector_specificity
from .variables import (
    Unresolvable,
    Query,
    resolve_custom_properties_traced,
    substitute_vars,
)


class PatchError(ValueError):
    """补丁定位非法或内容非法：整次预演 422，不产生任何部分结果。"""


def _resolve_layer_order(declared: list[str], rules, layer_overrides=None) -> dict[str, int]:
    """层声明顺序；规则或声明级补丁引用但未声明的层按首次出现顺序追加到末尾。"""
    order = list(declared)
    for ri, rule in enumerate(rules):
        layers_on_rule = [rule.layer]
        if layer_overrides:
            layers_on_rule.extend(
                layer for (ori, _di), layer in layer_overrides.items() if ori == ri
            )
        for layer in layers_on_rule:
            if layer is not None and layer not in order:
                order.append(layer)
    return {name: i for i, name in enumerate(order)}


def _element_label(el: ElementInfo) -> str:
    label = el.tag or "*"
    if el.id:
        label += f"#{el.id}"
    for cls in el.classes:
        label += f".{cls}"
    return label


# ---------- 补丁定位与一次性校验 ----------


def validate_patches(req: ComputeRequest) -> None:
    """在任何计算之前校验全部补丁：任一定位非法即抛 PatchError（-> 422）。"""
    seen: set[tuple[int, int]] = set()
    for pi, patch in enumerate(req.patches):
        where = f"patches[{pi}]（rule_index={patch.rule_index}, declaration_index={patch.declaration_index}）"
        if patch.rule_index < 0 or patch.declaration_index < 0:
            raise PatchError(f"{where} 非法：规则 / 声明下标不能为负")
        if patch.rule_index >= len(req.rules):
            raise PatchError(
                f"{where} 非法：rules 下标越界（共 {len(req.rules)} 条规则）"
            )
        rule = req.rules[patch.rule_index]
        if patch.declaration_index >= len(rule.declarations):
            raise PatchError(
                f"{where} 非法：规则 {patch.rule_index}（选择器 {rule.selector!r}）"
                f"只有 {len(rule.declarations)} 条声明"
            )
        if (patch.rule_index, patch.declaration_index) in seen:
            raise PatchError(f"{where} 重复：同一位置只能携带一条补丁")
        seen.add((patch.rule_index, patch.declaration_index))
        if not patch.at_least_one_change():
            raise PatchError(f"{where} 非法：补丁至少要给出 value / important / layer 之一")
        if patch.sets_layer and patch.layer is not None and not patch.layer.strip():
            raise PatchError(f"{where} 非法：layer 不能为空白字符串（置 null 表示未分层）")


def apply_patches(req: ComputeRequest):
    """返回 (patched_rules, layer_overrides)。

    patched_rules 为深拷贝并已套用 value / important 修改；声明级 layer 改动
    不写回规则，而通过 layer_overrides: (rule_index, decl_index) -> layer|None
    在构造候选时注入（同一条规则内的声明可以属于不同层）。
    """
    import copy

    patched_rules = copy.deepcopy(req.rules)
    layer_overrides: dict[tuple[int, int], str | None] = {}
    for patch in req.patches:
        decl: Declaration = patched_rules[patch.rule_index].declarations[
            patch.declaration_index
        ]
        if "value" in patch.model_fields_set:
            decl.value = patch.value
        if "important" in patch.model_fields_set:
            decl.important = patch.important
        if patch.sets_layer:
            layer_overrides[(patch.rule_index, patch.declaration_index)] = patch.layer
    return patched_rules, layer_overrides


# ---------- 快照（基线 / 补丁共用） ----------


@dataclass
class CustomNode:
    """元素链上一个生效的自定义属性声明（本元素层叠胜出者）。"""

    depth: int
    property: str
    status: str  # ok | invalid
    value: str | None
    winner: Candidate
    cyclic: bool


@dataclass
class DepEdge:
    """一条变量依赖边（from 为发起声明的节点标识，to 为被引用变量的
    提供节点标识），同时记录 fallback 是否改道与元素深度。

    节点标识：("custom", depth, property) 为某元素自定义属性胜出节点；
    ("normal", property) 为目标元素上的普通属性声明；
    ("missing", depth, name) 为引用未定义变量的终点占位。
    """

    src: tuple
    dst: tuple
    kind: str  # var | fallback | failed | cycle
    depth: int


@dataclass
class NormalEntry:
    property: str
    status: str  # computed | invalid
    value: str | None
    winner: Candidate


@dataclass
class Snapshot:
    response: ComputeResponse
    custom_nodes: dict[tuple, CustomNode]
    # 目标元素上每个自定义属性的最终状态：name -> (status, value, provide_depth|None)
    target_custom: dict[str, tuple[str, str | None, int | None]]
    normal: dict[str, NormalEntry]
    edges: list[DepEdge] = field(default_factory=list)


def _build_snapshot(
    element: ElementInfo,
    ancestors: list[ElementInfo],
    declared_layers: list[str],
    rules: list[Rule],
    layer_overrides: dict[tuple[int, int], str | None] | None = None,
) -> Snapshot:
    layer_overrides = layer_overrides or {}
    layer_index = _resolve_layer_order(declared_layers, rules, layer_overrides)

    # 预解析所有规则的选择器
    parsed = []
    for idx, rule in enumerate(rules):
        compounds, combinators = parse_selector(rule.selector)
        spec = selector_specificity(compounds)
        order = rule.order if rule.order is not None else idx
        parsed.append((rule, order, compounds, combinators, spec))

    def decl_layer(ri: int, di: int, rule_layer: str | None) -> str | None:
        return layer_overrides.get((ri, di), rule_layer)

    def candidates_for(element: ElementInfo, ancestors: list[ElementInfo], custom: bool):
        result: list[Candidate] = []
        for ri, (rule, order, compounds, combinators, spec) in enumerate(parsed):
            if not selector_matches(compounds, combinators, element, ancestors):
                continue
            for di, decl in enumerate(rule.declarations):
                is_custom = decl.property.startswith("--")
                if is_custom != custom:
                    continue
                effective_layer = decl_layer(ri, di, rule.layer)
                result.append(
                    Candidate(
                        selector=rule.selector,
                        origin=rule.origin,
                        layer=effective_layer,
                        layer_index=(
                            layer_index.get(effective_layer)
                            if effective_layer is not None
                            else None
                        ),
                        important=decl.important,
                        specificity=spec,
                        source_order=order,
                        decl_index=di,
                        property=decl.property if is_custom else decl.property.lower(),
                        value=decl.value,
                        rule_index=ri,
                    )
                )
        return result

    chain = list(ancestors) + [element]

    # ---- 自定义属性：沿祖先链逐元素层叠、继承并解析 var()，同时记录依赖 ----
    inherited: dict[str, tuple[str, str | None]] = {}
    provide_depth: dict[str, int] = {}  # 当前 resolved 值的提供深度（本元素覆盖则更新）
    custom_trace: list[CustomPropTraceOut] = []
    custom_nodes: dict[tuple, CustomNode] = {}
    edges: list[DepEdge] = []

    for depth, el in enumerate(chain):
        winners = cascade(candidates_for(el, chain[:depth], custom=True))
        own = {prop: winner.value for prop, (winner, _) in winners.items()}
        own_winners = {prop: winner for prop, (winner, _) in winners.items()}
        resolved, notes, cyclic, queries, values = resolve_custom_properties_traced(
            own, inherited
        )

        # 本元素胜出节点
        for prop, winner in own_winners.items():
            status, value = resolved[prop]
            node_id = ("custom", depth, prop)
            custom_nodes[node_id] = CustomNode(
                depth=depth,
                property=prop,
                status=status,
                value=value if status == "ok" else None,
                winner=winner,
                cyclic=prop in cyclic,
            )

        # 依据 queries 建立依赖边：引用本元素胜出的变量 -> 本元素 custom 节点；
        # 引用继承变量 -> 直接连到提供该值的祖先 custom 节点（缺失无目标）。
        for q in queries:
            src = ("custom", depth, q.from_prop)
            if q.name == "<syntax>":
                continue
            if q.name in own_winners:
                dst = ("custom", depth, q.name)
            elif q.name in inherited and q.name in provide_depth:
                dst = ("custom", provide_depth[q.name], q.name)
            else:
                dst = None
            if dst is None:
                if q.status != "ok":
                    edges.append(DepEdge(src, ("missing", depth, q.name), "failed", depth))
                continue
            if q.name in own_winners and q.name in cyclic:
                edges.append(DepEdge(src, dst, "cycle", depth))
            elif q.status != "ok":
                # 被引用变量失效（自身解析失败等），本引用靠 fallback 兜底或随之失效
                edges.append(
                    DepEdge(src, dst, "fallback" if q.fallback else "failed", depth)
                )
            else:
                edges.append(DepEdge(src, dst, "var", depth))

        label = _element_label(el)
        for prop in sorted(resolved):
            if prop in own:
                winner, eliminated = winners[prop]
                status, value = resolved[prop]
                custom_trace.append(
                    CustomPropTraceOut(
                        element=label,
                        property=prop,
                        status="ok" if status == "ok" else "invalid",
                        declared_value=winner.value,
                        resolved=value if status == "ok" else None,
                        note=notes.get(prop, "本元素层叠胜出"),
                        winner=winner.to_out(),
                        eliminated=eliminated,
                    )
                )
            else:
                status, value = resolved[prop]
                custom_trace.append(
                    CustomPropTraceOut(
                        element=label,
                        property=prop,
                        status="inherited",
                        resolved=value if status == "ok" else None,
                        note="继承自祖先" if status == "ok" else "继承自祖先（已失效）",
                    )
                )

        # 更新提供深度：本元素胜出（含失效）即由本元素提供
        for prop in own_winners:
            provide_depth[prop] = depth
        inherited = resolved

    target_depth = len(chain) - 1
    target_custom: dict[str, tuple[str, str | None, int | None]] = {
        prop: (status, value if status == "ok" else None, provide_depth.get(prop))
        for prop, (status, value) in inherited.items()
    }

    def lookup(name: str):
        return inherited.get(name, ("missing", None))

    # ---- 普通属性：仅在目标元素上层叠，再做 var() 替换 ----
    normal_winners = cascade(
        candidates_for(element, ancestors, custom=False)
    )
    computed: dict[str, str | None] = {}
    invalid: list[InvalidDeclOut] = []
    evidence: list[PropertyEvidenceOut] = []
    normal: dict[str, NormalEntry] = {}

    for prop in sorted(normal_winners):
        winner, eliminated = normal_winners[prop]
        steps: list[str] = []
        normal_queries: list[Query] = []

        def on_query(qname: str, status: str, used_fallback: bool) -> None:
            normal_queries.append(Query(qname, status, used_fallback, prop))

        try:
            value = (
                substitute_vars(winner.value, lookup, steps, on_query)
                if "var(" in winner.value
                else winner.value
            )
            computed[prop] = value
            evidence.append(
                PropertyEvidenceOut(
                    property=prop,
                    status="computed",
                    computed_value=value,
                    winner=winner.to_out(),
                    var_steps=steps,
                    eliminated=eliminated,
                )
            )
            normal[prop] = NormalEntry(prop, "computed", value, winner)
        except Unresolvable as exc:
            computed[prop] = None
            if exc.status == "missing":
                reason = f"var() 引用的自定义属性 {exc.name} 未定义，且声明没有 fallback"
            elif exc.status == "syntax":
                reason = "var() 语法错误，且声明没有可用 fallback"
            else:
                reason = (
                    f"var() 引用的自定义属性 {exc.name} 已失效（循环依赖或自身解析失败），"
                    "且声明没有 fallback"
                )
            invalid.append(InvalidDeclOut(property=prop, value=winner.value, reason=reason))
            evidence.append(
                PropertyEvidenceOut(
                    property=prop,
                    status="invalid",
                    computed_value=None,
                    winner=winner.to_out(),
                    var_steps=steps,
                    eliminated=eliminated,
                )
            )
            normal[prop] = NormalEntry(prop, "invalid", None, winner)

        src = ("normal", prop)
        for q in normal_queries:
            if q.name == "<syntax>":
                continue
            pd = provide_depth.get(q.name) if q.name in inherited else None
            if pd is not None and ("custom", pd, q.name) in custom_nodes:
                dst = ("custom", pd, q.name)
            elif q.name in inherited:
                # 继承到的是祖先链上没有本元素胜出节点的值（当前实现下不会发生，
                # 兜底为失败边）
                dst = ("missing", target_depth, q.name)
            else:
                dst = ("missing", target_depth, q.name)
            if dst[0] == "missing":
                edges.append(DepEdge(src, dst, "failed", target_depth))
            elif q.status != "ok":
                edges.append(
                    DepEdge(src, dst, "fallback" if q.fallback else "failed", target_depth)
                )
            else:
                edges.append(DepEdge(src, dst, "var", target_depth))

    response = ComputeResponse(
        computed=computed,
        custom_properties={
            name: val
            for name, (status, val, _d) in target_custom.items()
            if status == "ok"
        },
        invalid=invalid,
        evidence=evidence,
        custom_property_trace=custom_trace,
    )
    return Snapshot(
        response=response,
        custom_nodes=custom_nodes,
        target_custom=target_custom,
        normal=normal,
        edges=edges,
    )


# ---------- 入口 ----------


def compute(req: ComputeRequest) -> ComputeResponse | PreviewComputeResponse:
    if not req.patches:
        snap = _build_snapshot(req.element, req.ancestors, req.layers, req.rules)
        return snap.response

    validate_patches(req)  # 全部定位合法后才开始任何计算
    baseline = _build_snapshot(req.element, req.ancestors, req.layers, req.rules)
    patched_rules, layer_overrides = apply_patches(req)
    patched_layer_order = list(req.layers)
    for ri, rule in enumerate(patched_rules):
        for di in range(len(rule.declarations)):
            layer = layer_overrides.get((ri, di), rule.layer)
            if layer is not None and layer not in patched_layer_order:
                patched_layer_order.append(layer)
    patched = _build_snapshot(
        req.element,
        req.ancestors,
        patched_layer_order,
        patched_rules,
        layer_overrides,
    )

    from .preview import build_preview

    preview = build_preview(req, baseline, patched)
    return PreviewComputeResponse(
        **baseline.response.model_dump(),
        preview=preview,
    )
