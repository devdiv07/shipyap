"""Source-level boundaries of the editorial package, enforced by parsing the code.

The whole-tree scan in test_ingestion_boundaries already forbids network and process modules
everywhere outside ingestion/transport.py. These checks add what is specific to editorial: no
environment or credential access, no dynamic code, no route to the publication domain, and no
disk writes except through the shared, verified Assignment 02 writer.
"""

import ast
from pathlib import Path

from shipyap.editorial.models import LINKEDIN
from shipyap.publication.models import Channel

EDITORIAL = Path(__file__).resolve().parents[1] / "src" / "shipyap" / "editorial"
FORBIDDEN_BUILTINS = frozenset({"eval", "exec", "compile", "__import__", "open"})
FORBIDDEN_ATTRIBUTES = frozenset(
    {"environ", "getenv", "putenv", "system", "popen", "import_module"}
    | {"write", "write_bytes", "write_text", "mkdir", "makedirs", "touch", "rmtree"}
)
# Writes through the os module (str.replace and friends share the bare names, so qualify them).
FORBIDDEN_OS = frozenset({"open", "write", "rename", "replace", "unlink", "remove", "rmdir"})


def modules() -> list[Path]:
    paths = sorted(EDITORIAL.glob("*.py"))
    assert len(paths) >= 9  # the scan really covers the package
    return paths


def test_editorial_has_no_environment_dynamic_code_or_direct_disk_writes():
    for path in modules():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in FORBIDDEN_BUILTINS, (path.name, node.func.id)
            if isinstance(node, ast.Attribute):
                assert node.attr not in FORBIDDEN_ATTRIBUTES, (path.name, node.attr)
                if isinstance(node.value, ast.Name) and node.value.id == "os":
                    assert node.attr not in FORBIDDEN_OS, (path.name, node.attr)


def test_editorial_cannot_reach_the_publication_domain():
    for path in modules():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith("shipyap.publication"), path.name
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith("shipyap.publication"), path.name


def test_the_only_channel_is_linkedin_and_matches_the_publication_contract():
    assert LINKEDIN == Channel.LINKEDIN.value
