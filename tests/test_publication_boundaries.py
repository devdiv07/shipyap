"""Source-level boundaries of the Buffer publisher, enforced by parsing the code.

Whole-tree rules (network, processes, environment, which module names the publisher variables,
lazy adapter import) are in test_boundaries. This adds what is specific to publication: the
publisher cannot reach drafting or ingestion, and has no dynamic code or process access.
"""

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "public_research_os"
PUBLICATION = SRC / "publication"


def parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def test_the_publisher_and_the_drafter_cannot_reach_each_other():
    modules = sorted(PUBLICATION.glob("*.py"))
    assert len(modules) >= 8
    for path in modules:
        for node in ast.walk(parse(path)):
            names = []
            if isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            elif isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            for name in names:
                assert not name.startswith("public_research_os.drafting"), path.name
                assert not name.startswith("public_research_os.ingestion"), path.name
                assert "anthropic" not in name, path.name


def test_the_publisher_has_no_dynamic_code_or_process_access():
    forbidden = {"eval", "exec", "compile", "__import__"}
    for path in sorted(PUBLICATION.glob("*.py")):
        for node in ast.walk(parse(path)):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in forbidden, (path.name, node.func.id)
            if isinstance(node, ast.Attribute):
                assert node.attr not in {"system", "popen", "import_module"}, path.name
