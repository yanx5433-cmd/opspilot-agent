"""注册表单测：「声明 = 实现」必须在注册当场生效。

这一层是护栏的地基——sanitize_args 之所以能剔除未知参数、校验类型，
全靠注册时已经把声明和函数签名对齐了。所以这里测的是"对不齐就必须炸"。
"""
from __future__ import annotations

import pytest

from app.agent.tools_registry import ToolDecl, ToolRegistry, check_type, coerce


def _echo(host: str, window_hours: int = 24) -> dict:
    return {"ok": True, "summary": host, "evidence_kind": "metric", "window": window_hours}


def _decl(**kw) -> ToolDecl:
    base = dict(name="echo", description="回显", params={"host": "str", "window_hours": "int"},
                required=["host"], side_effect=False, evidence_kind="metric")
    base.update(kw)
    return ToolDecl(**base)


class TestRegisterValidation:
    def test_happy_path(self):
        r = ToolRegistry()
        r.register(_decl(), _echo)
        assert r.has("echo")
        assert r.names() == ["echo"]

    def test_declared_param_missing_from_signature_raises(self):
        r = ToolRegistry()
        with pytest.raises(ValueError, match="不一致"):
            r.register(_decl(params={"host": "str", "window_hours": "int", "extra": "str"}), _echo)

    def test_signature_param_missing_from_declaration_raises(self):
        r = ToolRegistry()
        with pytest.raises(ValueError, match="不一致"):
            r.register(_decl(params={"host": "str"}), _echo)

    def test_zero_arg_function_with_declared_params_raises(self):
        r = ToolRegistry()
        with pytest.raises(ValueError, match="不一致"):
            r.register(_decl(params={"host": "str"}), lambda: {"ok": True})

    def test_required_param_with_default_raises(self):
        """必需参数带默认值 = 声明在撒谎，LLM 少传一个参数也不会报错，是隐蔽的失效模式。"""
        r = ToolRegistry()
        with pytest.raises(ValueError, match="不该有默认值"):
            r.register(_decl(required=["window_hours"]), _echo)

    def test_required_param_not_declared_raises(self):
        r = ToolRegistry()
        with pytest.raises(ValueError, match="不在 params 中"):
            r.register(_decl(required=["ghost"]), _echo)

    def test_enum_on_undeclared_param_raises(self):
        r = ToolRegistry()
        with pytest.raises(ValueError, match="未声明参数"):
            r.register(_decl(enum_params={"ghost": ["a"]}), _echo)

    def test_duplicate_name_raises(self):
        r = ToolRegistry()
        r.register(_decl(), _echo)
        with pytest.raises(ValueError, match="重复注册"):
            r.register(_decl(description="第二次"), _echo)

    def test_decorator_registers_and_returns_function(self):
        r = ToolRegistry()

        @r.tool(name="echo", description="回显", params={"host": "str", "window_hours": "int"},
                required=["host"])
        def handler(host: str, window_hours: int = 24) -> dict:
            return {"ok": True, "summary": host}

        assert r.has("echo")
        assert handler("a")["summary"] == "a"

    def test_one_valid_registration_failure_leaves_registry_untouched(self):
        r = ToolRegistry()
        with pytest.raises(ValueError):
            r.register(_decl(name="bad", required=["ghost"]), _echo)
        assert r.names() == []


class TestRegistryQueries:
    def test_read_write_partition(self, registry):
        reads, writes = registry.read_tools(), registry.write_tools()
        assert set(reads) | set(writes) == set(registry.names())
        assert not set(reads) & set(writes)
        assert writes == ["notify_send", "report_render", "ticket_create"]

    def test_real_registry_counts(self, registry):
        """14 只工具 = 11 只读 + 3 只写。
        注意：app/tools/__init__.py 的模块注释写的是"10 只读 + 4 写"，与实现不符（KNOW-DOC）。
        """
        assert len(registry.names()) == 14
        assert len(registry.read_tools()) == 11
        assert len(registry.write_tools()) == 3

    def test_get_returns_decl_and_fn(self, registry):
        decl, fn = registry.get("metrics_query")
        assert decl.name == "metrics_query"
        assert callable(fn)
        assert registry.get("ghost") is None

    def test_prompt_manifest_shape(self, registry):
        first = registry.prompt_manifest()[0]
        assert set(first) == {"name", "description", "params", "required", "side_effect", "enum_params"}

    def test_validate_domain_passes_when_undeclared(self, registry):
        assert registry.validate_domain("metrics_query", "host", "anything") is True

    def test_validate_domain_rejects_out_of_range(self, registry):
        assert registry.validate_domain("metrics_query", "metric", "gpu") is False
        assert registry.validate_domain("metrics_query", "metric", "disk") is True

    def test_validate_domain_on_unknown_tool_raises(self, registry):
        """已知行为：调用方必须先确认工具存在（executor 就是这么做的）。钉死它，避免以后被误用。"""
        with pytest.raises(KeyError):
            registry.validate_domain("ghost", "host", "x")

    def test_decls_are_sorted_by_name(self, registry):
        names = [d.name for d in registry.decls()]
        assert names == sorted(names)


class TestCheckType:
    @pytest.mark.parametrize(
        ("type_name", "value", "expected"),
        [("str", "a", True), ("str", 1, False),
         ("int", 1, True), ("int", True, False), ("int", 1.0, False), ("int", "1", False),
         ("float", 1, True), ("float", 1.5, True), ("float", True, False),
         ("bool", True, True), ("bool", 1, False), ("bool", "true", False),
         ("list", [], True), ("list", (), False), ("list", "a", False),
         ("dict", {}, True), ("dict", [], False)],
    )
    def test_matrix(self, type_name, value, expected):
        assert check_type(type_name, value) is expected

    def test_bool_is_not_an_int(self):
        """Python 里 True 是 int 的实例；工具层必须区分，否则 window_hours=true 会被当成 1。"""
        assert check_type("int", True) is False

    def test_unknown_type_name_is_permissive(self):
        assert check_type("weird", object()) is True

    def test_list_suffix_is_stripped(self):
        assert check_type("list[]", ["a"]) is True


class TestCoerce:
    @pytest.mark.parametrize(
        ("value", "type_name", "expected"),
        [("24", "int", 24), (" 42 ", "int", 42), ("-7", "int", -7),
         ("1.5", "float", 1.5), ("true", "bool", True), ("1", "bool", True),
         ("no", "bool", False), ("web", "str", "web"), (80, "str", "80"),
         ("a, b ,c", "list", ["a", "b", "c"]), (["x"], "list", ["x"]),
         ("abc", "int", "abc"), ("", "list", []), ("None", "int", "None")],
    )
    def test_matrix(self, value, type_name, expected):
        assert coerce(value, type_name) == expected

    def test_unsafe_conversion_is_left_for_validate_to_reject(self):
        """coerce 只做无损收窄；剩下的交给 check_type 判失败，而不是硬转出一个错值。"""
        assert coerce("abc", "float") == "abc"
        assert check_type("float", coerce("abc", "float")) is False

    def test_bool_is_not_stringified(self):
        assert coerce(True, "str") is True

    def test_dedupe_preserves_order(self):
        from app.agent.tools_registry import dedupe

        assert dedupe(["b", "a", "b", "c", "a"]) == ["b", "a", "c"]
