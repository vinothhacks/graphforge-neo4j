"""Structural parsing of Go source (graphforge.git.parsers.golang)."""
from graphforge.git.parsers.golang import extract

SRC = '''// Package store persists orders.
package store

import "fmt"
import netHttp "net/http"

import (
    "context"
    "database/sql"
    uuid "github.com/google/uuid"
)

/* type NotAStruct struct {} */

type Status int

const (
    StatusNew Status = iota
    StatusDone
)

type Order struct {
    Base                       // embedded -> composition
    *Audit
    ID    string `json:"id"`
    Total float64
}

type Repository interface {
    io.Closer
    Get(ctx context.Context, id string) (*Order, error)
    Save(o *Order) error
}

type (
    Alpha struct {
        Name string
    }
    Beta interface {
        Ping() error
    }
    ID string
)

func New(db *sql.DB) (*Store, error) {
    return &Store{db: db}, nil
}

func (s *Store) Get(ctx context.Context, id string) (*Order, error) {
    if id == "" {
        return nil, fmt.Errorf("empty")
    }
    return nil, nil
}

func (s Store) internal() {}

type Empty struct{}
'''.splitlines()


def _by_name(items):
    return {i["name"]: i for i in items}


def test_package():
    assert extract(SRC)["package"] == "store"


def test_single_and_grouped_imports():
    imports = extract(SRC)["imports"]
    assert {i["fqn"] for i in imports} == {
        "fmt", "net/http", "context", "database/sql", "github.com/google/uuid"}
    aliases = {i["fqn"]: i["alias"] for i in imports}
    assert aliases["net/http"] == "netHttp"
    assert aliases["github.com/google/uuid"] == "uuid"
    assert aliases["context"] == ""


def test_structs():
    classes = _by_name(extract(SRC)["classes"])
    assert {"Order", "Alpha", "ID", "Empty"} <= set(classes)
    assert classes["Order"]["type"] == "struct"
    # embedded fields model Go composition
    assert classes["Order"]["extends"] == "Base"
    assert classes["Order"]["implements"] == ["Audit"]
    assert classes["ID"]["type"] == "type"       # named type declaration
    assert classes["Empty"]["type"] == "struct"  # `struct{}` on one line
    assert "NotAStruct" not in classes           # inside a block comment


def test_interfaces():
    ifaces = _by_name(extract(SRC)["interfaces"])
    assert set(ifaces) == {"Repository", "Beta"}
    assert ifaces["Repository"]["extends"] == "io.Closer"   # embedded interface
    assert ifaces["Repository"]["isAbstract"] is True


def test_iota_const_block_becomes_an_enum():
    info = extract(SRC)
    assert [e["name"] for e in info["enums"]] == ["Status"]
    # the `type Status int` alias is replaced, not duplicated
    assert "Status" not in {c["name"] for c in info["classes"]}


def test_funcs_and_methods_with_receivers():
    methods = _by_name(extract(SRC)["methods"])
    assert {"New", "Get", "internal", "Save", "Ping"} <= set(methods)
    assert methods["New"]["owner"] == ""                 # package-level func
    assert methods["New"]["returnType"] == "(*Store, error)"
    # the receiver names the owning type; the receiver's parens are not the args
    assert methods["internal"]["owner"] == "Store"
    assert methods["internal"]["receiver"] == "s"
    assert methods["internal"]["returnType"] == ""
    assert methods["Save"]["owner"] == "Repository"      # interface signature
    assert methods["Ping"]["owner"] == "Beta"
    # exported vs unexported identifiers
    assert methods["New"]["visibility"] == "public"
    assert methods["internal"]["visibility"] == "private"
    # control-flow keywords must not be captured
    assert "if" not in methods and "for" not in methods


def test_pointer_receiver_method_binds_to_base_type():
    methods = [m for m in extract(SRC)["methods"] if m["name"] == "Get"]
    assert {m["owner"] for m in methods} == {"Repository", "Store"}


def test_malformed_input_never_raises():
    for bad in ([], ["func (((("], ["type"], ["import ("], ['"unterminated'],
                ["/* never closed"], ["\x00\xff"]):
        info = extract(bad)
        assert set(info) >= {"package", "imports", "classes", "interfaces",
                             "enums", "methods", "annotations"}
