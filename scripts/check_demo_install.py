"""Offline smoke gate for an installed wheel; shell/CI performs build and installation.

Usage: python -B scripts/check_demo_install.py <installed-package-directory>
The locked venv supplies dependencies. The application itself must load from that directory.
"""

import argparse
import importlib.util
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def forbidden(*args, **kwargs):
    raise AssertionError("demo touched credentials or a provider")


def audit(event, args):
    if event in {"socket.connect", "socket.connect_ex", "socket.getaddrinfo", "subprocess.Popen"}:
        raise AssertionError("demo attempted outbound networking, DNS or process creation")
    if event in {"os.system", "os.posix_spawn", "os.exec", "os.fork"}:
        raise AssertionError("demo attempted process creation")
    if event == "socket.bind" and args[1][0] != "127.0.0.1":
        raise AssertionError("demo tried to listen beyond loopback")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("installed", type=Path)
    parser.add_argument("--leak-policy", type=Path)
    args = parser.parse_args()
    installed = args.installed.resolve(strict=True)
    sys.path.insert(0, str(installed))
    sys.addaudithook(audit)
    import shipyap
    from shipyap.automation.cli import main as cli
    from shipyap.drafting import provider
    from shipyap.publication import buffer
    from shipyap.review.server import ReviewServer

    assert Path(shipyap.__file__).resolve().is_relative_to(installed)
    resources = installed / "shipyap/demo_data"
    source = ROOT / "src/shipyap/demo_data"

    def contents(directory):
        return {
            p.relative_to(directory).as_posix(): p.read_bytes()
            for p in directory.rglob("*")
            if p.is_file()
        }

    assert contents(resources) == contents(source) and contents(resources)
    if args.leak_policy is not None:
        spec = importlib.util.spec_from_file_location(
            "leak_check", ROOT / "scripts/check_public_leaks.py"
        )
        check = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(check)
        assert check.scan(resources, policy=args.leak_policy) == []
    roots = []

    def inspect(server):
        desk = server.desk
        assert desk.demo_mode and server.scheduler is None
        assert server.demo_example.corrected_blocks == 0
        assert "9/10" in server.demo_example.original and "3/10" in server.demo_example.corrected
        items, refused = desk.queue()
        assert not items and len(refused) == 1 and refused[0].reason.value == "HELD_BACK"
        item = desk.item(refused[0].story_id)
        assert item.state.value == "UNAPPROVED"
        assert desk.fact_check(item)["label"] == "SAMPLE fact check (scripted)"
        assert not desk.gate(item).approvable and "9/10" in item.current.text
        texts = {s.id: s.text for s in item.current.response.segments}
        texts["S01"] = server.demo_example.corrected
        desk.save_edit(item.story.story_id, texts, 0, datetime.now(UTC))
        assert desk.gate(desk.item(item.story.story_id)).approvable
        items, refused = desk.queue()
        assert len(items) == 1 and not refused
        roots.append(desk.drafts.parent)
        raise KeyboardInterrupt

    # Loaders are poisoned, not supplied with keys. A real key in the launching shell cannot
    # be inspected or used along this path. All outbound socket/DNS attempts fail above.
    with (
        patch.object(provider, "load_api_key", forbidden),
        patch.object(provider, "AnthropicDrafter", forbidden),
        patch.object(buffer, "load_api_key", forbidden),
        patch.object(buffer, "publishing_enabled", forbidden),
        patch.object(buffer, "HttpsBufferTransport", forbidden),
        patch.object(buffer, "BufferPublisher", forbidden),
        patch.object(ReviewServer, "serve_forever", inspect),
        tempfile.TemporaryDirectory(prefix="pros-installed-smoke-") as name,
    ):
        import os

        previous = Path.cwd()
        try:
            os.chdir(name)
            assert not Path.cwd().is_relative_to(ROOT)
            assert cli(["demo"]) == 0
            assert cli(["demo"]) == 0
        finally:
            os.chdir(previous)
    assert len(roots) == 2 and roots[0] != roots[1] and all(not p.exists() for p in roots)
    print("INSTALLED_DEMO_CHECK: PASS (two launches, resources, no credentials/egress, cleanup)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
