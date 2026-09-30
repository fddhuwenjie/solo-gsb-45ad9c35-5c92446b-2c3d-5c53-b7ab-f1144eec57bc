"""端到端测试：POST /compute 的样式变更预演（patches）。

覆盖：
- 旧请求（无 patches / 空 patches）响应不带 preview 键，结构兼容；
- 补丁定位校验一次性生效：任一下标非法整次 422、无部分结果；
- 四类差异：winner_change / value_edit / var_value_change / status_switch；
- 循环形成 / 解除、fallback 改道、祖先继承链的因果轨迹；
- 未影响结果的补丁列入 shadowed_patches。
"""
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

P = {"tag": "p"}


def compute(payload, expected=200):
    resp = client.post("/compute", json=payload)
    assert resp.status_code == expected, resp.text
    return resp.json()


def base_payload(**overrides):
    payload = {"element": P, "ancestors": [], "layers": [], "rules": []}
    payload.update(overrides)
    return payload


def rule(selector, *decls, origin="author", layer=None, order=None):
    r = {
        "selector": selector,
        "origin": origin,
        "layer": layer,
        "declarations": [
            {"property": p, "value": v, "important": imp} for p, v, imp in decls
        ],
    }
    if order is not None:
        r["order"] = order
    return r


def decl(prop, value, important=False):
    return (prop, value, important)


def diff_by(data, prop, key="property_diffs"):
    for d in data["preview"][key]:
        if d["property"] == prop:
            return d
    raise AssertionError(f"no diff for {prop}")


def path_kinds(diff):
    return [[s["kind"] for s in path] for path in diff["causal_paths"]]


# ---------- 兼容性 ----------


def test_legacy_request_without_patches_has_no_preview():
    data = compute(
        base_payload(rules=[rule("p", decl("color", "red"))])
    )
    assert "preview" not in data
    assert data["computed"]["color"] == "red"


def test_empty_patches_has_no_preview():
    data = compute(base_payload(patches=[], rules=[rule("p", decl("color", "red"))]))
    assert "preview" not in data


# ---------- 422：定位一次性校验 ----------


def check_422(patches):
    resp = client.post(
        "/compute",
        json=base_payload(
            rules=[
                rule("p", decl("color", "red"), decl("margin", "0")),
                rule("p", decl("padding", "1px")),
            ],
            patches=patches,
        ),
    )
    assert resp.status_code == 422, resp.text
    return resp.json()["detail"]


def test_rule_index_out_of_range_422():
    detail = check_422([{"rule_index": 2, "declaration_index": 0, "value": "x"}])
    assert "下标越界" in detail


def test_declaration_index_out_of_range_422():
    detail = check_422([{"rule_index": 0, "declaration_index": 2, "value": "x"}])
    assert "只有 2 条声明" in detail


def test_negative_indices_422():
    assert "不能为负" in str(check_422([{"rule_index": -1, "declaration_index": 0, "value": "x"}]))
    assert "不能为负" in str(check_422([{"rule_index": 0, "declaration_index": -1, "value": "x"}]))


def test_duplicate_location_422():
    detail = check_422(
        [
            {"rule_index": 0, "declaration_index": 0, "value": "x"},
            {"rule_index": 0, "declaration_index": 0, "important": True},
        ]
    )
    assert "重复" in detail


def test_second_patch_invalid_makes_whole_request_422():
    # 第一条补丁合法、第二条定位非法：整次 422，不产生任何（部分）结果
    detail = check_422(
        [
            {"rule_index": 0, "declaration_index": 0, "value": "x"},
            {"rule_index": 9, "declaration_index": 0, "value": "y"},
        ]
    )
    assert "patches[1]" in detail


def test_patch_without_any_change_422():
    assert "至少要给出" in str(check_422([{"rule_index": 0, "declaration_index": 0}]))


def test_non_integer_index_422():
    check_422([{"rule_index": 0.5, "declaration_index": 0, "value": "x"}])
    check_422([{"rule_index": True, "declaration_index": 0, "value": "x"}])


def test_non_bool_important_422():
    check_422([{"rule_index": 0, "declaration_index": 0, "important": "yes"}])


def test_blank_layer_422():
    detail = check_422([{"rule_index": 0, "declaration_index": 0, "layer": "  "}])
    assert "空白" in detail


# ---------- 响应结构：基线与补丁完整结果 ----------


def test_preview_returns_baseline_and_patched_snapshots():
    data = compute(
        base_payload(
            rules=[rule("p", decl("color", "red"))],
            patches=[{"rule_index": 0, "declaration_index": 0, "value": "blue"}],
        )
    )
    preview = data["preview"]
    # 顶层字段即基线
    assert data["computed"]["color"] == "red"
    assert preview["baseline"]["computed"]["color"] == "red"
    assert preview["patched"]["computed"]["color"] == "blue"
    # 嵌套快照不带 preview 键
    assert "preview" not in preview["baseline"]
    assert "preview" not in preview["patched"]


# ---------- value_edit ----------


def test_value_patch_on_winner_is_value_edit():
    data = compute(
        base_payload(
            rules=[rule("p", decl("color", "red"), decl("margin", "0"))],
            patches=[{"rule_index": 0, "declaration_index": 0, "value": "blue"}],
        )
    )
    d = diff_by(data, "color")
    assert d["diff_type"] == "value_edit"
    assert d["baseline_value"] == "red"
    assert d["patched_value"] == "blue"
    assert path_kinds(d) == [["patch", "result"]]
    # 未改的属性不出现在差异中
    assert all(x["property"] != "margin" for x in data["preview"]["property_diffs"])


# ---------- winner_change ----------


def test_important_flip_changes_winner():
    data = compute(
        base_payload(
            layers=["base", "components"],
            rules=[
                rule("p", decl("color", "blue"), layer="base"),
                rule("p", decl("color", "green"), layer="components"),
            ],
            patches=[{"rule_index": 0, "declaration_index": 0, "important": True}],
        )
    )
    d = diff_by(data, "color")
    assert d["diff_type"] == "winner_change"
    assert d["baseline_value"] == "green"
    assert d["patched_value"] == "blue"
    assert path_kinds(d) == [["patch", "cascade", "result"]]
    assert d["baseline_winner"]["layer"] == "components"
    assert d["patched_winner"]["layer"] == "base"
    assert d["patched_winner"]["important"] is True


def test_layer_patch_demotes_winner():
    data = compute(
        base_payload(
            element={"tag": "p", "classes": ["t"]},
            layers=["reset", "components"],
            rules=[
                rule(".t", decl("color", "red")),
                rule("p", decl("color", "green"), layer="components"),
            ],
            patches=[{"rule_index": 0, "declaration_index": 0, "layer": "reset"}],
        )
    )
    d = diff_by(data, "color")
    assert d["diff_type"] == "winner_change"
    assert d["baseline_value"] == "red"
    assert d["patched_value"] == "green"
    # 因果轨迹自被改声明（旧胜出者）出发
    (path,) = d["causal_paths"]
    assert path[0]["kind"] == "patch"
    assert "layer" in path[0]["detail"]


def test_layer_null_moves_declaration_to_unlayered():
    data = compute(
        base_payload(
            layers=["a"],
            rules=[
                rule("p", decl("color", "red"), layer="a"),
                rule("p", decl("color", "blue")),
            ],
            patches=[{"rule_index": 0, "declaration_index": 0, "layer": None}],
        )
    )
    # 未分层（同特异性，且规则 0 源码次序更早）—— 改到未分层后仍按源码次序，
    # 规则 1 晚者胜；主要验证 layer: null 被接受为「显式未分层」
    assert data["preview"]["patched"]["computed"]["color"] == "blue"


# ---------- var_value_change ----------


def test_var_dependency_change_propagates():
    data = compute(
        base_payload(
            rules=[
                rule("p", decl("--gap", "10px"), decl("margin", "var(--gap)")),
            ],
            patches=[{"rule_index": 0, "declaration_index": 0, "value": "20px"}],
        )
    )
    preview = data["preview"]
    margin = diff_by(data, "margin")
    assert margin["diff_type"] == "var_value_change"
    assert margin["baseline_value"] == "10px"
    assert margin["patched_value"] == "20px"
    (path,) = margin["causal_paths"]
    assert [s["kind"] for s in path] == ["patch", "var", "result"]
    # 自定义属性本身记为 value_edit
    gap = diff_by(data, "--gap", key="custom_property_diffs")
    assert gap["diff_type"] == "value_edit"
    assert gap["baseline_value"] == "10px" and gap["patched_value"] == "20px"
    # 胜出者未变
    assert margin["baseline_winner"] == margin["patched_winner"]


def test_var_change_via_ancestor_inheritance():
    data = compute(
        base_payload(
            ancestors=[{"tag": "body"}],
            rules=[
                rule("body", decl("--gap", "10px")),
                rule("p", decl("margin", "var(--gap)")),
            ],
            patches=[{"rule_index": 0, "declaration_index": 0, "value": "33px"}],
        )
    )
    margin = diff_by(data, "margin")
    assert margin["diff_type"] == "var_value_change"
    assert margin["patched_value"] == "33px"
    (path,) = margin["causal_paths"]
    assert path[0]["element"] == "body"
    assert [s["kind"] for s in path] == ["patch", "var", "result"]


def test_chained_custom_property_propagates():
    data = compute(
        base_payload(
            rules=[
                rule(
                    "p",
                    decl("--base", "4px"),
                    decl("--double", "calc(var(--base) * 2)"),
                    decl("margin", "var(--double)"),
                ),
            ],
            patches=[{"rule_index": 0, "declaration_index": 0, "value": "6px"}],
        )
    )
    margin = diff_by(data, "margin")
    assert margin["patched_value"] == "calc(6px * 2)"
    kinds = [s["kind"] for s in margin["causal_paths"][0]]
    assert kinds == ["patch", "var", "var", "result"]


# ---------- fallback 改道 ----------


def test_fallback_diversion_when_variable_becomes_invalid():
    data = compute(
        base_payload(
            rules=[
                rule("p", decl("--x", "9px"), decl("margin", "var(--x, 1px)")),
            ],
            patches=[{"rule_index": 0, "declaration_index": 0, "value": "var(--x)"}],
        )
    )
    margin = diff_by(data, "margin")
    assert margin["diff_type"] == "var_value_change"
    assert margin["baseline_value"] == "9px"
    assert margin["patched_value"] == "1px"
    (path,) = margin["causal_paths"]
    assert [s["kind"] for s in path] == ["patch", "fallback", "result"]
    assert "fallback" in path[1]["detail"]


def test_fallback_abandoned_when_variable_becomes_defined():
    data = compute(
        base_payload(
            ancestors=[{"tag": "body"}],
            rules=[
                rule("body", decl("--x", "var(--nope)")),
                rule("p", decl("margin", "var(--x, 1px)")),
            ],
            patches=[{"rule_index": 0, "declaration_index": 0, "value": "4px"}],
        )
    )
    margin = diff_by(data, "margin")
    assert margin["baseline_value"] == "1px"
    assert margin["patched_value"] == "4px"
    (path,) = margin["causal_paths"]
    assert path[0]["element"] == "body"
    assert [s["kind"] for s in path] == ["patch", "var", "result"]


# ---------- 循环形成 / 解除 ----------


def test_cycle_formation_invalidates_chain():
    data = compute(
        base_payload(
            rules=[
                rule(
                    "p",
                    decl("--a", "var(--b)"),
                    decl("--b", "5px"),
                    decl("color", "var(--a)"),
                    decl("margin", "var(--a, red)"),
                ),
            ],
            patches=[{"rule_index": 0, "declaration_index": 1, "value": "var(--a)"}],
        )
    )
    preview = data["preview"]
    color = diff_by(data, "color")
    assert color["diff_type"] == "status_switch"
    assert color["baseline_status"] == "computed"
    assert color["patched_status"] == "invalid"
    assert color["patched_value"] is None
    (path,) = color["causal_paths"]
    assert [s["kind"] for s in path] == ["patch", "cycle", "failed", "result"]
    # 循环变量在自定义属性差异中标记为 status_switch
    for name in ("--a", "--b"):
        cd = diff_by(data, name, key="custom_property_diffs")
        assert cd["patched_status"] == "invalid"
    # 有 fallback 的普通属性值改变但状态不变
    margin = diff_by(data, "margin")
    assert margin["patched_status"] == "computed"
    assert margin["patched_value"] == "red"
    assert preview["patched"]["invalid"][0]["property"] == "color"


def test_cycle_break_restores_values():
    data = compute(
        base_payload(
            rules=[
                rule(
                    "p",
                    decl("--a", "var(--b)"),
                    decl("--b", "var(--a)"),
                    decl("color", "var(--a, red)"),
                ),
            ],
            patches=[{"rule_index": 0, "declaration_index": 1, "value": "7px"}],
        )
    )
    color = diff_by(data, "color")
    assert color["diff_type"] == "var_value_change"
    assert color["baseline_value"] == "red"
    assert color["patched_value"] == "7px"
    a = diff_by(data, "--a", key="custom_property_diffs")
    assert a["diff_type"] == "status_switch"
    assert a["baseline_status"] == "invalid"
    assert a["patched_status"] == "ok"
    assert a["patched_value"] == "7px"


# ---------- status_switch ----------


def test_computed_to_invalid_when_dependency_breaks():
    data = compute(
        base_payload(
            rules=[
                rule(
                    "p",
                    decl("--x", "2px"),
                    decl("margin", "var(--x)"),
                ),
            ],
            patches=[{"rule_index": 0, "declaration_index": 0, "value": "var(--nope)"}],
        )
    )
    margin = diff_by(data, "margin")
    assert margin["diff_type"] == "status_switch"
    assert margin["patched_status"] == "invalid"
    (path,) = margin["causal_paths"]
    assert path[-1]["kind"] == "result"
    assert any(s["kind"] == "failed" for s in path)


def test_invalid_to_computed_when_fallback_source_fixed():
    data = compute(
        base_payload(
            rules=[
                rule(
                    "p",
                    decl("--x", "var(--nope)"),
                    decl("margin", "var(--x)"),
                ),
            ],
            patches=[{"rule_index": 0, "declaration_index": 0, "value": "3px"}],
        )
    )
    margin = diff_by(data, "margin")
    assert margin["diff_type"] == "status_switch"
    assert margin["baseline_status"] == "invalid"
    assert margin["patched_status"] == "computed"
    assert margin["patched_value"] == "3px"


# ---------- shadowed ----------


def test_patch_on_losing_declaration_is_shadowed():
    data = compute(
        base_payload(
            rules=[
                rule("p", decl("color", "red")),
                rule("p", decl("color", "blue")),
            ],
            patches=[{"rule_index": 0, "declaration_index": 0, "value": "green"}],
        )
    )
    preview = data["preview"]
    assert preview["property_diffs"] == []
    (shadowed,) = preview["shadowed_patches"]
    assert shadowed["rule_index"] == 0
    assert shadowed["declaration_index"] == 0
    assert shadowed["property"] == "color"
    assert "落选" in shadowed["reason"]
    assert shadowed["changes"] == {"value": "green"}


def test_patch_on_winner_with_no_effect_is_shadowed():
    data = compute(
        base_payload(
            layers=["base"],
            rules=[rule("p", decl("color", "red"), layer="base")],
            # 唯一候选改到另一个层：仍胜出、值不变
            patches=[{"rule_index": 0, "declaration_index": 0, "layer": "other"}],
        )
    )
    preview = data["preview"]
    assert preview["property_diffs"] == []
    (shadowed,) = preview["shadowed_patches"]
    assert "胜出" in shadowed["reason"]


def test_effective_patches_are_not_shadowed():
    data = compute(
        base_payload(
            rules=[rule("p", decl("--gap", "10px"), decl("margin", "var(--gap)"))],
            patches=[{"rule_index": 0, "declaration_index": 0, "value": "20px"}],
        )
    )
    assert data["preview"]["shadowed_patches"] == []


# ---------- 只列变化的属性 ----------


def test_unchanged_properties_are_omitted_from_diffs():
    data = compute(
        base_payload(
            rules=[
                rule(
                    "p",
                    decl("--gap", "10px"),
                    decl("margin", "var(--gap)"),
                    decl("color", "red"),
                ),
            ],
            patches=[{"rule_index": 0, "declaration_index": 0, "value": "20px"}],
        )
    )
    props = {d["property"] for d in data["preview"]["property_diffs"]}
    assert props == {"margin"}
    custom = {d["property"] for d in data["preview"]["custom_property_diffs"]}
    assert custom == {"--gap"}
