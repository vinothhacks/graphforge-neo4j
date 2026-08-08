#!/usr/bin/env python3
"""Print the CHANGELOG.md section for a release tag, for use as a release body.

    python scripts/changelog_section.py v0.1.0 > release-notes.md

Headings are matched Keep-a-Changelog style, so all of these are recognised::

    ## [0.1.0] - 2026-08-08
    ## [0.1.0]
    ## 0.1.0

Lookup order for a tag, first hit wins:

    v0.1.0        -> "0.1.0"            -> "v0.1.0"      -> "Unreleased"
    v0.2.0-rc3    -> "0.2.0-rc3"        -> "0.2.0rc3"    -> "0.2.0" -> "Unreleased"

Falling back to ``Unreleased`` is deliberate: release candidates are usually cut
before the section has been renamed. Nothing here ever fails the build - a
missing section produces a short placeholder body and a ``::warning::``
annotation on stderr, because an imperfect release note is not a reason to abort
a publish that has already happened.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

HEADING = re.compile(r"^##\s+\[?(?P<name>[^\]\n]+?)\]?\s*(?:-\s*\d{4}-\d{2}-\d{2}\s*)?$")


def candidates(tag: str) -> list[str]:
    """Section names to try, most specific first."""
    bare = tag[1:] if re.fullmatch(r"v\d.*", tag) else tag
    names = [bare, tag]
    # PEP 440 spelling: 0.2.0-rc3 is published as 0.2.0rc3.
    pep440 = re.sub(r"[-_.]?(a|b|c|rc|alpha|beta|pre|preview)[-_.]?", r"\1", bare)
    names.append(pep440)
    # The final release the candidate belongs to.
    base = re.split(r"[-+]", bare, maxsplit=1)[0]
    names.append(base)
    names.append("Unreleased")
    seen: set[str] = set()
    return [n for n in names if n and not (n.lower() in seen or seen.add(n.lower()))]


def section(text: str, name: str) -> str | None:
    """Return the body under the `## <name>` heading, or None."""
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        match = HEADING.match(line)
        if match and match.group("name").strip().lower() == name.lower():
            start = i + 1
            break
    if start is None:
        return None
    end = len(lines)
    for j in range(start, len(lines)):
        if HEADING.match(lines[j]):
            end = j
            break
    return "\n".join(lines[start:end]).strip("\n")


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: changelog_section.py <tag> [CHANGELOG.md]", file=sys.stderr)
        return 2
    tag = argv[0]
    path = Path(argv[1]) if len(argv) > 1 else Path(__file__).resolve().parent.parent / "CHANGELOG.md"

    if not path.is_file():
        print(f"::warning::{path} not found; using a placeholder release body", file=sys.stderr)
        print(f"Release {tag}.")
        return 0

    text = path.read_text(encoding="utf-8")
    tried = candidates(tag)
    for name in tried:
        body = section(text, name)
        if body:
            print(f"resolved {tag} -> '## {name}'", file=sys.stderr)
            print(body)
            return 0

    print(
        f"::warning::no CHANGELOG.md section matched {tag} (tried: {', '.join(tried)})",
        file=sys.stderr,
    )
    print(f"Release {tag}.\n\nNo matching CHANGELOG.md section was found; see the commit log.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
