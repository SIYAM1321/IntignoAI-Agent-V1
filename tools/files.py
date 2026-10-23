from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any


TEXT_EXTENSIONS = {
    ".txt", ".py", ".pyw", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".json", ".md", ".markdown",
    ".html", ".htm", ".css", ".scss", ".sass", ".less", ".csv", ".xml", ".yaml", ".yml", ".toml",
    ".ini", ".cfg", ".conf", ".env", ".sql", ".sh", ".bat", ".cmd", ".ps1", ".java", ".c", ".h",
    ".cpp", ".hpp", ".cc", ".hh", ".go", ".rs", ".php", ".rb", ".swift", ".kt", ".kts", ".vue",
    ".svelte", ".astro", ".dockerignore", ".gitignore", ".editorconfig", ".properties", ".graphql", ".gql",
    ".r", ".lua", ".dart", ".ex", ".exs", ".fs", ".fsx", ".vb", ".cs", ".pl", ".pm", ".tex",
}
MAX_LIST_ITEMS = 5000


def resolve_workspace(value: str | None) -> Path:
    raw = (value or "~/Desktop/AI-Agent-Workspace").strip()
    return Path(os.path.expandvars(os.path.expanduser(raw))).resolve()


def safe_path(path: str, workspace: Path, *, allow_missing: bool = True, restrict_to_workspace: bool | None = None) -> Path:
    if not path or not path.strip():
        raise ValueError("Path is required.")
    workspace = workspace.resolve()
    if restrict_to_workspace is None:
        restrict_to_workspace = os.getenv("FILE_SCOPE", "computer").strip().lower() != "computer"
    candidate = Path(os.path.expandvars(os.path.expanduser(path.strip())))
    if not candidate.is_absolute():
        candidate = workspace / candidate
    resolved = candidate.resolve(strict=False)
    if restrict_to_workspace:
        try:
            resolved.relative_to(workspace)
        except ValueError as exc:
            raise PermissionError(f"Path is outside the configured workspace: {resolved}") from exc
    if not allow_missing and not resolved.exists():
        raise FileNotFoundError(str(resolved))
    return resolved


def _is_text_file(path: Path) -> bool:
    if path.name.lower() in {"dockerfile", ".gitignore", ".env", ".editorconfig", "license", "makefile"}:
        return True
    if path.suffix.lower() in TEXT_EXTENSIONS:
        return True
    if path.is_dir():
        return False
    try:
        sample = path.read_bytes()[:4096]
    except OSError:
        return False
    return b"\x00" not in sample


def _display(path: Path, workspace: Path) -> str:
    try:
        rel = path.relative_to(workspace)
        if str(rel) == ".":
            return "."
        return str(Path(".") / rel)
    except ValueError:
        return str(path)


def list_files(path: str = ".", workspace: Path | None = None) -> dict[str, Any]:
    ws = resolve_workspace(str(workspace) if workspace else None)
    target = safe_path(path, ws, allow_missing=False)
    if not target.is_dir():
        raise NotADirectoryError(str(target))
    entries = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.casefold()))
    truncated = len(entries) > MAX_LIST_ITEMS
    entries = entries[:MAX_LIST_ITEMS]
    result = []
    for item in entries:
        try:
            stat = item.stat()
        except OSError:
            stat = None
        result.append({
            "name": item.name,
            "type": "directory" if item.is_dir() else "file",
            "size": None if item.is_dir() else (stat.st_size if stat else None),
            "path": str(item),
            "display_path": _display(item, ws),
        })
    return {
        "path": str(target),
        "items": result,
        "count": len(result),
        "truncated": truncated,
    }


def read_file(path: str, workspace: Path | None = None) -> dict[str, Any]:
    ws = resolve_workspace(str(workspace) if workspace else None)
    target = safe_path(path, ws, allow_missing=False)
    if not target.is_file():
        raise IsADirectoryError(str(target))
    if not _is_text_file(target):
        raise ValueError("Binary or unsupported file; IntignoAI only edits text-based files.")
    size = target.stat().st_size
    max_bytes = int(os.getenv("MAX_FILE_READ_BYTES", "1048576"))
    if size > max_bytes:
        raise ValueError(f"File is too large to read in one operation ({size} bytes > {max_bytes}).")
    try:
        content = target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        content = target.read_text(encoding="utf-8", errors="replace")
    lines = content.count("\n") + (1 if content else 0)
    return {
        "path": str(target),
        "display_path": _display(target, ws),
        "size": size,
        "lines": lines,
        "content": content,
    }


def create_file(path: str, content: str = "", workspace: Path | None = None) -> dict[str, Any]:
    ws = resolve_workspace(str(workspace) if workspace else None)
    target = safe_path(path, ws, allow_missing=True)
    if target.exists():
        raise FileExistsError(str(target))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return {
        "created": True,
        "path": str(target),
        "display_path": _display(target, ws),
        "size": target.stat().st_size,
    }


def update_file(path: str, content: str, workspace: Path | None = None) -> dict[str, Any]:
    ws = resolve_workspace(str(workspace) if workspace else None)
    target = safe_path(path, ws, allow_missing=False)
    if not target.is_file():
        raise IsADirectoryError(str(target))
    if not _is_text_file(target):
        raise ValueError("Binary file editing is not supported.")
    backup = target.with_name(target.name + ".intignoai.bak")
    try:
        shutil.copy2(target, backup)
    except OSError:
        backup = None
    target.write_text(content, encoding="utf-8")
    return {
        "updated": True,
        "path": str(target),
        "display_path": _display(target, ws),
        "size": target.stat().st_size,
        "backup": str(backup) if backup and backup.exists() else None,
    }


def delete_file(path: str, workspace: Path | None = None) -> dict[str, Any]:
    ws = resolve_workspace(str(workspace) if workspace else None)
    target = safe_path(path, ws, allow_missing=False)
    if not target.is_file():
        raise IsADirectoryError(str(target))
    target.unlink()
    return {"deleted": True, "path": str(target), "display_path": _display(target, ws)}


def create_directory(path: str, workspace: Path | None = None) -> dict[str, Any]:
    ws = resolve_workspace(str(workspace) if workspace else None)
    target = safe_path(path, ws, allow_missing=True)
    target.mkdir(parents=True, exist_ok=False)
    return {"created": True, "path": str(target), "display_path": _display(target, ws)}


def delete_directory(path: str, workspace: Path | None = None) -> dict[str, Any]:
    ws = resolve_workspace(str(workspace) if workspace else None)
    target = safe_path(path, ws, allow_missing=False)
    if not target.is_dir():
        raise NotADirectoryError(str(target))
    if target == ws:
        raise PermissionError("The configured workspace root cannot be deleted.")
    shutil.rmtree(target)
    return {"deleted": True, "path": str(target), "display_path": _display(target, ws)}


def move_file(source: str, destination: str, workspace: Path | None = None) -> dict[str, Any]:
    ws = resolve_workspace(str(workspace) if workspace else None)
    src = safe_path(source, ws, allow_missing=False)
    dst = safe_path(destination, ws, allow_missing=True)
    if src == dst:
        raise ValueError("Source and destination are the same.")
    if dst.exists():
        raise FileExistsError(str(dst))
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    return {"moved": True, "source": str(src), "destination": str(dst)}


def copy_file(source: str, destination: str, workspace: Path | None = None) -> dict[str, Any]:
    ws = resolve_workspace(str(workspace) if workspace else None)
    src = safe_path(source, ws, allow_missing=False)
    dst = safe_path(destination, ws, allow_missing=True)
    if not src.is_file():
        raise ValueError("copy_file expects a file source.")
    if dst.exists():
        raise FileExistsError(str(dst))
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return {"copied": True, "source": str(src), "destination": str(dst)}


def rename_file(path: str, new_name: str, workspace: Path | None = None) -> dict[str, Any]:
    ws = resolve_workspace(str(workspace) if workspace else None)
    src = safe_path(path, ws, allow_missing=False)
    name = Path(new_name).name
    if not name or name in {".", ".."} or name != new_name:
        raise ValueError("new_name must be a simple file/directory name without path separators.")
    dst = safe_path(str(src.parent / name), ws, allow_missing=True)
    if dst.exists():
        raise FileExistsError(str(dst))
    src.rename(dst)
    return {"renamed": True, "source": str(src), "destination": str(dst)}
