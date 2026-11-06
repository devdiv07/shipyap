"""Whole-tree boundaries, enforced by parsing the code, not by trust.

- Network code only where it is needed: the GitHub transport, the provider SDK in
  drafting/provider.py, the standard-library HTTP and TLS modules in publication/buffer.py, and the
  local HTTP server in review/server.py (127.0.0.1, no requests; test_review checks).
- Python never starts a process or a browser; notifications go through the PowerShell wrapper.
- The environment is read in exactly two places, one per credential: drafting/provider.py and
  publication/buffer.py. Only those modules name their variables.
- Nothing imports the provider or the Buffer adapter at module level, so offline commands never
  load the SDK or the HTTP stack.

Package-specific rules (dynamic code, disk writes, cross-domain imports) stay in the
test_<package>_boundaries files.
"""

import ast
from pathlib import Path

from shipyap.drafting.provider import API_KEY_ENV as MODEL_KEY_ENV
from shipyap.publication.buffer import API_KEY_ENV as BUFFER_KEY_ENV
from shipyap.publication.buffer import PUBLISHING_DISABLED_ENV

SRC = Path(__file__).resolve().parents[1] / "src" / "shipyap"
TRANSPORT = SRC / "ingestion" / "transport.py"
PROVIDER = SRC / "drafting" / "provider.py"
BUFFER = SRC / "publication" / "buffer.py"
BUFFER_NETWORK = frozenset({"http", "ssl"})
REVIEW_SERVER = SRC / "review" / "server.py"
REVIEW_NETWORK = frozenset({"http", "urllib"})
LAZY_MODULES = ("shipyap.drafting.provider", "shipyap.publication.buffer")

NETWORK_MODULES = frozenset(
    {
        "socket",
        "ssl",
        "http",
        "urllib",
        "urllib3",
        "requests",
        "httpx",
        "httpx2",
        "httpcore",
        "httpcore2",
        "h11",
        "anthropic",
        "aiohttp",
        "asyncio",
        "ftplib",
        "smtplib",
        "telnetlib",
        "xmlrpc",
        "websocket",
        "websockets",
    }
)
EXECUTION_MODULES = frozenset({"subprocess", "ctypes", "pty", "multiprocessing", "webbrowser"})
PROCESS_ATTRIBUTES = frozenset(
    {"system", "popen", "startfile", "spawnl", "spawnv", "execl", "execv", "execvp"}
)


def parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def sources() -> list[Path]:
    paths = sorted(SRC.rglob("*.py"))
    assert {TRANSPORT, PROVIDER, BUFFER, REVIEW_SERVER} <= set(paths) and len(paths) > 15
    return paths


def imported_top_level(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    return names


def string_constants(name: str) -> set[Path]:
    return {
        path
        for path in sources()
        for node in ast.walk(parse(path))
        if isinstance(node, ast.Constant) and node.value == name
    }


def test_network_code_only_where_needed_and_no_module_starts_a_process():
    assert {"http", "ssl"} <= imported_top_level(parse(TRANSPORT))
    assert imported_top_level(parse(PROVIDER)) & NETWORK_MODULES == {"anthropic"}
    assert imported_top_level(parse(BUFFER)) & NETWORK_MODULES == BUFFER_NETWORK
    assert imported_top_level(parse(REVIEW_SERVER)) & NETWORK_MODULES == REVIEW_NETWORK
    allowed = {
        TRANSPORT: NETWORK_MODULES,
        PROVIDER: frozenset({"anthropic"}),
        BUFFER: BUFFER_NETWORK,
        REVIEW_SERVER: REVIEW_NETWORK,
    }
    for path in sources():
        tree = parse(path)
        imported = imported_top_level(tree)
        name = path.relative_to(SRC).as_posix()
        assert not imported & EXECUTION_MODULES, name
        assert not imported & (NETWORK_MODULES - allowed.get(path, frozenset())), name
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                assert node.attr not in PROCESS_ATTRIBUTES, (name, node.attr)


def test_the_environment_is_read_only_by_the_provider_and_the_buffer_module():
    readers = []
    for path in sources():
        for node in ast.walk(parse(path)):
            if isinstance(node, ast.Attribute) and node.attr in {"environ", "getenv", "putenv"}:
                readers.append((path, node.attr))
    assert readers == [(PROVIDER, "environ"), (BUFFER, "environ")]


def test_only_the_owning_module_names_each_environment_variable():
    """publication/errors.py has an error CODE with the same text as the switch; it reads
    nothing."""
    assert (BUFFER_KEY_ENV, PUBLISHING_DISABLED_ENV) == (
        "PROS_BUFFER_API_KEY",
        "PUBLISHING_DISABLED",
    )
    assert string_constants(MODEL_KEY_ENV) == {PROVIDER}
    assert string_constants(BUFFER_KEY_ENV) == {BUFFER}
    assert string_constants(PUBLISHING_DISABLED_ENV) == {BUFFER, SRC / "publication" / "errors.py"}


def test_nothing_imports_the_provider_or_the_buffer_adapter_at_module_level():
    """The automation imports each inside the function that needs it, after its paid or
    scheduling gate."""
    for path in sources():
        for node in parse(path).body:
            if isinstance(node, ast.ImportFrom) and node.module:
                names = {node.module, *(f"{node.module}.{a.name}" for a in node.names)}
            elif isinstance(node, ast.Import):
                names = {a.name for a in node.names}
            else:
                continue
            for module in LAZY_MODULES:
                assert module not in names, (path, module)
