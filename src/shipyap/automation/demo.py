"""A fresh, synthetic-only review desk. No provider, credential or network client."""

import json
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from importlib.resources import files
from pathlib import Path

from shipyap.automation.config import Config
from shipyap.automation.initialise import DEMO_MARKER, initialise
from shipyap.drafting.story_draft import link_document, story_paths
from shipyap.editorial.bundle import verify_bundle
from shipyap.editorial.inputs import parse_brief, parse_response
from shipyap.editorial.models import BundleKind
from shipyap.editorial.service import run_brief, run_build
from shipyap.review.pages import ApprovalDefaults, DemoExample
from shipyap.review.service import ReviewDesk
from shipyap.stories.backlog import StoryBacklog
from shipyap.stories.discovery import run_discovery


@dataclass(frozen=True)
class DemoSession:
    config: Config
    desk: ReviewDesk
    story_id: str
    example: DemoExample


def _copy_resource(source, target: Path) -> None:
    if source.is_dir():
        target.mkdir()
        for child in source.iterdir():
            _copy_resource(child, target / child.name)
    else:
        target.write_bytes(source.read_bytes())


@contextmanager
def workspace() -> Iterator[DemoSession]:
    with tempfile.TemporaryDirectory(prefix="pros-demo-") as name:
        root = Path(name).resolve()
        config = initialise(root, "sample-owner/retry-lab", "+00:00")
        (root / DEMO_MARKER).write_text(
            "SYNTHETIC offline demo. Scheduling is disabled.\n", encoding="utf-8"
        )
        resources = root / "sample-data"
        _copy_resource(files("shipyap").joinpath("demo_data"), resources)
        snapshot = resources / "evidence"
        now = datetime.now(UTC)
        run_discovery(
            config.preferences, config.backlog, [snapshot], [resources / "story.json"], now
        )
        with StoryBacklog.open(config.backlog, create=False) as backlog:
            (story,) = backlog.stories()
        paths = story_paths(config.drafts, story.story_id)
        result = run_brief(snapshot, resources / "contribution.json", paths.brief)
        for version in ("before", "after"):
            path = resources / f"{version}.json"
            response = json.loads(path.read_text("utf-8"))
            response["brief_sha256"] = result.brief_sha256
            path.write_text(json.dumps(response), encoding="utf-8")
        before_path = root / "sample-before"
        before = run_build(snapshot, paths.brief, resources / "before.json", before_path)
        after = run_build(snapshot, paths.brief, resources / "after.json", paths.draft)
        if before.counts.blocking != 1 or after.counts.blocking != 0:
            raise ValueError("demo checks did not produce the expected result")
        before_bundle = verify_bundle(before_path, BundleKind.DRAFT)
        before_map = json.loads(before_bundle.files["assertion-map.json"])
        if not any(
            f["code"] == "NUMBER_NOT_IN_EVIDENCE" for f in before_map["segments"][0]["findings"]
        ):
            raise ValueError("demo did not catch its planted number")
        checked = verify_bundle(paths.draft, BundleKind.DRAFT)
        paths.link.mkdir()
        (paths.link / "story-link.json").write_bytes(
            link_document(story, paths, checked.record_sha256, checked.record)
        )
        with StoryBacklog.open(config.backlog, create=False) as backlog:
            backlog.mark_offered(story.story_id, now)
        brief = parse_brief(checked.files["brief.json"])
        original = parse_response((resources / "before.json").read_bytes())
        corrected = parse_response(checked.files["response.json"])
        sample_check = json.loads((resources / "fact-check.json").read_text(encoding="utf-8"))
        if sample_check.get("label") != "SAMPLE fact check (scripted)":
            raise ValueError("demo fact check is not labelled as sample data")
        sample_check["draft"] = paths.draft.name
        example = DemoExample(
            original.segments[0].text,
            corrected.segments[0].text,
            "README.md:3\n" + brief.source("README.md").lines[2],
            "NUMBER_NOT_IN_EVIDENCE: 9",
            after.counts.blocking,
        )
        desk = ReviewDesk(
            config.drafts,
            config.backlog,
            config.state / "review-ledger.sqlite",
            (snapshot,),
            eval_cases=config.state / "eval-cases",
            fact_checks=lambda sid: sample_check if sid == story.story_id else None,
            demo_mode=True,
        )
        gate = desk.gate(desk.item(story.story_id))
        if not gate.approvable:
            raise ValueError("corrected sample is not reviewable")
        yield DemoSession(config, desk, story.story_id, example)


def serve() -> int:
    from shipyap.review.server import ReviewServer

    try:
        with workspace() as demo:

            def defaults() -> ApprovalDefaults:
                due = (datetime.now(UTC) + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
                return ApprovalDefaults(
                    "0123456789abcdef01234567", due, "Synthetic demo destination"
                )

            with ReviewServer(
                demo.desk,
                0,
                defaults=defaults,
                demo_example=demo.example,
                stopped=lambda: (demo.config.workspace_root / "STOP").exists(),
            ) as server:
                print("SYNTHETIC SAMPLE DATA. Offline demo; scheduling is permanently disabled.")
                from shipyap.automation.launch import instruction

                instruction(server.start_url)
                print("Open the sample draft. Edits are temporary. Ctrl+C stops the demo.")
                sys.stdout.flush()
                try:
                    server.serve_forever()
                except KeyboardInterrupt:
                    pass
        return 0
    except Exception:
        print("DEMO_UNAVAILABLE: sample data, temporary storage or local port could not be opened.")
        return 2
