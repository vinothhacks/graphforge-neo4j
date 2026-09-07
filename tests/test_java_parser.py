from graphforge.git.parsers.java import extract

SRC = """package com.acme;

import java.util.List;
import java.io.Serializable;

/* a block
   comment */
public abstract class Widget extends Base implements Serializable, Comparable {
    private String name;
    public String getName() { return name; }
    public void setName(String n) { this.name = n; }
}

interface Handler {
    void handle(Widget w);
}
""".splitlines()


def test_package_and_imports():
    info = extract(SRC)
    assert info["package"] == "com.acme"
    assert {i["fqn"] for i in info["imports"]} == {"java.util.List", "java.io.Serializable"}


def test_types():
    info = extract(SRC)
    cls = info["classes"][0]
    assert cls["name"] == "Widget"
    assert cls["isAbstract"] is True
    assert cls["extends"] == "Base"
    assert set(cls["implements"]) == {"Serializable", "Comparable"}
    assert info["interfaces"][0]["name"] == "Handler"


def test_methods():
    info = extract(SRC)
    names = {m["name"] for m in info["methods"]}
    assert {"getName", "setName"} <= names
    # control-flow keywords must not be picked up as methods
    assert "if" not in names and "return" not in names


def test_inline_annotations_on_declaration_line():
    # annotations sharing the class line must not swallow the declaration
    info = extract(["package com.acme;", '@Entity @Table(name="orders") public class Order {}'])
    assert len(info["classes"]) == 1
    c = info["classes"][0]
    assert c["name"] == "Order"
    assert c["stereotype"] == "Entity"
    assert c["mappedTable"] == "orders"


def test_malformed_input_never_raises():
    for bad in (
        [],
        ["class ((((("],
        ["public void )("],
        ["@@@@"],
        ["package ;"],
        ["'''never closed"],
        ["\x00 binary"],
        ["import"],
    ):
        info = extract(bad)
        assert isinstance(info["classes"], list)
        assert set(info) >= {
            "package",
            "imports",
            "classes",
            "interfaces",
            "enums",
            "methods",
            "annotations",
        }
