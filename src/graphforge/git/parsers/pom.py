"""Parse Maven pom.xml into module coordinates and dependencies."""
from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Any, Dict

_NS = {"m": "http://maven.apache.org/POM/4.0.0"}


def _text(element, path: str) -> str:
    if element is None:
        return ""
    found = element.find(f"m:{path}", _NS)
    if found is None:
        found = element.find(path)
    return (found.text or "").strip() if found is not None else ""


def parse_pom(pom_path: str) -> Dict[str, Any]:
    """Return groupId/artifactId/version/packaging/name + dependency list."""
    result: Dict[str, Any] = {
        "groupId": "", "artifactId": "", "version": "",
        "packaging": "jar", "name": "", "dependencies": [],
    }
    try:
        root = ET.parse(pom_path).getroot()
    except Exception:
        return result

    result["groupId"] = _text(root, "groupId")
    result["artifactId"] = _text(root, "artifactId")
    result["version"] = _text(root, "version")
    result["packaging"] = _text(root, "packaging") or "jar"
    result["name"] = _text(root, "name")

    # Inherit groupId/version from <parent> when the child omits them.
    parent = root.find("m:parent", _NS) or root.find("parent")
    if parent is not None:
        result["groupId"] = result["groupId"] or _text(parent, "groupId")
        result["version"] = result["version"] or _text(parent, "version")

    deps_elem = root.find("m:dependencies", _NS)
    if deps_elem is None:
        deps_elem = root.find("dependencies")
    if deps_elem is not None:
        for dep in deps_elem:
            result["dependencies"].append({
                "groupId": _text(dep, "groupId"),
                "artifactId": _text(dep, "artifactId"),
                "version": _text(dep, "version"),
                "scope": _text(dep, "scope") or "compile",
            })
    return result
