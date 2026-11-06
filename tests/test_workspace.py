import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import check_workspace as audit  # noqa: E402


def test_repo_structure_and_skills_are_consistent(tmp_path, monkeypatch):
    assert audit.check() == []
    for relative in audit.REQUIRED:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("synthetic fixture", encoding="utf-8")
    for relative in (".vscode/settings.json", ".vscode/tasks.json"):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(audit, "ROOT", tmp_path)
    assert audit.check() == []  # A public checkout has no optional private agent kit.
    (tmp_path / "skills-src").mkdir()
    assert audit.check()  # A partial kit must not silently bypass its checks.
    for relative in audit.AGENT_KIT:
        (tmp_path / relative).mkdir(exist_ok=True)
    for relative in audit.KIT_REQUIRED:
        (tmp_path / relative).write_text("synthetic prompt", encoding="utf-8")
    (tmp_path / ".claude/settings.json").write_text("{}", encoding="utf-8")
    assert "Agent kit has no skill manifests" in audit.check()
    assert "Missing skill mirror directory: .claude/skills" in audit.check()
    manifest = "---\nname: sample\ndescription: synthetic fixture\n---\n"
    for relative in ("skills-src", ".claude/skills", ".agents/skills"):
        directory = tmp_path / relative / "sample"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "SKILL.md").write_text(manifest, encoding="utf-8")
    assert audit.check() == []
