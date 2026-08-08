"""Language / file parsers used by the code-structure scanner.

Every language module exposes ``extract(lines: list[str]) -> dict`` returning the
same keys (package, imports, classes, interfaces, enums, methods, annotations),
so ``git.scan`` and ``git.ingest`` stay language-agnostic.
"""

from . import generic, golang, java, pom, python, typescript

__all__ = ["generic", "golang", "java", "pom", "python", "typescript"]
