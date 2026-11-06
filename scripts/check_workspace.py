"""Standard-library audit of workspace files and an optional complete agent kit."""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REQUIRED = (
    "AGENTS.md",
    "CLAUDE.md",
    "pyproject.toml",
    "docs/architecture.md",
    "docs/security/threat-model.md",
    "src/public_research_os/publication/port.py",
)
AGENT_KIT = ("prompts", "skills-src", ".claude", ".agents")
KIT_REQUIRED = (
    "prompts/01-first-vertical-slice-claude.md",
    "prompts/02-independent-review-codex.md",
)


def files_in(folder: Path) -> dict[str, bytes]:
    return {str(p.relative_to(folder)): p.read_bytes() for p in folder.rglob("*") if p.is_file()}


def check() -> list[str]:
    errors = []
    for relative in REQUIRED:
        if not (ROOT / relative).is_file():
            errors.append(f"Missing file: {relative}")
    kit_present = any((ROOT / path).exists() for path in AGENT_KIT)
    if kit_present:
        for relative in AGENT_KIT:
            if not (ROOT / relative).is_dir():
                errors.append(f"Missing agent-kit directory: {relative}")
        for relative in KIT_REQUIRED:
            if not (ROOT / relative).is_file():
                errors.append(f"Missing file: {relative}")
    json_files = (".vscode/settings.json", ".vscode/tasks.json")
    if kit_present:
        json_files += (".claude/settings.json",)
    for path in json_files:
        try:
            json.loads((ROOT / path).read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            errors.append(f"Invalid JSON: {path}: {exc}")
    if not kit_present:
        return errors
    source = files_in(ROOT / "skills-src")
    if not list((ROOT / "skills-src").glob("*/SKILL.md")):
        errors.append("Agent kit has no skill manifests")
    for relative in (".claude/skills", ".agents/skills"):
        if not (ROOT / relative).is_dir():
            errors.append(f"Missing skill mirror directory: {relative}")
        mirrored = files_in(ROOT / relative)
        if source != mirrored:
            errors.append(f"Skill mirror differs: {relative}; run python scripts/sync_skills.py")
    for path in (ROOT / "skills-src").glob("*/SKILL.md"):
        body = path.read_text(encoding="utf-8")
        if not (body.startswith("---\n") and "\nname:" in body and "\ndescription:" in body):
            errors.append(f"Invalid skill metadata: {path}")
    return errors


if __name__ == "__main__":
    problems = check()
    if problems:
        for item in problems:
            print(f"ERROR: {item}", file=sys.stderr)
        raise SystemExit(1)
    print("PASS: required workspace files, JSON and any included agent kit")
