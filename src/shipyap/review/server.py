"""The local HTTP interface of `pros review`: standard library only, bound to 127.0.0.1.

Exposure and request rules:
- The listening address is the constant HOST; there is no option to bind anything else.
- Every request must carry `Host: 127.0.0.1:<port>`, so a DNS-rebinding page cannot reach it.
- Every page needs the session cookie, set only by opening the one-time URL printed at start
  (`/?token=...`). The token is random per launch and compared in constant time.
- Every POST also needs the token as a form field, a same-origin `Origin` (when the browser sends
  one; `null` only with `Sec-Fetch-Site: same-origin`), a urlencoded body under MAX_BODY_BYTES
  and each field at most once. The cookie is `SameSite=Strict` and `HttpOnly`.
- Responses forbid scripts, framing, caching and referrers. Errors show fixed codes, never a
  traceback, path content or the token.
- Scheduling is off unless the server is given a `scheduler` (a provider factory). Only the CLI
  builds one, after both start gates; no GET, page render or other action calls it. The schedule
  action is a POST like the others, under every rule above, and answers with a redirect, so a
  browser refresh repeats the GET, never the request.

This is a barrier against other sites and accidents. It is not authentication: any local
process that can read the printed URL or the terminal can act as the owner.
"""

import hmac
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlsplit

from shipyap.editorial.errors import EditorialError
from shipyap.publication.approval import Candidate, confirmation_phrase
from shipyap.publication.errors import PublicationError, PublicationErrorCode
from shipyap.publication.history import fingerprint
from shipyap.publication.ledger import ReviewSubject
from shipyap.publication.port import SchedulingProvider
from shipyap.review.pages import (
    STOP_MESSAGE,
    ApprovalDefaults,
    DemoExample,
    Page,
    StoryView,
    message_page,
    queue_page,
    story_page,
)
from shipyap.review.service import (
    ReviewDesk,
    ReviewError,
    ReviewErrorCode,
    x_digest,
)
from shipyap.review.x_post import XPost
from shipyap.stories.errors import StoryError

HOST = "127.0.0.1"
COOKIE = "pros_review"
MAX_BODY_BYTES = 256 * 1024
MAX_FIELDS = 200
_HEADERS = (
    (
        "Content-Security-Policy",
        "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
        "frame-ancestors 'none'; base-uri 'none'",
    ),
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "DENY"),
    ("Referrer-Policy", "no-referrer"),
    ("Cache-Control", "no-store"),
)
_Failure = (ReviewError, PublicationError, EditorialError, StoryError)
_HALTED_BY_STOP = frozenset(
    {"sheet", "repost-sheet", "approve", "schedule"}
)  # edit and reject send nothing


@dataclass
class Confirmation:
    key: str
    subject: ReviewSubject
    seq: int
    candidate: Candidate
    x_post: XPost
    completed_seq: int | None = None
    warning: tuple[str, ...] = ()
    repost_history: str | None = None


class ReviewServer(HTTPServer):
    def __init__(
        self,
        desk: ReviewDesk,
        port: int,
        *,
        token: str | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        scheduler: Callable[[], SchedulingProvider] | None = None,
        defaults: Callable[[], ApprovalDefaults | None] | None = None,
        stopped: Callable[[], bool] = lambda: False,
        demo_example: DemoExample | None = None,
    ) -> None:
        if desk.demo_mode and scheduler is not None:
            raise ValueError("a demo cannot have a scheduler")
        self.desk = desk
        self.demo_example = demo_example
        self.defaults = defaults  # pre-fills the approval form; None: empty fields
        self.token = token or secrets.token_urlsafe(32)
        self.confirmations: dict[str, Confirmation] = {}
        self.clock = clock
        self.scheduler = scheduler  # None: scheduling is off (the default)
        # The owner's STOP file, asked on every request: while it exists nothing is approved or
        # sent to Buffer, and nothing already in Buffer is cancelled.
        self.stopped = stopped
        super().__init__((HOST, port), _Handler)

    def server_bind(self) -> None:
        # HTTPServer.server_bind would look the host name up (getfqdn); nothing here needs it.
        super(HTTPServer, self).server_bind()
        self.server_name, self.server_port = HOST, self.server_address[1]

    @property
    def origin(self) -> str:
        return f"http://{HOST}:{self.server_port}"

    @property
    def start_url(self) -> str:
        return f"{self.origin}/?token={self.token}"


class _Handler(BaseHTTPRequestHandler):
    server: ReviewServer
    server_version = "pros-review"
    sys_version = ""

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 (stdlib signature)
        pass  # request lines carry the token on the first visit; nothing is logged

    # --- dispatch --------------------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 (stdlib name)
        self._guarded(self._get)

    def do_POST(self) -> None:  # noqa: N802 (stdlib name)
        self._guarded(self._post)

    def _guarded(self, method: Callable[[], None]) -> None:
        """An unexpected defect answers with a fixed page, never a traceback or a dropped
        connection. Nothing was recorded unless the ledger committed before the defect."""
        try:
            method()
        except Exception:
            self.close_connection = True
            try:
                self._render(self._message(500, "Internal error", "The request failed."))
            except OSError:
                pass

    def _get(self) -> None:
        if not self._host_ok():
            return
        url = urlsplit(self.path)
        if url.path == "/" and url.query:
            self._start_session(url.query)
            return
        if not self._session_ok():
            return
        parts = url.path.strip("/").split("/")
        if url.path == "/":
            self._render(self._queue())
        elif len(parts) == 2 and parts[0] == "story":
            panel = parse_qs(url.query, max_num_fields=4).get("panel", [""])[0]
            focus = panel if panel in {"edit", "approve", "reject", "schedule", "decision"} else ""
            self._render(self._story(parts[1], StoryView(focus=focus)))
        else:
            self._render(self._message(404, "Not found", "There is no such page."))

    def _post(self) -> None:
        if not self._host_ok() or not self._session_ok() or not self._same_origin():
            self._discard_body()
            return
        form = self._form()
        if form is None:
            return
        parts = urlsplit(self.path).path.strip("/").split("/")
        if len(parts) != 3 or parts[0] != "story":
            self._render(self._message(404, "Not found", "There is no such action."))
            return
        sid, action = parts[1], parts[2]
        handler = {
            "edit": self._edit,
            "sheet": self._sheet,
            "repost-sheet": self._repost_sheet,
            "approve": self._approve,
            "cancel": self._cancel,
            "reject": self._reject,
            "schedule": self._schedule,
        }.get(action)
        if handler is None:
            self._render(self._message(404, "Not found", "There is no such action."))
            return
        if action == "schedule" and self.server.desk.demo_mode:
            self._render(
                self._message(
                    403,
                    "Scheduling disabled",
                    "This is a synthetic demo. Scheduling is permanently disabled.",
                )
            )
            return
        if action in _HALTED_BY_STOP and self.server.stopped():
            self._render(self._message(423, "STOPPED", STOP_MESSAGE))
            return
        seq = form.get("seq", "")
        if not seq.isdigit() or len(seq) > 18:
            self._render(self._message(400, "Bad request", "The form is incomplete."))
            return
        handler(sid, form, int(seq))

    # --- pages and actions -----------------------------------------------------------------

    def _queue(self) -> Page:
        try:
            items, refused = self.server.desk.queue()
        except _Failure as exc:
            return self._message(500, "Review unavailable", _describe(exc)[0])
        return Page(
            200,
            queue_page(
                items,
                refused,
                stopped=self.server.stopped(),
                demo=self.server.desk.demo_mode,
                gates={i.story.story_id: self.server.desk.gate(i) for i in items},
            ),
        )

    def _story(self, sid: str, view: StoryView, status: int = 200) -> Page:
        try:
            item = self.server.desk.item(sid)
        except _Failure as exc:
            code = 404 if getattr(exc, "code", None) == ReviewErrorCode.STORY_NOT_FOUND else 409
            return self._message(code, "Draft unavailable", " ".join(_describe(exc)))
        stopped = self.server.stopped()
        scheduling = self.server.scheduler is not None and not stopped
        defaults = self.server.defaults() if self.server.defaults is not None else None
        page = story_page(
            item,
            self.server.token,
            view,
            None,
            stopped=stopped,
            scheduling=scheduling,
            defaults=defaults,
            fact_check=self.server.desk.fact_check(item),
            gate=self.server.desk.gate(item),
            x_state=self.server.desk.x_state(item),
            demo=self.server.desk.demo_mode,
            demo_example=self.server.demo_example,
            records=self.server.desk.records(item.current.brief),
        )
        return Page(status, page)

    def _edit(self, sid: str, form: dict[str, str], seq: int) -> None:
        desk = self.server.desk
        texts = {name[4:]: value for name, value in form.items() if name.startswith("seg-")}
        full = frozenset(name[5:] for name in form if name.startswith("full-"))
        if form.get("action") == "save":
            try:
                desk.save_edit(sid, texts, seq, self.server.clock(), full)
            except _Failure as exc:
                view = StoryView(texts=texts, full_range=full, error=_describe(exc), focus="edit")
                if getattr(exc, "code", None) == ReviewErrorCode.EVAL_CASE_NOT_SAVED:
                    view = StoryView(error=_describe(exc), focus="edit")
                self._render(self._story(sid, view, 409))
                return
            self._redirect(sid, "edit")
            return
        try:
            preview = desk.preview(desk.item(sid), texts, full)
        except _Failure as exc:
            view = StoryView(texts=texts, full_range=full, error=_describe(exc), focus="edit")
            self._render(self._story(sid, view, 409))
            return
        self._render(
            self._story(sid, StoryView(texts=texts, full_range=full, preview=preview, focus="edit"))
        )

    def _repost_sheet(self, sid: str, form: dict[str, str], seq: int) -> None:
        self._sheet(sid, form, seq, repost=True)

    def _sheet(self, sid: str, form: dict[str, str], seq: int, *, repost: bool = False) -> None:
        try:
            item = self.server.desk.item(sid)
            if item.seq != seq:
                raise PublicationError(PublicationErrorCode.REVIEW_STALE)
            sheet = self.server.desk.approval_candidate(
                sid,
                form.get("channel_id", ""),
                form.get("due_at", ""),
                self.server.clock(),
                repost=repost,
            )
            x_sheet = self.server.desk.x_sheet(sid, sheet)
            pending = Confirmation(secrets.token_urlsafe(32), item.subject(), seq, sheet, x_sheet)
            if repost:
                matches = self.server.desk.repost_history(item, sheet)
                if not matches:
                    raise PublicationError(PublicationErrorCode.REPOST_CONFIRMATION_REQUIRED)
                pending.repost_history = fingerprint(matches)
            self.server.confirmations[sid] = pending
        except _Failure as exc:
            self._render(
                self._story(
                    sid,
                    StoryView(
                        error=_describe(exc),
                        focus="approve",
                        channel_id=form.get("channel_id", ""),
                        due_at=form.get("due_at", ""),
                    ),
                    409,
                )
            )
            return
        self._render(
            self._story(
                sid,
                StoryView(
                    sheet=sheet,
                    x_sheet=x_sheet,
                    confirmation=pending.key,
                    focus="approve",
                    repost=repost,
                ),
            )
        )

    def _approve(self, sid: str, form: dict[str, str], seq: int) -> None:
        try:
            pending = self.server.confirmations.get(sid)
            if (
                pending is None
                or not _equal(form.get("confirmation", ""), pending.key)
                or seq != pending.seq
                or form.get("confirm") != "yes"
                or set(form)
                != (
                    {"seq", "confirmation", "confirm", "typed"}
                    if pending.repost_history
                    else {"seq", "confirmation", "confirm"}
                )
            ):
                raise PublicationError(PublicationErrorCode.REVIEW_STALE)
            item = self.server.desk.item(sid)
            if item.subject() != pending.subject:
                raise PublicationError(PublicationErrorCode.REVIEW_STALE)
            if pending.completed_seq is not None:
                if (
                    item.seq == pending.completed_seq
                    and item.approval is not None
                    and item.approval.approval_id == pending.candidate.approval_id
                ):
                    if pending.warning:
                        self._render(
                            self._story(sid, StoryView(error=pending.warning, focus="approve"))
                        )
                    else:
                        self._redirect(sid, "approve")
                    return
                raise PublicationError(PublicationErrorCode.REVIEW_STALE)
            candidate = self.server.desk.approval_candidate(
                sid,
                pending.candidate.channel_id,
                pending.candidate.due_at,
                self.server.clock(),
                repost=pending.repost_history is not None,
            )
            post = self.server.desk.x_sheet(sid, candidate)
            if item.seq != pending.seq or candidate != pending.candidate or post != pending.x_post:
                raise PublicationError(PublicationErrorCode.REVIEW_STALE)
            _, decision = self.server.desk.approve(
                sid,
                candidate.channel_id,
                candidate.due_at,
                confirmation_phrase(candidate.approval_id),
                seq,
                self.server.clock(),
                x_digest(post),
                repost_typed=form.get("typed") if pending.repost_history is not None else None,
                history_sha256=pending.repost_history,
            )
            pending.completed_seq = decision.seq
        except _Failure as exc:
            if getattr(exc, "code", None) == ReviewErrorCode.EVAL_CASE_NOT_SAVED:
                receipt = self.server.desk.item(sid)
                if (
                    pending is not None
                    and receipt.subject() == pending.subject
                    and receipt.approval is not None
                    and receipt.approval.approval_id == pending.candidate.approval_id
                ):
                    pending.completed_seq = receipt.seq
                    pending.warning = _describe(exc)
            self._render(self._story(sid, StoryView(error=_describe(exc), focus="approve"), 409))
            return
        self._redirect(sid, "approve")

    def _cancel(self, sid: str, form: dict[str, str], seq: int) -> None:
        pending = self.server.confirmations.get(sid)
        if pending is not None and _equal(form.get("confirmation", ""), pending.key):
            del self.server.confirmations[sid]
        self._redirect(sid, "approve")

    def _reject(self, sid: str, form: dict[str, str], seq: int) -> None:
        try:
            self.server.desk.reject(sid, form.get("typed", ""), seq, self.server.clock())
        except _Failure as exc:
            self._render(
                self._story(
                    sid,
                    StoryView(error=_describe(exc), focus="reject", typed=form.get("typed", "")),
                    409,
                )
            )
            return
        self._redirect(sid, "decision")

    def _schedule(self, sid: str, form: dict[str, str], seq: int) -> None:
        """One owner-confirmed submission. Whatever the provider answered is in the ledger; the
        redirected page shows it. Nothing here retries."""
        try:
            self.server.desk.schedule(
                sid,
                form.get("approval_id", ""),
                form.get("typed", ""),
                seq,
                self.server.scheduler,
                self.server.clock(),
            )
        except _Failure as exc:
            status = (
                403 if getattr(exc, "code", None) == ReviewErrorCode.SCHEDULING_DISABLED else 409
            )
            self._render(
                self._story(
                    sid,
                    StoryView(error=_describe(exc), focus="schedule", typed=form.get("typed", "")),
                    status,
                )
            )
            return
        self._redirect(sid, "schedule")

    def _message(self, status: int, title: str, text: str) -> Page:
        return message_page(status, title, text, demo=self.server.desk.demo_mode)

    # --- request checks --------------------------------------------------------------------

    def _host_ok(self) -> bool:
        if self.headers.get("Host") != f"{HOST}:{self.server.server_port}":
            self._render(self._message(400, "Bad request", "Open the page at the printed address."))
            return False
        return True

    def _session_ok(self) -> bool:
        if _equal(self._cookie(), self.server.token):
            return True
        self._render(
            self._message(
                403,
                "Not signed in",
                "Open the browser address printed by shipyap review or shipyap demo.",
            )
        )
        return False

    def _same_origin(self) -> bool:
        origin = self.headers.get("Origin")
        site = self.headers.get("Sec-Fetch-Site")
        # Under Referrer-Policy: no-referrer, Chrome sends `Origin: null` on its own same-origin
        # form POSTs; that is accepted only with the browser's own `Sec-Fetch-Site: same-origin`.
        null_same_origin = origin == "null" and site == "same-origin"
        if (
            origin is not None and origin != self.server.origin and not null_same_origin
        ) or site not in (None, "same-origin"):
            self._render(self._message(403, "Refused", "Cross-site requests are refused."))
            return False
        return True

    def _start_session(self, query: str) -> None:
        fields = parse_qs(query, max_num_fields=4)
        token = fields.get("token", [""])
        if len(token) != 1 or not _equal(token[0], self.server.token):
            self._render(self._message(403, "Not signed in", "This address is not valid."))
            return
        # A notification link names one draft. Only a well-formed story id is followed; anything
        # else lands on the queue, so the redirect can never leave this page.
        story = fields.get("story", [""])
        valid = len(story) == 1 and re.fullmatch(r"[0-9a-f]{64}", story[0])
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header(
            "Set-Cookie", f"{COOKIE}={self.server.token}; Path=/; HttpOnly; SameSite=Strict"
        )
        self.send_header("Location", f"/story/{story[0]}" if valid else "/")
        self._finish_headers(0)

    def _cookie(self) -> str:
        for part in (self.headers.get("Cookie") or "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == COOKIE:
                return value
        return ""

    def _form(self) -> dict[str, str] | None:
        kind = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        length = self.headers.get("Content-Length") or ""
        if kind != "application/x-www-form-urlencoded" or not length.isdigit():
            self._render(self._message(400, "Bad request", "The form could not be read."))
            self._discard_body()
            return None
        if int(length) > MAX_BODY_BYTES:
            self.close_connection = True
            self._render(self._message(413, "Too large", "The form is too large."))
            return None
        try:
            body = self.rfile.read(int(length)).decode("utf-8")
            parsed = parse_qs(body, keep_blank_values=True, max_num_fields=MAX_FIELDS)
        except (UnicodeDecodeError, ValueError):
            self._render(self._message(400, "Bad request", "The form could not be read."))
            return None
        if any(len(values) != 1 for values in parsed.values()):
            self._render(self._message(400, "Bad request", "A form field was repeated."))
            return None
        form = {name: values[0] for name, values in parsed.items()}
        if not _equal(form.pop("token", ""), self.server.token):
            self._render(self._message(403, "Refused", "The form is not from this session."))
            return None
        return form

    def _discard_body(self) -> None:
        """Read a refused request's body (within the limit) and close. Closing a socket with
        unread data resets the connection, and the client could lose the refusal page."""
        self.close_connection = True
        length = self.headers.get("Content-Length") or ""
        if length.isdigit() and int(length) <= MAX_BODY_BYTES:
            try:
                self.rfile.read(int(length))
            except OSError:
                pass

    # --- responses -------------------------------------------------------------------------

    def _render(self, page: Page) -> None:
        data = page.html.encode("utf-8")
        self.send_response(page.status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self._finish_headers(len(data))
        self.wfile.write(data)

    def _redirect(self, sid: str, section: str = "decision") -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", f"/story/{sid}?panel={section}#{section}")
        self._finish_headers(0)

    def _finish_headers(self, length: int) -> None:
        for name, value in _HEADERS:
            self.send_header(name, value)
        self.send_header("Content-Length", str(length))
        self.end_headers()


def _describe(exc: Exception) -> tuple[str, ...]:
    """A fixed code and message, plus the editorial schema details (never input values)."""
    code = getattr(exc, "code", "ERROR")
    lines = [f"{code}: {exc}" if isinstance(exc, PublicationError) else str(exc)]
    lines.extend(getattr(exc, "details", ()))
    return tuple(lines)


def _equal(given: str, expected: str) -> bool:
    return hmac.compare_digest(given.encode("utf-8"), expected.encode("utf-8"))
