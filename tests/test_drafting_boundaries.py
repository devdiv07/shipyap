"""Source-level boundaries of the drafting package, enforced by parsing the code.

Whole-tree rules (network, processes, environment, lazy provider import) are in
test_boundaries. This adds what is specific to drafting: no dynamic code, no route to the
publication domain and no direct disk writes (bundles are published only through the editorial
path).
"""

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "public_research_os"
DRAFTING = SRC / "drafting"
FORBIDDEN_BUILTINS = frozenset({"eval", "exec", "compile", "__import__", "open"})
FORBIDDEN_ATTRIBUTES = frozenset(
    {"system", "popen", "import_module", "putenv"}
    | {"write", "write_bytes", "write_text", "mkdir", "makedirs", "touch", "rmtree"}
)
FORBIDDEN_OS = frozenset({"open", "write", "rename", "replace", "unlink", "remove", "rmdir"})


def parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def modules() -> list[Path]:
    paths = sorted(DRAFTING.glob("*.py"))
    assert len(paths) >= 7  # the scan really covers the package
    return paths


def test_drafting_has_no_dynamic_code_publication_route_or_direct_disk_writes():
    for path in modules():
        for node in ast.walk(parse(path)):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in FORBIDDEN_BUILTINS, (path.name, node.func.id)
            if isinstance(node, ast.Attribute):
                assert node.attr not in FORBIDDEN_ATTRIBUTES, (path.name, node.attr)
                if isinstance(node.value, ast.Name) and node.value.id == "os":
                    assert node.attr not in FORBIDDEN_OS | {"getenv"}, (path.name, node.attr)
            if isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith("public_research_os.publication"), path.name
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith("public_research_os.publication"), path.name
