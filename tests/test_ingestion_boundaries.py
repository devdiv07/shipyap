"""Source-level boundaries for the ingestion slice, enforced by parsing the code, not by trust.

Whole-tree network and process rules are in test_boundaries."""

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "public_research_os"
INGESTION = SRC / "ingestion"
FORBIDDEN_BUILTINS = frozenset({"eval", "exec", "compile", "__import__"})
FORBIDDEN_ATTRIBUTES = frozenset(
    {"system", "popen", "import_module", "startfile", "spawnl", "spawnv", "execl", "execv"}
    | {"environ", "getenv", "putenv"}  # ingestion never reads the environment (no tokens)
)
CREDENTIAL_HEADERS = frozenset({"authorization", "proxy-authorization", "cookie", "x-api-key"})


def parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def test_ingestion_code_cannot_execute_or_import_content_or_read_the_environment():
    paths = sorted(INGESTION.glob("*.py"))
    assert len(paths) >= 8
    for path in paths:
        for node in ast.walk(parse(path)):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in FORBIDDEN_BUILTINS, (path.name, node.func.id)
            if isinstance(node, ast.Attribute):
                assert node.attr not in FORBIDDEN_ATTRIBUTES, (path.name, node.attr)


def test_no_credential_header_name_appears_as_a_string_value_in_ingestion_code():
    for path in sorted(INGESTION.glob("*.py")):
        for node in ast.walk(parse(path)):
            # Prose in docstrings is not an exact match; a header name or dict key would be.
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert node.value.strip().lower() not in CREDENTIAL_HEADERS, path.name
