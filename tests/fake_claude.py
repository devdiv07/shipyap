"""A stand-in for the `claude` CLI, run by scripts/model-plan.ps1 in the Windows-only tests.

It answers `--version`, `auth status --json` and print mode (`-p`) like Claude Code, using the
same fake model as the engine tests, and logs what it was given (arguments, working folder,
environment names, stdin) to FAKE_CLAUDE_LOG so the tests can check the wrapper's isolation.
FAKE_CLAUDE_AUTH: "ok" (default), "logged_out" or "api_key". FAKE_CLAUDE_SHAPE:
"structured" (default) or "result" (no structured_output: the JSON is in the result text).
"""

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))


def _log(entry: dict) -> None:
    path = os.environ.get("FAKE_CLAUDE_LOG")
    if path:
        with open(path, "a", encoding="utf-8") as log:
            log.write(json.dumps(entry) + "\n")


def main(argv: list[str]) -> int:
    auth = os.environ.get("FAKE_CLAUDE_AUTH", "ok")
    if argv == ["--version"]:
        print("2.1.318 (Claude Code)")
        return 0
    if argv[:2] == ["auth", "status"]:
        print(
            json.dumps(
                {
                    "loggedIn": auth != "logged_out",
                    "authMethod": "api_key" if auth == "api_key" else "claude.ai",
                    "apiProvider": "firstParty",
                    "subscriptionType": None if auth == "api_key" else "pro",
                }
            )
        )
        return 0 if auth != "logged_out" else 1
    stdin = sys.stdin.buffer.read().decode("utf-8")
    option = {argv[i]: argv[i + 1] for i in range(len(argv) - 1) if argv[i].startswith("--")}
    system = Path(option["--system-prompt-file"]).read_text(encoding="utf-8")
    _log(
        {
            "argv": argv,
            "cwd": os.getcwd(),
            "cwd_entries": sorted(os.listdir(".")),
            "env": sorted(k.upper() for k in os.environ),
            "stdin": stdin,
            "system": system,
        }
    )
    schema = json.loads(option["--json-schema"])  # fails loudly if quoting broke the JSON
    from test_automation import Model

    request = SimpleNamespace(output_schema=schema, user_text=stdin)
    text = Model().generate(request).texts[0]
    result = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "",
        "usage": {"input_tokens": len(stdin) // 4, "output_tokens": len(text) // 4},
        "modelUsage": {option["--model"]: {"outputTokens": len(text) // 4}},
    }
    if os.environ.get("FAKE_CLAUDE_SHAPE") == "result":
        result["result"] = f"```json\n{text}\n```"
    else:
        result["structured_output"] = json.loads(text)
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
