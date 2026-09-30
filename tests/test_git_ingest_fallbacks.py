"""git.ingest keeps working when its optional progress bar is not installed."""

from __future__ import annotations

import importlib
import sys

import pytest

from graphforge.core.config import GitSettings
from graphforge.core.neo4j_writer import Neo4jWriter
from graphforge.git import ingest as ingest_mod


@pytest.fixture
def ingest_without_tqdm(monkeypatch):
    """The module as it imports on a machine with no tqdm, restored afterwards."""
    monkeypatch.setitem(sys.modules, "tqdm", None)  # `import tqdm` now raises ImportError
    # reload re-runs the module in its old namespace, so drop the old binding first
    monkeypatch.delattr(ingest_mod, "tqdm", raising=False)
    yield importlib.reload(ingest_mod)
    monkeypatch.undo()
    importlib.reload(ingest_mod)


def test_the_file_loop_uses_the_progress_fallback(ingest_without_tqdm, tmp_path):
    """Regression: one loop called tqdm() by name, a NameError without tqdm."""
    assert not hasattr(ingest_without_tqdm, "tqdm"), "fixture did not hide tqdm"
    src = tmp_path / "src"
    (src / "app").mkdir(parents=True)
    (src / "app" / "util.py").write_text("def helper():\n    return 1\n", encoding="utf-8")

    with Neo4jWriter(settings=None, emit_path=str(tmp_path / "out.cypher")) as writer:
        ingestor = ingest_without_tqdm.GitIngestor(
            writer, GitSettings(repo_dir=str(tmp_path / "repos"))
        )
        assert ingestor._ingest_structure("demo", str(src), include_lines=False) == 1
    assert "demo/app/util.py" in (tmp_path / "out.cypher").read_text(encoding="utf-8")
