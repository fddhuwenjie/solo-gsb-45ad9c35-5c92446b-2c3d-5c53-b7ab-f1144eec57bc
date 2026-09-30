"""样式变更预演的差异归因引擎。

基线快照与补丁快照各跑一遍 engine._build_snapshot，本模块负责：
  1. 只列出目标元素上发生变化的普通属性与自定义属性；
  2. 区分层叠胜出者变化（winner_change）、同一胜出声明被直接改值
     （value_edit）、同一胜出声明因 var() 依赖变化而改值
     （var_value_change）以及 computed/invalid 状态切换（status_switch）；
  3. 沿变量依赖边（含 fallback 改道、循环形成 / 解除、继承）给出从被改
     声明连到最终属性的因果轨迹；
  4. 未影响任何结果的补丁单独列为 shadowed。
"""
from __future__ import annotations

from .cascade import Candidate
from .engine import DepEdge, Snapshot
from .models import (
    CausalStepOut,
    ComputeRequest,
    CustomPropertyDiffOut,
    PropertyDiffOut,
    ShadowedPatchOut,
    StylePreviewOut,
)

# 每个差异最多给出多少条独立因果轨迹（每个被改声明一条）
_MAX_PATHS = 6


# ---------- 图工具 ----------


def _dedup_edges(edges: list[DepEdge]) -> list[DepEdge]:
    seen: set[tuple] = set()
    out: list[DepEdge] = []
    for e in edges:
        key = (e.src, e.dst, e.kind)
        if key not in seen:
            seen.add(key)
            out.append(e)
    return out


def _dedup_adjacency(edges: list[DepEdge]) -> dict[tuple, list[DepEdge]]:
    adj: dict[tuple, list[DepEdge]] = {}
    for e in _dedup_edges(edges):
        adj.setdefault(e.src, []).append(e)
    return adj


def _union_adjacency(base: Snapshot, patched: Snapshot) -> dict[tuple, list[DepEdge]]:
    """合并两快照的去重有向边（声明 -> 它引用的变量 / 最终属性）。"""
    adj: dict[tuple, list[DepEdge]] = {}
    for e in _dedup_edges(base.edges + patched.edges):
        adj.setdefault(e.src, []).append(e)
    return adj


def _candidate_id(c: Candidate) -> tuple[int, int, str]:
    return (c.rule_index, c.decl_index, c.property)


def _node_prop(node: tuple) -> str:
    """节点上的属性名：normal 节点为二元组，其余为 (kind, depth, prop)。"""
    return node[1] if node[0] == "normal" else node[2]


def _edge_detail(edge: DepEdge, element_labels: list[str]) -> str:
    depth = edge.depth
    label = element_labels[depth] if 0 <= depth < len(element_labels) else element_labels[-1]
    name = edge.dst[2]
    if edge.kind == "var":
        return f"{label}：var({name}) 取值成功，沿变量链继续"
    if edge.kind == "fallback":
        return f"{label}：var({name}) 引用的变量缺失 / 失效，改走 fallback"
    if edge.kind == "failed":
        return f"{label}：var({name}) 引用的变量缺失 / 失效，且无 fallback，声明随之失效"
    if edge.kind == "cycle":
        return f"{label}：var({name}) 使变量链进入循环（循环中的自定义属性失效）"
    return f"{label}：var({name}) 引用异常"


def _patch_summary(req: ComputeRequest, rule_index: int, decl_index: int) -> str:
    parts: list[str] = []
    for patch in req.patches:
        if patch.rule_index == rule_index and patch.declaration_index == decl_index:
            rule = req.rules[rule_index]
            decl = rule.declarations[decl_index]
            if "value" in patch.model_fields_set:
                parts.append(f"value: {decl.value!r} -> {patch.value!r}")
            if "important" in patch.model_fields_set:
                parts.append(f"important: {decl.important} -> {patch.important}")
            if patch.sets_layer:
                parts.append(f"layer: {rule.layer!r} -> {patch.layer!r}")
    return "补丁修改声明：" + "；".join(parts) if parts else (
        "该声明被补丁修改后经层叠成为胜出者 / 落选者"
    )


def _search_forward(
    adj: dict[tuple, list[DepEdge]],
    target: tuple,
    roots: set[tuple],
) -> tuple[dict[tuple, DepEdge], set[tuple]]:
    """自 target 沿依赖边正向（target 引用的变量 -> 更深层的变量提供节点）
    BFS，返回 parent 映射（parent[x] 为发现 x 时经过的边 e：e.dst == x）
    与命中的 roots。"""
    parent: dict[tuple, DepEdge] = {}
    hit: set[tuple] = set()
    queue = [target]
    visited = {target}
    head = 0
    while head < len(queue):
        cur = queue[head]
        head += 1
        if cur in roots:
            hit.add(cur)
        for e in adj.get(cur, ()):
            nxt = e.dst
            if nxt in visited:
                continue
            visited.add(nxt)
            parent[nxt] = e
            queue.append(nxt)
    return parent, hit


def _find_paths(
    base_adj: dict[tuple, list[DepEdge]],
    patched_adj: dict[tuple, list[DepEdge]],
    union_adj: dict[tuple, list[DepEdge]],
    targets: list[tuple],
    roots: set[tuple],
    base: Snapshot,
    patched: Snapshot,
    element_labels: list[str],
    req: ComputeRequest,
    direct_roots: list[dict] | None = None,
) -> list[list[CausalStepOut]]:
    """对每个 target，优先沿补丁后的依赖图正向回溯到被改声明（root），
    补丁图不可达时退回基线图、再退回合并图。每个可达 root 一条轨迹，
    顺序按 root 在 BFS 上的距离（近者在前）。

    direct_roots 给出不依赖图可达性的直接根（层叠易主的直接因果），每项为
    {"rule_index", "decl_index", "property", "element", "note_kind"}，
    note_kind ∈ {"new_winner", "old_winner"}。
    """
    direct_roots = direct_roots or []
    paths: list[list[CausalStepOut]] = []
    seen: set[tuple] = set()
    direct_decl_ids = {(s["rule_index"], s["decl_index"]) for s in direct_roots}

    # 直接根：被改声明 -> （层叠易主） -> 最终属性，无变量链
    for spec in direct_roots:
        key = ("direct", spec["rule_index"], spec["decl_index"], spec["property"])
        if key in seen:
            continue
        seen.add(key)
        if len(paths) >= _MAX_PATHS:
            break
        steps = [
            CausalStepOut(
                kind="patch",
                element=spec["element"],
                property=spec["property"],
                detail=_patch_summary(req, spec["rule_index"], spec["decl_index"]),
            )
        ]
        if spec["note_kind"] == "new_winner":
            if spec.get("cascade"):
                steps.append(
                    CausalStepOut(
                        kind="cascade",
                        element=element_labels[-1],
                        property=spec["property"],
                        detail="被改声明经层叠成为胜出者",
                    )
                )
        else:
            steps.append(
                CausalStepOut(
                    kind="cascade",
                    element=element_labels[-1],
                    property=spec["property"],
                    detail="被改声明经层叠失去胜位，胜出者改用另一声明",
                )
            )
        steps.append(
            CausalStepOut(
                kind="result",
                element=element_labels[-1],
                property=spec["property"],
                detail="传导至最终属性，状态 / 取值见差异字段",
            )
        )
        paths.append(steps)

    # 图根：沿变量依赖链回溯
    for target in targets:
        for adj in (patched_adj, base_adj, union_adj):
            parent, hit = _search_forward(adj, target, roots)
            if hit:
                break
        else:
            continue

        def depth_of(node: tuple) -> int:
            d = 0
            cur = node
            while cur in parent:
                cur = parent[cur].src
                d += 1
            return d

        for root in sorted(hit, key=depth_of):
            if root in seen:
                continue
            # 该根的胜出声明已有直接轨迹（层叠易主 / 直接改值），不重复给图路径
            root_cn = (
                patched.custom_nodes.get(root)
                or base.custom_nodes.get(root)
                if root[0] == "custom"
                else None
            )
            root_entry = (
                patched.normal.get(root[1]) or base.normal.get(root[1])
                if root[0] == "normal"
                else None
            )
            root_decl = (
                (root_cn.winner.rule_index, root_cn.winner.decl_index)
                if root_cn
                else (root_entry.winner.rule_index, root_entry.winner.decl_index)
                if root_entry
                else None
            )
            if root_decl in direct_decl_ids:
                continue
            seen.add(root)
            if len(paths) >= _MAX_PATHS:
                break

            steps: list[CausalStepOut] = []
            if root[0] == "custom":
                cn = patched.custom_nodes.get(root) or base.custom_nodes.get(root)
                ri = cn.winner.rule_index if cn else root[1]
                di = cn.winner.decl_index if cn else 0
                depth = root[1]
                steps.append(
                    CausalStepOut(
                        kind="patch",
                        element=element_labels[depth],
                        property=root[2],
                        detail=_patch_summary(req, ri, di),
                    )
                )
            else:  # normal 节点
                entry = patched.normal.get(root[1]) or base.normal.get(root[1])
                ri = entry.winner.rule_index if entry else 0
                di = entry.winner.decl_index if entry else 0
                steps.append(
                    CausalStepOut(
                        kind="patch",
                        element=element_labels[-1],
                        property=root[1],
                        detail=_patch_summary(req, ri, di),
                    )
                )

            # parent[x] 是 x <- e.src 的引用边：自 root 向 target 回放
            cur = root
            target_set = set(targets)
            while cur not in target_set:
                e = parent.get(cur)
                if e is None:
                    break
                steps.append(
                    CausalStepOut(
                        kind=e.kind,
                        element=element_labels[e.depth]
                        if 0 <= e.depth < len(element_labels)
                        else element_labels[-1],
                        property=_node_prop(e.src),
                        detail=_edge_detail(e, element_labels),
                    )
                )
                cur = e.src

            steps.append(
                CausalStepOut(
                    kind="result",
                    element=element_labels[cur[1]]
                    if cur[0] != "normal"
                    else element_labels[-1],
                    property=_node_prop(cur),
                    detail="传导至最终属性，状态 / 取值见差异字段",
                )
            )
            paths.append(steps)
    return paths


# ---------- 胜出者与补丁 ----------


def _normal_winner_node(snap: Snapshot, prop: str) -> tuple:
    return ("normal", prop)


def _custom_target_node(snap: Snapshot, prop: str) -> tuple | None:
    status, _value, depth = snap.target_custom.get(prop, ("missing", None, None))
    if depth is None:
        return None
    node = ("custom", depth, prop)
    return node if node in snap.custom_nodes else None


# ---------- 差异主流程 ----------


def build_preview(
    req: ComputeRequest, base: Snapshot, patched: Snapshot
) -> StylePreviewOut:
    element_labels: list[str] = []
    from .engine import _element_label

    for el in list(req.ancestors) + [req.element]:
        element_labels.append(_element_label(el))
    target_label = element_labels[-1]

    base_adj = _dedup_adjacency(base.edges)
    patched_adj = _dedup_adjacency(patched.edges)
    union_adj = _union_adjacency(base, patched)
    patch_ids = {(pt.rule_index, pt.declaration_index) for pt in req.patches}

    def patched_winner_nodes(snap: Snapshot) -> set[tuple]:
        """快照中胜出声明恰好是补丁目标的节点（即因果轨迹允许的根）。"""
        nodes: set[tuple] = set()
        for nid, cn in snap.custom_nodes.items():
            if (cn.winner.rule_index, cn.winner.decl_index) in patch_ids:
                nodes.add(nid)
        for prop, entry in snap.normal.items():
            if (entry.winner.rule_index, entry.winner.decl_index) in patch_ids:
                nodes.add(("normal", prop))
        return nodes

    roots = patched_winner_nodes(patched) | patched_winner_nodes(base)

    prop_diffs: list[PropertyDiffOut] = []
    used_roots: set[tuple] = set()
    used_direct_patches: set[tuple[int, int]] = set()

    all_props = sorted(set(base.normal) | set(patched.normal))
    for prop in all_props:
        b = base.normal.get(prop)
        p = patched.normal.get(prop)
        if b is None or p is None:
            # 候选集不因补丁改变（补丁不改选择器），理论上两侧一致
            continue
        same_winner = _candidate_id(b.winner) == _candidate_id(p.winner)
        if same_winner and b.status == p.status and b.value == p.value:
            continue

        if b.status != p.status:
            diff_type = "status_switch"
        elif not same_winner:
            diff_type = "winner_change"
        elif (p.winner.rule_index, p.winner.decl_index) in patch_ids and _value_of_patch(
            req, p.winner
        ) is not None:
            diff_type = "value_edit"
        else:
            diff_type = "var_value_change"

        bnode = _normal_winner_node(base, prop)
        pnode = _normal_winner_node(patched, prop)
        targets = [pnode]
        diff_roots = set()
        direct_roots: list[dict] = []
        note_parts: list[str] = []
        if diff_type in ("winner_change", "status_switch"):
            # 新 / 旧胜出声明被补丁直接修改 => 层叠易主的直接根（不经变量图）；
            # 胜出声明自身未被改时，沿其 var() 依赖边追溯到被改的自定义属性。
            if (p.winner.rule_index, p.winner.decl_index) in patch_ids:
                direct_roots.append(
                    {
                        "rule_index": p.winner.rule_index,
                        "decl_index": p.winner.decl_index,
                        "property": prop,
                        "element": element_labels[-1],
                        "note_kind": "new_winner",
                        "cascade": not same_winner,
                    }
                )
            # 无论新胜出声明是否被直接修改，都沿补丁图追溯其 var() 依赖上
            # 被改的自定义属性（可与直接根共同给出多条因果轨迹）
            diff_roots |= _reachable_roots(patched_adj, [pnode], roots - {pnode})
            if (b.winner.rule_index, b.winner.decl_index) in patch_ids:
                direct_roots.append(
                    {
                        "rule_index": b.winner.rule_index,
                        "decl_index": b.winner.decl_index,
                        "property": prop,
                        "element": element_labels[-1],
                        "note_kind": "old_winner",
                        "cascade": not same_winner,
                    }
                )
            else:
                diff_roots |= _reachable_roots(base_adj, [bnode], roots - {bnode})
            if not same_winner:
                note_parts.append(
                    f"胜出声明：{b.winner.selector}[{b.winner.value!r}] -> "
                    f"{p.winner.selector}[{p.winner.value!r}]"
                )
        elif diff_type == "value_edit":
            direct_roots.append(
                {
                    "rule_index": p.winner.rule_index,
                    "decl_index": p.winner.decl_index,
                    "property": prop,
                    "element": element_labels[-1],
                    "note_kind": "new_winner",
                    "cascade": False,
                }
            )
        else:
            diff_roots |= _reachable_roots(patched_adj, targets, roots)
            note_parts.append("胜出声明未变，其 var() 依赖链上的自定义属性发生变化")

        paths = _find_paths(
            base_adj,
            patched_adj,
            union_adj,
            targets,
            diff_roots,
            base,
            patched,
            element_labels,
            req,
            direct_roots=direct_roots,
        )
        used_roots |= diff_roots
        used_direct_patches.update(
            (dr["rule_index"], dr["decl_index"]) for dr in direct_roots
        )
        prop_diffs.append(
            PropertyDiffOut(
                property=prop,
                diff_type=diff_type,
                baseline_status=b.status,
                patched_status=p.status,
                baseline_value=b.value,
                patched_value=p.value,
                baseline_winner=b.winner.to_out(),
                patched_winner=p.winner.to_out(),
                causal_paths=paths,
                note="；".join(note_parts),
            )
        )

    custom_diffs: list[CustomPropertyDiffOut] = []
    custom_names = sorted(set(base.target_custom) | set(patched.target_custom))
    for name in custom_names:
        bs, bv, bd = base.target_custom.get(name, ("missing", None, None))
        ps, pv, pd_ = patched.target_custom.get(name, ("missing", None, None))
        bnode = _custom_target_node(base, name)
        pnode = _custom_target_node(patched, name)
        bwin = base.custom_nodes[bnode].winner if bnode else None
        pwin = patched.custom_nodes[pnode].winner if pnode else None
        same_winner = (
            bwin is not None
            and pwin is not None
            and _candidate_id(bwin) == _candidate_id(pwin)
        )
        if bs == ps and bv == pv and same_winner:
            continue

        targets = [n for n in (pnode, bnode) if n is not None]
        if bs != ps:
            diff_type = "status_switch"
        elif not same_winner:
            diff_type = "winner_change"
        elif (
            pnode is not None
            and (pwin.rule_index, pwin.decl_index) in patch_ids
            and _value_of_patch(req, pwin) is not None
        ):
            diff_type = "value_edit"
        else:
            diff_type = "var_value_change"

        diff_roots: set[tuple] = set()
        direct_roots: list[dict] = []
        note_parts: list[str] = []

        def custom_root_spec(winner, depth: int, note_kind: str, cascade: bool) -> dict:
            return {
                "rule_index": winner.rule_index,
                "decl_index": winner.decl_index,
                "property": name,
                "element": element_labels[depth],
                "note_kind": note_kind,
                "cascade": cascade,
            }

        if diff_type == "value_edit":
            direct_roots.append(custom_root_spec(pwin, pnode[1], "new_winner", False))
        elif diff_type in ("winner_change", "status_switch"):
            if pnode is not None and (pwin.rule_index, pwin.decl_index) in patch_ids:
                direct_roots.append(
                    custom_root_spec(pwin, pnode[1], "new_winner", not same_winner)
                )
            if pnode is not None:
                diff_roots |= _reachable_roots(
                    patched_adj, [pnode], roots - {pnode}
                )
            if bnode is not None and (bwin.rule_index, bwin.decl_index) in patch_ids:
                direct_roots.append(
                    custom_root_spec(bwin, bnode[1], "old_winner", not same_winner)
                )
            elif bnode is not None:
                diff_roots |= _reachable_roots(base_adj, [bnode], roots - {bnode})
            if not same_winner and bwin and pwin:
                note_parts.append(
                    f"胜出声明：{bwin.selector}[{bwin.value!r}] -> "
                    f"{pwin.selector}[{pwin.value!r}]"
                )
        else:
            diff_roots |= _reachable_roots(patched_adj, targets, roots)
            diff_roots |= _reachable_roots(base_adj, targets, roots)
            note_parts.append("胜出声明未变，其 var() 依赖链（含循环 / fallback）发生变化")

        if diff_type == "status_switch":
            note_parts.append(f"状态切换：{bs} -> {ps}")

        paths = _find_paths(
            base_adj,
            patched_adj,
            union_adj,
            targets,
            diff_roots,
            base,
            patched,
            element_labels,
            req,
            direct_roots=direct_roots,
        )
        used_roots |= diff_roots
        used_direct_patches.update(
            (dr["rule_index"], dr["decl_index"]) for dr in direct_roots
        )
        custom_diffs.append(
            CustomPropertyDiffOut(
                property=name,
                element=target_label,
                diff_type=diff_type,
                baseline_status=bs,
                patched_status=ps,
                baseline_value=bv,
                patched_value=pv,
                baseline_winner=bwin.to_out() if bwin else None,
                patched_winner=pwin.to_out() if pwin else None,
                causal_paths=paths,
                note="；".join(note_parts),
            )
        )

    shadowed = _shadowed_patches(
        req, base, patched, used_roots, used_direct_patches
    )

    return StylePreviewOut(
        baseline=base.response,
        patched=patched.response,
        property_diffs=prop_diffs,
        custom_property_diffs=custom_diffs,
        shadowed_patches=shadowed,
    )


def _value_of_patch(req: ComputeRequest, winner: Candidate) -> str | None:
    """若胜出声明对应补丁携带 value 修改，返回该新值，否则 None。"""
    for pt in req.patches:
        if pt.rule_index == winner.rule_index and pt.declaration_index == winner.decl_index:
            if "value" in pt.model_fields_set:
                return pt.value
    return None


def _reachable_roots(
    adj: dict[tuple, list[DepEdge]],
    targets: list[tuple],
    roots: set[tuple],
) -> set[tuple]:
    """从 targets 沿依赖边正向能到达的 roots。"""
    hit: set[tuple] = set()
    visited: set[tuple] = set()
    queue = list(targets)
    visited.update(targets)
    head = 0
    while head < len(queue):
        cur = queue[head]
        head += 1
        if cur in roots:
            hit.add(cur)
        for e in adj.get(cur, ()):
            nxt = e.dst
            if nxt in visited:
                continue
            visited.add(nxt)
            queue.append(nxt)
    return hit


def _shadowed_patches(
    req: ComputeRequest,
    base: Snapshot,
    patched: Snapshot,
    used_roots: set[tuple],
    used_direct_patches: set[tuple[int, int]],
) -> list[ShadowedPatchOut]:
    """未出现在任何差异因果轨迹中的补丁。"""
    used_decl_ids: set[tuple[int, int]] = set(used_direct_patches)
    for node in used_roots:
        if node[0] == "custom":
            # 两个快照都查一次（BFS 用的是合并图，胜出可能在任一侧）
            for snap in (base, patched):
                cn = snap.custom_nodes.get(node)
                if cn:
                    used_decl_ids.add((cn.winner.rule_index, cn.winner.decl_index))
        elif node[0] == "normal":
            for snap in (base, patched):
                entry = snap.normal.get(node[1])
                if entry:
                    used_decl_ids.add((entry.winner.rule_index, entry.winner.decl_index))

    out: list[ShadowedPatchOut] = []
    for patch in req.patches:
        ri, di = patch.rule_index, patch.declaration_index
        if (ri, di) in used_decl_ids:
            continue
        decl = req.rules[ri].declarations[di]
        wins = _decl_wins(base, ri, di) or _decl_wins(patched, ri, di)
        if wins:
            reason = "声明在层叠中胜出，但修改未改变任何最终属性的状态或取值"
        else:
            reason = "声明在层叠中落选，修改不影响胜出结果"
        changes: dict[str, object] = {}
        if "value" in patch.model_fields_set:
            changes["value"] = patch.value
        if "important" in patch.model_fields_set:
            changes["important"] = patch.important
        if patch.sets_layer:
            changes["layer"] = patch.layer
        out.append(
            ShadowedPatchOut(
                rule_index=ri,
                declaration_index=di,
                property=decl.property,
                changes=changes,
                reason=reason,
            )
        )
    return out


def _decl_wins(snap: Snapshot, rule_index: int, decl_index: int) -> bool:
    for cn in snap.custom_nodes.values():
        if cn.winner.rule_index == rule_index and cn.winner.decl_index == decl_index:
            return True
    for entry in snap.normal.values():
        if entry.winner.rule_index == rule_index and entry.winner.decl_index == decl_index:
            return True
    return False
