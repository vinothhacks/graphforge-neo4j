"""File-type table and line classification shared by all languages."""
from __future__ import annotations

import os

# Map file extension -> logical file type stored on :File.type
SUPPORTED_EXTENSIONS = {
    ".java": "java", ".kt": "kotlin", ".kts": "kotlin", ".scala": "scala", ".groovy": "groovy",
    ".py": "python", ".rb": "ruby", ".php": "php", ".go": "go", ".rs": "rust", ".swift": "swift",
    ".cs": "csharp", ".c": "c", ".h": "c", ".cpp": "cpp", ".cc": "cpp", ".hpp": "cpp",
    ".js": "javascript", ".jsx": "javascript", ".ts": "typescript", ".tsx": "typescript",
    ".vue": "vue", ".sql": "sql",
    ".xml": "xml", ".xhtml": "xml", ".html": "html", ".htm": "html",
    ".css": "css", ".scss": "scss", ".sass": "sass", ".less": "less",
    ".json": "json", ".yml": "yaml", ".yaml": "yaml", ".toml": "toml",
    ".properties": "properties", ".gradle": "gradle", ".md": "markdown",
    ".sh": "shell", ".bat": "batch", ".ps1": "powershell",
}

# Files matched by exact name regardless of extension.
NAMED_FILES = {
    "pom.xml": "maven", "build.gradle": "gradle", "build.gradle.kts": "gradle",
    "Dockerfile": "docker", "Makefile": "make", "requirements.txt": "pip",
}

# Directories never worth walking into.
SKIP_DIRS = {
    ".git", ".svn", ".hg", "node_modules", "target", "build", "dist", "out",
    ".idea", ".vscode", "__pycache__", ".gradle", ".mvn", "bin", "obj",
    "venv", ".venv", "env", "vendor", "coverage", ".pytest_cache",
}

SKIP_FILES = {".DS_Store", "Thumbs.db", "vssver.scc"}


def file_type_for(filename: str) -> str | None:
    """Return the logical type for a filename, or None if unsupported."""
    if filename in NAMED_FILES:
        return NAMED_FILES[filename]
    ext = os.path.splitext(filename)[1].lower()
    return SUPPORTED_EXTENSIONS.get(ext)


def classify_line(content: str, ext: str) -> str:
    """Classify a source line as blank/comment/import/annotation/code/etc."""
    stripped = content.strip()
    if not stripped:
        return "blank"

    if ext in (".java", ".kt", ".kts", ".scala", ".groovy", ".c", ".h", ".cpp",
               ".cc", ".hpp", ".cs", ".go", ".rs", ".swift", ".js", ".jsx",
               ".ts", ".tsx", ".php"):
        if stripped.startswith("//"):
            return "comment"
        if stripped.startswith(("/*", "*", "*/")):
            return "comment"
        if stripped.startswith("import ") or stripped.startswith("#include"):
            return "import"
        if stripped.startswith("package "):
            return "package"
        if stripped.startswith("@"):
            return "annotation"
        if stripped.startswith("export "):
            return "export"

    if ext in (".py", ".rb", ".sh", ".yml", ".yaml", ".properties", ".toml"):
        if stripped.startswith("#"):
            return "comment"
        if ext == ".py" and stripped.startswith(("import ", "from ")):
            return "import"

    if ext in (".xml", ".xhtml", ".html", ".htm"):
        if stripped.startswith("<!--"):
            return "comment"

    if ext in (".css", ".scss", ".sass", ".less"):
        if stripped.startswith(("/*", "//")):
            return "comment"

    return "code"
