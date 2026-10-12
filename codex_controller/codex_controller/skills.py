"""Discover administrator-installed skills without giving the model file access."""
from __future__ import annotations

import ast
from dataclasses import dataclass
import os
from pathlib import Path
import re
import stat
from typing import Any

from .tool_catalog import ToolDefinition

SKILL_NAMES = frozenset({"controller_skill_list", "controller_skill_read"})
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


class SkillError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def private_text(root: Path, relative: str, limit: int = 65536) -> str:
    """Only bounded regular files; no symlink in any path component."""
    if any(part in {".", "..", ""} for part in relative.split("/")):
        raise SkillError("skill_path_invalid")
    path = Path(relative)
    if path.is_absolute() or not path.parts or any(part in {".", ".."} for part in path.parts):
        raise SkillError("skill_path_invalid")
    # Resolve no attacker-selected symlink, including the managed root itself.
    current = root
    if root.is_symlink():
        raise SkillError("skill_path_invalid")
    directory = None
    try:
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        for part in path.parts[:-1]:
            next_directory = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = next_directory
        descriptor = os.open(path.parts[-1], os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=directory)
        with os.fdopen(descriptor, "rb") as stream:
            metadata = os.fstat(stream.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > limit:
                raise SkillError("skill_file_invalid")
            data = stream.read(limit + 1)
        if len(data) > limit:
            raise SkillError("skill_file_invalid")
        return data.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise SkillError("skill_file_unavailable") from exc
    finally:
        if directory is not None:
            os.close(directory)



@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    folder: str
    root: Path


def metadata(text: str) -> tuple[str, str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise SkillError("skill_metadata_invalid")
    try:
        end = lines.index("---", 1)
    except ValueError as exc:
        raise SkillError("skill_metadata_invalid") from exc
    fields: dict[str, str] = {}
    for index, line in enumerate(lines[1:end], 1):
        match = re.match(r"^(name|description):\s*(.*)$", line)
        if match is None:
            continue
        key, value = match.groups()
        if key in fields:
            raise SkillError("skill_metadata_invalid")
        if value in {"|", ">", "|-", ">-"}:
            continuation = []
            for child in lines[index + 1:end]:
                if child and not child[0].isspace():
                    break
                continuation.append(child.strip())
            value = " ".join(continuation)
        elif value.startswith(('"', "'")):
            try:
                value = ast.literal_eval(value)
            except (ValueError, SyntaxError) as exc:
                raise SkillError("skill_metadata_invalid") from exc
        if not isinstance(value, str):
            raise SkillError("skill_metadata_invalid")
        fields[key] = value.strip()
    name, description = fields.get("name", ""), fields.get("description", "")
    if not NAME_RE.fullmatch(name) or not 1 <= len(description) <= 1000:
        raise SkillError("skill_metadata_invalid")
    return name, description


class SkillCatalog:
    def __init__(self, root: Path, *, additional_roots: tuple[Path, ...] = (), boundary: Path | None = None):
        self.root = root
        self.roots = (root, *additional_roots)
        self.boundary = boundary
        self.skills: dict[str, Skill] = {}
        self.errors = 0

    def refresh(self) -> None:
        skills: dict[str, Skill] = {}
        self.errors = 0
        folders: list[tuple[Path, Path]] = []
        for root in self.roots:
            if self.boundary is not None:
                try:
                    relative = root.relative_to(self.boundary)
                    current = self.boundary
                    for part in relative.parts:
                        current = current / part
                        if current.is_symlink():
                            raise ValueError()
                except ValueError:
                    self.errors += 1
                    continue
            if not root.exists():
                continue
            if root.is_symlink() or not root.is_dir():
                self.errors += 1
                continue
            try:
                folders.extend((root, folder) for folder in sorted(root.iterdir()))
            except OSError:
                self.errors += 1
        duplicates: set[str] = set()
        for root, folder in folders[:128]:
            if not folder.is_dir() or not NAME_RE.fullmatch(folder.name):
                continue
            try:
                name, description = metadata(private_text(root, f"{folder.name}/SKILL.md"))
                if name in skills or name in duplicates:
                    skills.pop(name, None)
                    duplicates.add(name)
                    self.errors += 1
                    continue
                skills[name] = Skill(name, description, folder.name, root)
            except SkillError:
                self.errors += 1
        self.errors += max(0, len(folders) - 128)
        self.skills = skills

    def index(self) -> list[dict[str, str]]:
        return [{"name": skill.name, "description": skill.description} for skill in self.skills.values()]

    def definitions(self) -> tuple[ToolDefinition, ...]:
        if not self.skills:
            return ()
        return (
            ToolDefinition("controller_skill_list", "查看已安装流程", "controller_skills", "read_only",
                           "列出管理员安装的可用 skill 名称与描述。按语义选择后先读取流程。", ("有哪些可用技能",),
                           input_schema={"type": "object", "properties": {}, "additionalProperties": False}, requires_job_context=True),
            ToolDefinition("controller_skill_read", "读取技能流程", "controller_skills", "read_only",
                           "按 name 读取 SKILL.md 或 references 下的文本。只读取说明，不执行脚本或授予新权限。", ("读取匹配的技能流程",),
                           input_schema={"type": "object", "properties": {"name": {"type": "string", "maxLength": 64}, "resource": {"type": "string", "maxLength": 160}},
                                         "required": ["name"], "additionalProperties": False}, requires_job_context=True),
        )

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "controller_skill_list" and arguments == {}:
            return {"skills": self.index(), "invalid_skills": self.errors}
        if name != "controller_skill_read" or set(arguments) - {"name", "resource"}:
            raise SkillError("skill_arguments_invalid")
        skill = self.skills.get(arguments.get("name")) if isinstance(arguments.get("name"), str) else None
        if skill is None:
            raise SkillError("skill_not_found")
        resource = arguments.get("resource", "SKILL.md")
        if not isinstance(resource, str) or len(resource) > 160 or (resource != "SKILL.md" and not resource.startswith("references/")):
            raise SkillError("skill_path_invalid")
        if resource != "SKILL.md" and Path(resource).suffix not in {".md", ".txt", ".json", ".csv", ".yaml", ".yml"}:
            raise SkillError("skill_path_invalid")
        return {"name": skill.name, "resource": resource, "content": private_text(skill.root, f"{skill.folder}/{resource}")}
