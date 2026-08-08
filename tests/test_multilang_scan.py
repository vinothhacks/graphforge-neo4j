"""New-language parsers wired end to end: scan -> :Class / :Method in the graph."""
from graphforge.core.config import GitSettings
from graphforge.core.neo4j_writer import Neo4jWriter
from graphforge.git import scan as scan_mod
from graphforge.git.ingest import GitIngestor

FILES = {
    "app/models.py": (
        '"""Orders."""\n'
        "from sqlalchemy.orm import DeclarativeBase\n"
        "\n"
        "class Order(DeclarativeBase):\n"
        '    __tablename__ = "orders"\n'
        "\n"
        "    def total(self) -> float:\n"
        "        return 0.0\n"
        "\n"
        "def build() -> Order:\n"
        "    return Order()\n"
    ),
    "web/Widget.ts": (
        "import { Component } from '@angular/core';\n"
        "\n"
        "@Component({ selector: 'w' })\n"
        "export class Widget extends Base {\n"
        "  render(): void {}\n"
        "}\n"
    ),
    "web/legacy.jsx": (
        "export function Panel() { return null; }\n"
    ),
    "svc/store/store.go": (
        "package store\n"
        "\n"
        'import "context"\n'
        "\n"
        "type Order struct {\n"
        "    ID string\n"
        "}\n"
        "\n"
        "type Store struct {\n"
        "    conn string\n"
        "}\n"
        "\n"
        "func (s *Store) Get(ctx context.Context) (*Order, error) { return nil, nil }\n"
        "\n"
        "func New() *Store { return nil }\n"
    ),
    "java/Widget.java": (
        "package com.acme;\n"
        "\n"
        "public class Widget {\n"
        "    public String name() { return null; }\n"
        "}\n"
    ),
}


def _tree(tmp_path):
    for rel, body in FILES.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    return tmp_path


def _emit(tmp_path):
    root = _tree(tmp_path / "repo")
    out = tmp_path / "o.cypher"
    with Neo4jWriter(settings=None, emit_path=str(out)) as w:
        GitIngestor(w, GitSettings(repo_dir=str(tmp_path / "clones"))).ingest_repo(
            {"path": str(root), "name": "demo"}, with_history=False)
    return out.read_text()


def test_every_supported_language_has_a_parser():
    assert set(scan_mod.PARSERS) == {"java", "python", "typescript", "javascript", "go"}


def test_scan_populates_structure_and_namespace(tmp_path):
    root = _tree(tmp_path / "repo")
    files = {f.relpath: f for f in scan_mod.scan_repo(str(root), "demo")["files"]}

    py = files["app/models.py"]
    assert py.type == "python"
    assert py.structure["classes"][0]["name"] == "Order"
    assert py.namespace == "app.models"          # dotted module path
    assert py.package == ""                      # :Package nodes stay Java-only

    go = files["svc/store/store.go"]
    assert go.namespace == "svc.store"           # the package directory
    assert go.structure["package"] == "store"

    ts = files["web/Widget.ts"]
    assert ts.namespace == "web.Widget"

    java = files["java/Widget.java"]
    assert java.namespace == "com.acme"          # unchanged: the declared package
    assert java.package == "com.acme"
    assert java.java is java.structure           # back-compat alias still works
    assert py.java is None


def test_scan_only_paths_filter(tmp_path):
    root = _tree(tmp_path / "repo")
    files = scan_mod.scan_repo(str(root), "demo", only_paths={"app/models.py"})["files"]
    assert [f.relpath for f in files] == ["app/models.py"]


def test_python_produces_class_and_method_nodes(tmp_path):
    text = _emit(tmp_path)
    assert "MERGE (n:Class {id: 'demo/app.models.Order'})" in text
    assert "n.language = 'python'" in text
    assert "n.mappedTable = 'orders'" in text
    assert "MERGE (n:Method {id: 'demo/app.models.Order#total'})" in text
    assert "r:HAS_METHOD" in text
    # a module-level function has no owning class, so it hangs off the file
    assert "MERGE (n:Method {id: 'demo/app/models.py#build'})" in text
    assert "r:CONTAINS_METHOD" in text


def test_go_produces_class_and_method_nodes(tmp_path):
    text = _emit(tmp_path)
    assert "MERGE (n:Class {id: 'demo/svc.store.Order'})" in text
    assert "n.type = 'struct'" in text
    assert "n.language = 'go'" in text
    # the receiver binds the method to its type, not to the file's first type
    assert "MERGE (n:Method {id: 'demo/svc.store.Store#Get'})" in text
    # a package-level func does not belong to a type
    assert "MERGE (n:Method {id: 'demo/svc/store/store.go#New'})" in text


def test_typescript_and_jsx_produce_nodes(tmp_path):
    text = _emit(tmp_path)
    assert "MERGE (n:Class {id: 'demo/web.Widget.Widget'})" in text
    assert "n.stereotype = 'Component'" in text
    assert "SET n:Component" in text          # stereotype promoted to a label
    assert "MERGE (n:Method {id: 'demo/web.Widget.Widget#render'})" in text
    assert "MERGE (n:Method {id: 'demo/web/legacy.jsx#Panel'})" in text


def test_java_output_is_unchanged(tmp_path):
    text = _emit(tmp_path)
    assert "MERGE (n:Class {id: 'demo/com.acme.Widget'})" in text
    assert "MERGE (n:Method {id: 'demo/com.acme.Widget#name'})" in text
    assert "MERGE (n:Package {id: 'demo/./com.acme'})" in text
    assert "r:CONTAINS_FILE" in text
