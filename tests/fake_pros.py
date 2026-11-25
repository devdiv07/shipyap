"""A stand-in for `pros`, run by scripts/run-and-notify.ps1 and scripts/model-plan.ps1 in the
Windows-only tests. Every command but `run` is the real CLI (so `pros settings` reads the real
config). `run` reads no network: it plays the rounds listed in FAKE_PROS_RUNS (a JSON list of
{"exit": n, "notify": [lines] or null}, one per call, the last one repeating), writes notify.txt
in the configured workspace, and logs its arguments to FAKE_PROS_LOG.
"""

import json
import os
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    if not argv or argv[0] != "run":
        from shipyap.automation.cli import main as pros

        return pros(argv)
    from shipyap.automation.config import load_config

    log = Path(os.environ["FAKE_PROS_LOG"])
    done = log.read_text(encoding="utf-8").splitlines() if log.is_file() else []
    with log.open("a", encoding="utf-8") as out:
        out.write(json.dumps(argv) + "\n")
    rounds = json.loads(Path(os.environ["FAKE_PROS_RUNS"]).read_text(encoding="utf-8"))
    step = rounds[min(len(done), len(rounds) - 1)]
    root = load_config(Path(argv[argv.index("--config") + 1])).workspace_root
    if step.get("notify") is not None:
        (root / "notify.txt").write_text("".join(f"{line}\n" for line in step["notify"]), "utf-8")
    if step.get("say"):
        print(step["say"])
    return step["exit"]


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
