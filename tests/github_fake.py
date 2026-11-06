"""In-process fake of the GitHub Git Data endpoints the reader uses. No sockets, no network.

All repository content here is synthetic. Blob ids are real Git object ids (so the reader's
recomputation is genuinely exercised); tree ids are deterministic labels, because the reader only
requires a response to carry the id that was asked for.
"""

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from shipyap.ingestion.github import TransportResponse, git_blob_id

REVISION = "0123456789abcdef0123456789abcdef01234567"
OWNER = "synthetic-owner"
REPO = "synthetic-repo"
WORKSPACE = "demo_workspace"
SUBMODULE_COMMIT = "c" * 40


@dataclass(frozen=True)
class Node:
    mode: str
    type: str
    data: bytes | None


def blob(data: bytes, mode: str = "100644") -> Node:
    return Node(mode=mode, type="blob", data=data)


def symlink(target: bytes = b"README.md") -> Node:
    return Node(mode="120000", type="blob", data=target)


def submodule() -> Node:
    return Node(mode="160000", type="commit", data=None)


def json_response(
    document: object, status: int = 200, headers: Mapping[str, str] | None = None
) -> TransportResponse:
    return TransportResponse(status, dict(headers or {}), json.dumps(document).encode("utf-8"))


def error_response(
    status: int, headers: Mapping[str, str] | None = None, message: str = "synthetic"
) -> TransportResponse:
    return json_response({"message": message}, status, headers)


def request_document(paths: list[str], **overrides: object) -> dict[str, object]:
    document: dict[str, object] = {
        "request_version": "1",
        "workspace_id": WORKSPACE,
        "owner": OWNER,
        "repo": REPO,
        "revision_sha": REVISION,
        "paths": paths,
    }
    document.update(overrides)
    return document


def request_bytes(paths: list[str], **overrides: object) -> bytes:
    return json.dumps(request_document(paths, **overrides)).encode("utf-8")


def tree_id(directory: str) -> str:
    return hashlib.sha1(b"tree:" + directory.encode("utf-8"), usedforsecurity=False).hexdigest()


class FakeGithub:
    """Implements the reader's `Transport` protocol over a synthetic repository."""

    def __init__(self, files: Mapping[str, bytes | Node], revision: str = REVISION) -> None:
        self.revision = revision
        self.nodes = {
            path: node if isinstance(node, Node) else blob(node) for path, node in files.items()
        }
        self.calls: list[tuple[str, str, int]] = []
        # Exact API path -> response, exception to raise, or function that edits the response.
        self.overrides: dict[str, object] = {}
        self._trees: dict[str, dict[str, object]] = {}
        self._blobs: dict[str, bytes] = {}
        self._build()

    # --- API paths a test may want to override or assert on -------------------------------
    def commit_path(self) -> str:
        return f"/repos/{OWNER}/{REPO}/git/commits/{self.revision}"

    def tree_path(self, directory: str = "") -> str:
        return f"/repos/{OWNER}/{REPO}/git/trees/{tree_id(directory)}"

    def blob_path(self, path: str) -> str:
        return f"/repos/{OWNER}/{REPO}/git/blobs/{self._blob_sha(path)}"

    def requested_paths(self) -> list[str]:
        return [call[0] for call in self.calls]

    def served_bytes(self, path: str) -> bytes:
        data = self.nodes[path].data
        assert data is not None
        return data

    # --- Transport protocol -----------------------------------------------------------------
    def get(self, path: str, accept: str, max_bytes: int, deadline: float) -> TransportResponse:
        self.calls.append((path, accept, max_bytes))
        override = self.overrides.get(path)
        if isinstance(override, Exception):
            raise override
        if isinstance(override, TransportResponse):
            return override
        response = self._route(path)
        if callable(override):
            edit: Callable[[TransportResponse], TransportResponse] = override
            return edit(response)
        return response

    # --- internals ----------------------------------------------------------------------------
    def _blob_sha(self, path: str) -> str:
        node = self.nodes[path]
        return SUBMODULE_COMMIT if node.data is None else git_blob_id(node.data)

    def _build(self) -> None:
        directories = {""}
        for path in self.nodes:
            parts = path.split("/")
            directories.update("/".join(parts[:depth]) for depth in range(1, len(parts)))
        for directory in directories:
            entries: list[dict[str, object]] = []
            for child in directories:
                if child and child.rpartition("/")[0] == directory:
                    entries.append(
                        {
                            "path": child.rpartition("/")[2],
                            "mode": "040000",
                            "type": "tree",
                            "sha": tree_id(child),
                            "url": "https://evil.example/ignored",
                        }
                    )
            for path, node in self.nodes.items():
                if path.rpartition("/")[0] != directory:
                    continue
                entry: dict[str, object] = {
                    "path": path.rpartition("/")[2],
                    "mode": node.mode,
                    "type": node.type,
                    "sha": self._blob_sha(path),
                    "url": "https://evil.example/ignored",
                }
                if node.data is not None:
                    entry["size"] = len(node.data)
                    self._blobs[self._blob_sha(path)] = node.data
                entries.append(entry)
            self._trees[tree_id(directory)] = {
                "sha": tree_id(directory),
                "truncated": False,
                "tree": entries,
            }

    def _route(self, path: str) -> TransportResponse:
        parts = path.split("/")
        # ["", "repos", owner, repo, "git", kind, sha]
        if len(parts) != 7 or parts[2:4] != [OWNER, REPO]:
            return error_response(404)
        kind, sha = parts[5], parts[6]
        if kind == "commits" and sha == self.revision:
            return json_response(
                {
                    "sha": self.revision,
                    "tree": {"sha": tree_id(""), "url": "https://evil.example/ignored"},
                    "message": "synthetic commit",
                    "html_url": "https://evil.example/ignored",
                }
            )
        if kind == "trees" and sha in self._trees:
            return json_response(self._trees[sha])
        if kind == "blobs" and sha in self._blobs:
            return TransportResponse(200, {}, self._blobs[sha])
        return error_response(404)
