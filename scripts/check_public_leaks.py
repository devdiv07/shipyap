"""Scan every file in a release tree using the private, shared marker policy.

The policy itself stays in the dev repository: exporting it would disclose the markers.
No file is exempt. Only the exact author field/copyright notice and the README's own
`git clone https://github.com/<owner>/shipyap.git` line may be masked when requested.
"""

import argparse
import json
import re
import stat
import tomllib
from pathlib import Path

POLICY = Path(__file__).with_name("public_leak_markers.json")


def _normal(text: str) -> str:
    return re.sub(r"\\+", "/", text.replace("\\/", "/")).casefold()


def _mask_author(text: str, relative: str, author: str) -> str:
    text = text.replace("\r\n", "\n")
    if relative == "LICENSE":
        return re.sub(
            rf"(?m)^(Copyright \(c\) 2026 ){re.escape(author)}$", r"\1AUTHOR", text, count=1
        )
    if relative != "pyproject.toml":
        return text
    try:
        if tomllib.loads(text).get("project", {}).get("authors") != [{"name": author}]:
            return text
    except tomllib.TOMLDecodeError:
        return text
    # Prove the replacement changes the parsed author field, not a lookalike inside a
    # multiline description or another table. All other occurrences remain visible.
    lines = text.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if line.strip() == f'authors = [{{ name = "{author}" }}]':
            changed = lines.copy()
            changed[index] = line.replace(author, "AUTHOR")
            candidate = "".join(changed)
            if tomllib.loads(candidate)["project"]["authors"] == [{"name": "AUTHOR"}]:
                return candidate
    return text


def _mask_clone_url(text: str, relative: str, login: str) -> str:
    # The public repository's own clone command, as one whole README line, once. Any other
    # occurrence of the login (another URL, prose, another file) stays visible.
    if relative != "README.md":
        return text
    line = f"git clone https://github.com/{login}/shipyap.git"
    return re.sub(rf"(?m)^{re.escape(line)}$", "git clone OWNER_REPOSITORY_URL", text, count=1)


def scan(root: Path, *, allow_project_author: bool = False, policy: Path = POLICY) -> list[str]:
    config = json.loads(policy.read_text("utf-8"))
    markers = {key: _normal(value) for key, value in config["literals"].items()}
    root = root.resolve(strict=True)
    failures = []
    pending = [root]
    while pending:
        directory = pending.pop()
        for path in sorted(directory.iterdir()):
            relative = path.relative_to(root).as_posix()
            for key, marker in markers.items():
                if marker in _normal(relative):
                    failures.append(f"{relative}: {key} (path)")
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                failures.append(f"{relative}: LINK_NOT_ALLOWED")
                continue
            if path.is_dir():
                pending.append(path)
                continue
            if not stat.S_ISREG(info.st_mode):
                failures.append(f"{relative}: NOT_A_REGULAR_FILE")
                continue
            raw = path.read_bytes()
            # Inspect UTF-8 and both UTF-16 byte orders, including files without a BOM.
            for encoding in ("utf-8-sig", "utf-16-le", "utf-16-be"):
                text = raw.decode(encoding, errors="replace").lstrip("\ufeff")
                if allow_project_author:
                    text = _mask_author(text, relative, config["author_name"])
                    text = _mask_clone_url(text, relative, config["literals"]["owner_login"])
                normal = _normal(text)
                for key, marker in markers.items():
                    if marker in normal:
                        failures.append(f"{relative}: {key}")
    return sorted(set(failures))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--allow-project-author", action="store_true")
    args = parser.parse_args()
    try:
        failures = scan(args.root, allow_project_author=args.allow_project_author)
    except (OSError, ValueError):
        print("PUBLIC_LEAK_CHECK_FAILED: input or policy unreadable")
        return 2
    for failure in failures:
        print(f"PUBLIC_LEAK: {failure}")
    print(f"PUBLIC_LEAK_CHECK: {'FAIL' if failures else 'PASS'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
