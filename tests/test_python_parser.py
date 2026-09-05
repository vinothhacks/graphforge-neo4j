"""Structural parsing of Python source (graphforge.git.parsers.python)."""

from graphforge.git.parsers.python import extract

SRC = '''"""Order domain model."""
from __future__ import annotations

import os, sys as system
import collections.abc
from typing import Any, Optional as Opt
from .base import (
    Base,
    Mixin,
)
from . import util

QUERY = "class NotAClass:"          # a keyword inside a string


@dataclass
class Order(Base, Mixin):
    """An order.

    class AlsoNotAClass:            # a keyword inside a docstring
    """
    __tablename__ = "orders"

    def __init__(self, oid: int) -> None:
        self.oid = oid

    @property
    def total(self) -> float:
        if self.oid:
            return 1.0
        return 0.0

    async def refresh(
        self,
        force: bool = False,
    ) -> Order:
        return self

    def __secret(self) -> None:
        pass


class Status(Enum):
    NEW = 1
    DONE = 2


class Repo(Protocol):
    def get(self, oid: int) -> Order: ...


class Legacy(models.Model):
    class Meta:
        db_table = "legacy_orders"


def module_level(a, b=2) -> int:
    return a + b
'''.splitlines()


def test_module_docstring():
    assert extract(SRC)["docstring"] == "Order domain model."


def test_imports():
    fqns = {i["fqn"] for i in extract(SRC)["imports"]}
    assert {
        "os",
        "sys",
        "collections.abc",
        "typing.Any",
        "typing.Optional",
        ".base.Base",
        ".base.Mixin",
        ".util",
        "__future__.annotations",
    } <= fqns
    aliases = {i["fqn"]: i["alias"] for i in extract(SRC)["imports"]}
    assert aliases["sys"] == "system"
    assert aliases["typing.Optional"] == "Opt"


def test_classes_and_bases():
    info = extract(SRC)
    order = next(c for c in info["classes"] if c["name"] == "Order")
    assert order["bases"] == ["Base", "Mixin"]
    assert order["extends"] == "Base"
    assert order["implements"] == ["Mixin"]
    assert order["type"] == "class"
    assert order["line"] == 17
    # keywords inside strings/docstrings must not become declarations
    assert {c["name"] for c in info["classes"]} == {"Order", "Legacy"}


def test_enum_and_protocol_buckets():
    info = extract(SRC)
    assert [e["name"] for e in info["enums"]] == ["Status"]
    assert [i["name"] for i in info["interfaces"]] == ["Repo"]
    assert info["interfaces"][0]["isAbstract"] is True


def test_functions_and_methods():
    info = extract(SRC)
    by_name = {m["name"]: m for m in info["methods"]}
    assert {"__init__", "total", "refresh", "__secret", "get", "module_level"} <= set(by_name)
    assert by_name["refresh"]["isAsync"] is True  # multi-line `async def`
    assert by_name["total"]["isAsync"] is False
    assert by_name["total"]["returnType"] == "float"
    assert by_name["module_level"]["returnType"] == "int"
    assert by_name["total"]["owner"] == "Order"
    assert by_name["get"]["owner"] == "Repo"
    assert by_name["module_level"]["owner"] == ""  # module-level function
    assert by_name["__secret"]["visibility"] == "private"
    assert by_name["__init__"]["visibility"] == "public"  # dunder is API surface


def test_decorators_land_in_annotations():
    info = extract(SRC)
    names = {a["name"] for a in info["annotations"]}
    assert {"dataclass", "property"} <= names
    order = next(c for c in info["classes"] if c["name"] == "Order")
    assert order["annotations"] == ["dataclass"]
    total = next(m for m in info["methods"] if m["name"] == "total")
    assert total["annotations"] == ["property"]


def test_orm_table_mapping():
    info = extract(SRC)
    order = next(c for c in info["classes"] if c["name"] == "Order")
    assert order["mappedTable"] == "orders"  # SQLAlchemy __tablename__
    assert order["stereotype"] == "Entity"
    legacy = next(c for c in info["classes"] if c["name"] == "Legacy")
    assert legacy["mappedTable"] == "legacy_orders"  # Django class Meta.db_table
    assert "Meta" not in {c["name"] for c in info["classes"]}


def test_dotted_decorator_and_async_def_alone():
    info = extract(["@app.route('/x')", "async def handler():", "    pass"])
    assert info["annotations"][0]["name"] == "app.route"
    assert info["methods"][0] == {
        "name": "handler",
        "line": 2,
        "returnType": "",
        "visibility": "public",
        "isAsync": True,
        "owner": "",
        "annotations": ["app.route"],
    }


def test_malformed_input_never_raises():
    for bad in (
        [],
        ["class ((((("],
        ["def )(:"],
        ["@@@@"],
        ["from import"],
        ["'''never closed"],
        ["\x00\xff binary"],
        ["import"],
    ):
        info = extract(bad)
        assert info["classes"] == [] or isinstance(info["classes"], list)
        assert set(info) >= {
            "package",
            "imports",
            "classes",
            "interfaces",
            "enums",
            "methods",
            "annotations",
        }
