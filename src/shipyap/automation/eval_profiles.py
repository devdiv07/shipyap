"""Profile selection only. The existing replay, normaliser and gates are unchanged."""

import json
import stat
from pathlib import Path


def public_cases(base: Path) -> Path:
    """Public cases cannot refer to private siblings, links or a workspace database."""
    try:
        root = base / "public"
        pending = [root]
        while pending:
            path = pending.pop()
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError
            if stat.S_ISDIR(info.st_mode):
                pending.extend(path.iterdir())
            elif not stat.S_ISREG(info.st_mode):
                raise ValueError
        root = root.resolve(strict=True)

        def contained(value: object) -> None:
            if not isinstance(value, str) or not value or "\\" in value:
                raise ValueError
            relative = Path(value)
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError
            target = (root / relative).resolve(strict=True)
            if not target.is_relative_to(root):
                raise ValueError

        listing = json.loads((root / "drafts.json").read_text("utf-8"))
        expected = json.loads((root / "expected.json").read_text("utf-8"))
        if not listing["cases"] or not expected["drafts"] or not expected["stories"]:
            raise ValueError
        for item in listing["cases"]:
            for key in ("brief", "response", "fact_check", "repair_fixture"):
                if key in item:
                    contained(item[key])
        stories = tuple((root / "stories").glob("*.json"))
        if not stories:
            raise ValueError
        for path in stories:
            contained("snapshots/" + json.loads(path.read_text("utf-8"))["snapshot"])
        for path in (root / "publications").glob("*.history.json"):
            value = json.loads(path.read_text("utf-8"))
            contained(value["original_response"])
            contained(value["brief"])
        return root
    except (OSError, ValueError, TypeError, KeyError):
        raise ValueError("PUBLIC_CASES_INVALID") from None
