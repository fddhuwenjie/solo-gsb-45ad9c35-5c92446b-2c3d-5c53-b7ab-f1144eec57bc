"""计算引擎：收集候选声明 -> 层叠 -> 自定义属性继承与 var() 解析 -> 生成证据。"""
from __future__ import annotations

from .cascade import Candidate, cascade
from .models import (
    ComputeRequest,
    ComputeResponse,
    CustomPropTraceOut,
    ElementInfo,
    InvalidDeclOut,
    PropertyEvidenceOut,
)
from .selector import parse_selector, selector_matches, selector_specificity
from .variables import Unresolvable, resolve_custom_properties, substitute_vars


def _resolve_layer_order(declared: list[str], rules) -> dict[str, int]:
    """层声明顺序；规则中引用但未声明的层按首次出现顺序追加到末尾。"""
    order = list(declared)
    for rule in rules:
        if rule.layer is not None and rule.layer not in order:
            order.append(rule.layer)
    return {name: i for i, name in enumerate(order)}


def _element_label(el: ElementInfo) -> str:
    label = el.tag or "*"
    if el.id:
        label += f"#{el.id}"
    for cls in el.classes:
        label += f".{cls}"
    return label


def compute(req: ComputeRequest) -> ComputeResponse:
    layer_index = _resolve_layer_order(req.layers, req.rules)

    # 预解析所有规则的选择器
    parsed = []
    for idx, rule in enumerate(req.rules):
        compounds, combinators = parse_selector(rule.selector)
        spec = selector_specificity(compounds)
        order = rule.order if rule.order is not None else idx
        parsed.append((rule, order, compounds, combinators, spec))

    def candidates_for(element: ElementInfo, ancestors: list[ElementInfo], custom: bool):
        result: list[Candidate] = []
        for rule, order, compounds, combinators, spec in parsed:
            if not selector_matches(compounds, combinators, element, ancestors):
                continue
            for di, decl in enumerate(rule.declarations):
                is_custom = decl.property.startswith("--")
                if is_custom != custom:
                    continue
                result.append(
                    Candidate(
                        selector=rule.selector,
                        origin=rule.origin,
                        layer=rule.layer,
                        layer_index=layer_index.get(rule.layer) if rule.layer is not None else None,
                        important=decl.important,
                        specificity=spec,
                        source_order=order,
                        decl_index=di,
                        property=decl.property if is_custom else decl.property.lower(),
                        value=decl.value,
                    )
                )
        return result

    # ---- 自定义属性：沿祖先链逐元素层叠、继承并解析 var() ----
    chain = list(req.ancestors) + [req.element]
    inherited: dict[str, tuple[str, str | None]] = {}
    custom_trace: list[CustomPropTraceOut] = []
    for depth, el in enumerate(chain):
        winners = cascade(candidates_for(el, chain[:depth], custom=True))
        own = {prop: winner.value for prop, (winner, _) in winners.items()}
        resolved, notes = resolve_custom_properties(own, inherited)
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
        inherited = resolved

    element_custom = inherited

    def lookup(name: str):
        return element_custom.get(name, ("missing", None))

    # ---- 普通属性：仅在目标元素上层叠，再做 var() 替换 ----
    winners = cascade(candidates_for(req.element, req.ancestors, custom=False))
    computed: dict[str, str | None] = {}
    invalid: list[InvalidDeclOut] = []
    evidence: list[PropertyEvidenceOut] = []
    for prop in sorted(winners):
        winner, eliminated = winners[prop]
        steps: list[str] = []
        try:
            value = (
                substitute_vars(winner.value, lookup, steps)
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

    return ComputeResponse(
        computed=computed,
        custom_properties={
            name: value for name, (status, value) in element_custom.items() if status == "ok"
        },
        invalid=invalid,
        evidence=evidence,
        custom_property_trace=custom_trace,
    )
