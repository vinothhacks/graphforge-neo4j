"""Structural parsing of TypeScript / JavaScript (graphforge.git.parsers.typescript)."""

from graphforge.git.parsers.typescript import extract

SRC = """/* header comment
   class NotAClass {}  */
import { Component, OnInit } from '@angular/core';
import DefaultThing from './thing';
import * as fs from 'fs';
import './styles.css';
export { Helper } from './helper';
const legacy = require('legacy-lib');

const sql = "class AlsoNotAClass {";   // a keyword inside a string

export interface Repo<T> extends Base, Other {
  get(id: number): Promise<T>;
  readonly size: number;
}

export type Id = string | number;

export enum Status { NEW = 'new', DONE = 'done' }

@Component({
  selector: 'app-order',
})
export class OrderComponent extends Base implements OnInit, Repo<Order> {
  private total = 0;

  constructor(private svc: OrderService) { super(); }

  async ngOnInit(): Promise<void> {
    if (this.total) { return; }
    for (const x of []) { console.log(x); }
  }

  static create(): OrderComponent { return new OrderComponent(); }
}

@Entity({ name: 'orders' })
export class Order {}

@Entity('legacy_orders')
class LegacyOrder {}

export function helper(a: number): string { return 'x'; }
export const arrow = async (a: number): Promise<void> => { };
export const notAFunction = (1 + 2) * 3;
const shorthand = x => x + 1;
""".splitlines()


def _by_name(items):
    return {i["name"]: i for i in items}


def test_imports_and_requires():
    info = extract(SRC)
    modules = {i["module"] for i in info["imports"]}
    assert {"@angular/core", "./thing", "fs", "./styles.css", "./helper", "legacy-lib"} <= modules
    named = {i["fqn"] for i in info["imports"]}
    assert "@angular/core/Component" in named and "@angular/core/OnInit" in named
    kinds = {i["module"]: i["kind"] for i in info["imports"]}
    assert kinds["./styles.css"] == "side-effect"
    assert kinds["legacy-lib"] == "require"
    assert kinds["./helper"] == "re-export"


def test_exports():
    exports = set(extract(SRC)["exports"])
    assert {
        "Helper",
        "Repo",
        "Id",
        "Status",
        "OrderComponent",
        "Order",
        "helper",
        "arrow",
    } <= exports


def test_classes_extends_implements():
    info = extract(SRC)
    classes = _by_name(info["classes"])
    assert set(classes) == {"OrderComponent", "Order", "LegacyOrder"}
    oc = classes["OrderComponent"]
    assert oc["extends"] == "Base"
    # generic arguments are dropped so graph identities stay stable
    assert oc["implements"] == ["OnInit", "Repo"]
    assert oc["isExported"] is True


def test_interfaces_enums_and_type_aliases():
    info = extract(SRC)
    ifaces = _by_name(info["interfaces"])
    assert ifaces["Repo"]["type"] == "interface"
    assert ifaces["Repo"]["extends"] == "Base"
    assert ifaces["Repo"]["implements"] == ["Other"]
    assert ifaces["Id"]["type"] == "type"
    assert [e["name"] for e in info["enums"]] == ["Status"]


def test_functions_methods_and_arrows():
    methods = _by_name(extract(SRC)["methods"])
    assert {"get", "constructor", "ngOnInit", "create", "helper", "arrow", "shorthand"} <= set(
        methods
    )
    assert methods["ngOnInit"]["isAsync"] is True
    assert methods["ngOnInit"]["returnType"] == "Promise<void>"
    assert methods["ngOnInit"]["owner"] == "OrderComponent"
    assert methods["create"]["isStatic"] is True
    assert methods["get"]["owner"] == "Repo"  # interface signature
    assert methods["helper"]["owner"] == ""  # module-level function
    assert methods["arrow"]["isAsync"] is True  # arrow-function const
    # control flow and non-function consts must not be picked up
    assert "if" not in methods and "for" not in methods
    assert "notAFunction" not in methods


def test_decorators_land_in_annotations():
    info = extract(SRC)
    assert {a["name"] for a in info["annotations"]} == {"Component", "Entity"}
    classes = _by_name(info["classes"])
    # a multi-line decorator still binds to the class that follows it
    assert classes["OrderComponent"]["annotations"] == ["Component"]
    assert classes["OrderComponent"]["stereotype"] == "Component"


def test_typeorm_entity_table_names():
    classes = _by_name(extract(SRC)["classes"])
    assert classes["Order"]["stereotype"] == "Entity"
    assert classes["Order"]["mappedTable"] == "orders"  # { name: 'orders' }
    assert classes["LegacyOrder"]["mappedTable"] == "legacy_orders"  # positional


def test_plain_javascript():
    info = extract(
        [
            "const express = require('express');",
            "export default class App extends Server {",
            "  run() { return 1; }",
            "}",
            "export const boot = () => new App();",
        ]
    )
    assert info["imports"][0]["module"] == "express"
    assert [c["name"] for c in info["classes"]] == ["App"]
    assert info["classes"][0]["extends"] == "Server"
    assert {"run", "boot"} <= {m["name"] for m in info["methods"]}


def test_malformed_input_never_raises():
    for bad in (
        [],
        ["class {{{{"],
        ["@@@((("],
        ["import from"],
        ["`unterminated"],
        ["/* never closed"],
        ["\x00\xff"],
    ):
        info = extract(bad)
        assert set(info) >= {
            "package",
            "imports",
            "classes",
            "interfaces",
            "enums",
            "methods",
            "annotations",
        }
