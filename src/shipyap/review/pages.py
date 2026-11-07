"""Server-rendered HTML for `pros review`. Plain forms, no script, every value escaped.

The pages say what each state means and no more: UNAPPROVED until an approval is recorded,
semantic support UNKNOWN always, and approval as the owner's decision to post, not a
certification that any claim is true. An approved time is a target until Buffer confirms it: a
send intent is described by its ledger state only (ACCEPTED is not "scheduled", SCHEDULED is not
"published").
"""

from dataclasses import dataclass
from html import escape
from pathlib import Path

from shipyap.editorial.models import (
    Brief,
    CheckedSourceRef,
    DraftStatus,
    Finding,
    FindingCode,
    Severity,
    SourceRef,
)
from shipyap.publication.approval import (
    Candidate,
)
from shipyap.publication.history import repost_phrase
from shipyap.publication.ledger import (
    LEAD_ACCEPTED,
    Intent,
    IntentState,
    ReviewKind,
)
from shipyap.publication.x import X_MAX_WEIGHT, x_weight
from shipyap.review.gate import Gate, Note
from shipyap.review.service import (
    REFUSALS,
    STALE_MESSAGE,
    Preview,
    ReviewItem,
    ReviewState,
    XState,
    reject_phrase,
    schedule_phrase,
    scope_key,
)
from shipyap.review.ux import attention_count, effective_gate, flagged
from shipyap.review.ux import status as plain_status
from shipyap.review.x_post import XPost

CERTIFICATION_NOTICE = (
    "Approval here records your decision to post this exact text to this destination at this "
    "time. It is not semantic certification: no program has checked that the post interprets its "
    "sources correctly, and semantic support stays UNKNOWN. Citation checks only located the "
    "cited lines in the pinned snapshot."
)
LOCAL_NOTICE = (
    "Local review. Viewing, editing, approving and rejecting send nothing. A post goes to Buffer "
    'only when you confirm "Schedule approved post" on an approved draft, and only if '
    "scheduling is enabled in your settings when shipyap review starts."
)
STOP_MESSAGE = (
    "STOPPED: a STOP file is in your workspace. Nothing can be approved or sent to Buffer until "
    "you remove it. Posts already scheduled in Buffer are not cancelled; pause them in Buffer."
)
# What each persisted send intent lets the page say (the ledger's meanings; see ledger.py).
INTENT_GUIDANCE: dict[IntentState, str] = {
    IntentState.SENDING: (
        "Outcome uncertain: the request may have been sent, or the process stopped while sending. "
        "Do not submit again; this approval is never resent. Check the current status in Buffer."
    ),
    IntentState.ACCEPTED: (
        "Buffer accepted the request. Scheduling is not yet independently confirmed; "
        "Check the current status in Buffer. It is not published."
    ),
    IntentState.AMBIGUOUS: (
        "Outcome uncertain: the post may or may not exist in Buffer. Do not submit again; this "
        "approval is never resent. Check the current status in Buffer."
    ),
    IntentState.REJECTED: (
        "Buffer refused the request; nothing was created. This approval is never submitted again."
    ),
    IntentState.SCHEDULED: (
        "Buffer reports this exact text, channel and time as scheduled. It is not published yet."
    ),
    IntentState.NOT_SCHEDULED: (
        "The post exists in Buffer but will not publish as it stands (draft or awaiting approval "
        "in Buffer)."
    ),
    IntentState.SENT: "Buffer reports the post as sent (published).",
    IntentState.PROVIDER_ERROR: (
        "Buffer reports an error for the post. Nothing is resent automatically."
    ),
    IntentState.MISMATCH: (
        "Buffer's copy differs from what you approved (text, channel or time). Check it in Buffer."
    ),
}
# What some findings mean for the owner deciding how to fix them; the finding itself is unchanged.
FINDING_NOTES: dict[FindingCode, str] = {
    FindingCode.NUMBER_NOT_IN_EVIDENCE: (
        "Numbers are looked for only in the evidence the reference selects. A reference with a "
        "quote selects just the quoted text, even when other numbers are visible in the cited "
        "lines; a reference without a quote selects the whole cited line range. If the numbers "
        "are in the cited lines, choose 'Use full cited range' for that reference in Edit. If "
        "they are not, fix the wording."
    ),
    FindingCode.NO_INSIGHT_REFERENCE: (
        "No segment cites the brief's insight item (for a story draft: the backlog angle, "
        "labelled as such). This is a warning, not a block. Do not add contributor details that "
        "were never recorded to satisfy it."
    ),
    FindingCode.NO_LIMITATION_REFERENCE: (
        "No segment cites one of the brief's limitation items (for a story draft: the recorded "
        "provenance and scope notes listed under Limitations). This is a warning, not a block; "
        "check that the post states its scope honestly. Do not invent limitations or contributor "
        "details to satisfy it."
    ),
}
NOT_CANCELLED = (
    "A Buffer request was already made for this approval. A local edit, rejection or new "
    "approval withdraws it here only: it does NOT cancel or change the post in Buffer. Change or "
    "delete it in Buffer yourself."
)

_CSS = """
:root { --fg:#1d1d1f; --muted:#5f6368; --bg:#ffffff; --panel:#f5f6f8; --line:#d9dce1;
  --bad:#b3261e; --warn:#8a5a00; --ok:#1e6b35; --accent:#2b4fc7; }
* { box-sizing: border-box; }
body { margin:0; font:15px/1.5 system-ui, sans-serif; color:var(--fg); background:var(--bg); }
main { max-width: 980px; margin: 0 auto; padding: 16px; }
h1 { font-size: 22px; margin: 8px 0 4px; } h2 { font-size: 17px; margin: 24px 0 8px; }
.notice { background:#fff7e0; border:1px solid #e6c870; padding:8px 12px; border-radius:6px; }
.error { background:#fdecea; border:1px solid var(--bad); padding:8px 12px; border-radius:6px; }
.done { background:#e8f5ec; border:1px solid var(--ok); padding:8px 12px; border-radius:6px; }
.panel { background:var(--panel); border:1px solid var(--line); border-radius:6px; padding:12px; }
details { margin:12px 0; } summary { cursor:pointer; font-weight:600; }
.status { font-weight:600; } .flagged { margin:12px 0; } :target { scroll-margin-top:16px; }
blockquote { margin:0 0 8px; }
pre { white-space: pre-wrap; word-break: break-word; margin:0; font: 14px/1.5 ui-monospace,
  monospace; }
.post { background:#fff; border:1px solid var(--line); padding:12px; border-radius:6px; }
.post pre { font:17px/1.65 system-ui, sans-serif; max-width:65ch; margin:0 auto;
  word-break:normal; overflow-wrap:anywhere; }
.claim-evidence { display:grid; grid-template-columns:1fr 1fr; gap:20px; }
@media (max-width:700px) { .claim-evidence { grid-template-columns:1fr; } }
.badge { display:inline-block; padding:1px 8px; border-radius:10px; font-size:13px;
  border:1px solid var(--line); background:#fff; }
.UNAPPROVED { color:var(--warn); } .APPROVED { color:var(--ok); } .REJECTED { color:var(--bad); }
.BLOCKING { color:var(--bad); font-weight:600; } .WARNING { color:var(--warn); }
table { border-collapse: collapse; width: 100%; } td, th { text-align:left; vertical-align:top;
  padding:4px 6px; border-bottom:1px solid var(--line); font-size:14px; }
textarea, input[type=text] { width:100%; font: 14px/1.4 ui-monospace, monospace; padding:6px; }
textarea { min-height: 3.2em; }
button { font-size:14px; padding:6px 14px; margin: 6px 6px 0 0; cursor:pointer; }
.muted { color: var(--muted); font-size: 13px; }
a { color: var(--accent); }
"""


@dataclass(frozen=True)
class Page:
    status: int
    html: str


def e(value: object) -> str:
    return escape(str(value), quote=True)


DEMO_GUIDE = (
    "SYNTHETIC SAMPLE DATA. Read the original draft and its Must fix card. "
    "Choose Fix it and recheck to try the supplied correction, then review it for approval."
)

PRODUCT_NAME = "Shipyap"
DEMO_NOTICE = "SYNTHETIC SAMPLE DATA. Offline demo: scheduling is disabled. All edits and review decisions are temporary. No model fact-check was run."


def _demo_intro(demo: bool) -> str:
    if not demo:
        return ""
    return (
        f"<p class=notice>{e(DEMO_GUIDE)}</p>"
        "<p class=muted>SYNTHETIC SAMPLE DATA. Scheduling is disabled.</p>"
    )


@dataclass(frozen=True)
class DemoExample:
    original: str
    corrected: str
    evidence: str
    block: str
    corrected_blocks: int


def layout(title: str, body: str, *, demo: bool = False) -> str:
    brand = f"{PRODUCT_NAME} demo" if demo else PRODUCT_NAME
    return (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width, initial-scale=1">'
        f"<title>{e(title)} · {e(brand)}</title><style>{_CSS}</style></head><body><main>"
        f"{body}<footer><p class=muted>{e(DEMO_NOTICE if demo else LOCAL_NOTICE)}</p></footer></main></body></html>"
    )


def message_page(status: int, title: str, text: str, *, demo: bool = False) -> Page:
    return Page(status, layout(title, f"<h1>{e(title)}</h1><p>{e(text)}</p>", demo=demo))


def queue_page(items, refused, *, stopped=False, demo=False, gates=None) -> str:
    rows = []
    for item in sorted(
        items,
        key=lambda i: (i.story.offered_at or i.story.first_seen_at, i.story.story_id),
        reverse=True,
    ):
        state = plain_status(item, (gates or {}).get(item.story.story_id))
        rows.append(
            f'<tr><td><a href="/story/{e(item.story.story_id)}">{e(item.story.latest.angle)}</a>'
            f"<br><span class=muted>{e(item.story.repo_id)}</span></td><td>{e(state)}"
            + (
                "<p>Earlier Buffer submissions may still exist; check Buffer.</p>"
                if item.withdrawn_intents
                else ""
            )
            + "</td>"
            f"<td><details><summary>Details</summary><p>Story id: {e(item.story.story_id)}</p>"
            f"<p>Review: {e(item.state.value)}; {e(item.current.amap.draft_status.value)}</p>"
            f"<p>Buffer: {e(_buffer_summary(item))}{'(edited)' if item.edited else ''}</p>"
            f"<p>First offered: {e(item.story.offered_at or item.story.first_seen_at)}</p></details></td></tr>"
        )
    body = [
        _stop_banner(stopped),
        _demo_intro(demo),
        "<h1>Drafts</h1>",
    ]
    if rows:
        body.append(
            "<table><tr><th>Draft</th><th>Status</th><th></th></tr>" + "".join(rows) + "</table>"
        )
    else:
        body.append(
            "<p>No drafts to review yet. Drafts appear here after shipyap run prepares them.</p>"
        )
    if refused:
        body.append(
            "<details><summary>Details: unavailable drafts</summary>"
            + "".join(
                f"<p>{e(entry.story_id)}: {e(entry.reason.value)} - {e(REFUSALS[entry.reason])}</p>"
                for entry in refused
            )
            + "</details>"
        )
    return layout("Drafts", "".join(body), demo=demo)


@dataclass(frozen=True)
class StoryView:
    """What a story page shows besides the item: unsaved edits, a sheet, a result line."""

    texts: dict[str, str] | None = None  # the editor's content, when it is not the saved text
    full_range: frozenset[str] = frozenset()  # unsaved "use full cited range" choices
    preview: Preview | None = None
    sheet: Candidate | None = None
    x_sheet: XPost | None = None  # what X gets with the sheet's approval
    error: tuple[str, ...] = ()
    done: str | None = None
    confirmation: str | None = None
    focus: str = ""
    channel_id: str | None = None
    due_at: str | None = None
    typed: str = ""
    repost: bool = False


def story_page(
    item: ReviewItem,
    token: str,
    view: StoryView,
    publish_hint: str | None,
    *,
    stopped: bool = False,
    scheduling: bool = False,
    defaults: "ApprovalDefaults | None" = None,
    fact_check: dict | None = None,
    gate: Gate | None = None,
    x_state: XState | None = None,
    demo: bool = False,
    demo_example: DemoExample | None = None,
    records=(),
) -> str:
    current = item.current
    gate = effective_gate(item, gate, fact_check)
    state = plain_status(item, gate)
    count = attention_count(gate)
    unsaved = view.texts is not None and (view.preview is None or view.preview.changed)
    if state == "Not fact-checked - cannot approve":
        pass
    elif stopped and not item.publications:
        state = "Stopped - approval and scheduling are paused"
    elif unsaved and not item.publications:
        state = "Unsaved edits - save or discard them before approving"
    elif item.state == ReviewState.UNAPPROVED and count and not item.publications:
        state = f"{count} {'thing needs' if count == 1 else 'things need'} your attention"
    parts = [
        _demo_intro(demo),
        _stop_banner(stopped),
        '<p><a href="/">&larr; All drafts</a></p>',
        "<h1>Post</h1>",
        f"<div class=post><pre>{e(current.text or 'No post text was prepared.')}</pre></div>",
        f"<p class=status id=decision tabindex=-1>{e(state)}</p>",
    ]
    if view.error:
        reason = {
            "approve": "Approval could not be recorded. Review the saved post and try again.",
            "reject": "Rejection was not recorded. Check the phrase and try again.",
            "edit": "The edit could not be completed. Your input is kept below.",
            "schedule": "Scheduling could not be completed. Check the confirmation and outcome below.",
        }.get(view.focus, "This action could not be completed. See Details.")
        recorded = any("EVAL_CASE_NOT_SAVED" in line for line in view.error)
        if recorded:
            reason = "Your change was recorded, but its replay example could not be saved. Do not submit it again."
        parts.append(f"<div class={'notice' if recorded else 'error'} role=alert>{reason}</div>")
    if view.done:
        parts.append(f"<p class=done>{e(view.done)}</p>")
    parts.append(flagged(item, gate, records))
    if item.duplicate_posts:
        parts.append(
            "<div class=notice><p>Earlier publication history locks ordinary approval and scheduling. A repeat needs typed Repost.</p>"
            + "".join(
                f"<p>{e(p.state)} {e(p.due_at)}; post {e(p.post_id or 'outcome uncertain')}; {e(p.rule)} {p.score:.0%}</p><details><summary>Earlier text</summary><pre>{e(p.text)}</pre></details>"
                for p in item.duplicate_posts
            )
            + "</div>"
        )

    if item.withdrawn_intents:
        parts.append(f"<p class=notice>{e(NOT_CANCELLED)}</p>")
    if item.state == ReviewState.REJECTED:
        parts.append(
            "<p>This review was rejected; another publication requires typed Repost.</p>"
            if item.publications
            else "<p>A rejection is final for this story.</p>"
        )
    if item.state != ReviewState.REJECTED or item.publications:
        parts.append("<section id=approve>")
        if not stopped:
            parts.append(_approve_section(item, token, view, defaults, gate))
        parts.append("</section>")
        if demo and demo_example is not None and not stopped and not unsaved:
            parts.append(_demo_fix(item, token, demo_example, gate))
        if item.state != ReviewState.REJECTED:
            parts.append(_reject_section(item, token, view))
    if (
        item.state == ReviewState.APPROVED
        and not demo
        and (not item.publications or item.repost_ready or item.intent is not None)
    ):
        section = _schedule_section(item, token, scheduling and not stopped, None, x_state)
        if view.focus == "schedule":
            section = section.replace(
                "<input type=text name=typed",
                f"<input autofocus value={_q(view.typed)} type=text name=typed",
                1,
            )
        parts.append("<section id=schedule>" + section + "</section>")
    opened = " open" if view.focus == "edit" or view.texts is not None else ""
    parts.append(f"<details id=details{opened}><summary>Details</summary>")
    if item.state != ReviewState.REJECTED and not item.stale:
        editor = _edit_section(item, token, view)
        if view.focus == "edit":
            editor = editor.replace("<textarea ", "<textarea autofocus ", 1)
        parts.append("<section id=edit>" + editor + "</section>")
    else:
        reason = (
            "This draft was rejected; rejection is final."
            if item.state == ReviewState.REJECTED
            else STALE_MESSAGE
        )
        parts.append(f"<section id=edit><h2>Edit and revalidate</h2><p>{e(reason)}</p></section>")
    if view.error:
        parts.append("<h2>Action details</h2>" + "<br>".join(e(line) for line in view.error))
    parts.append(f"<p class=muted>{e(CERTIFICATION_NOTICE)}</p>")
    parts.append(
        f"<p>{len(current.text)} characters. Review: {e(item.state.value)}. "
        f"Checks: {e(current.amap.draft_status.value)}. Semantic support: <span class=badge>UNKNOWN</span></p>"
    )
    if item.edited:
        parts.append("<p>edited by owner</p>")
    if item.stale:
        parts.append(f"<p>{e(STALE_MESSAGE)}</p>")
    parts.extend(
        [
            _story_section(item),
            _evidence_section(item),
            _validation_section(item),
            _limitations_section(item),
        ]
    )
    parts.extend(
        [
            _fact_check_section(fact_check, demo=demo),
            _gate_section(item, gate),
            _x_approval_section(x_state),
            _withdrawn_section(item),
            _history_section(item),
        ]
    )
    if item.approval is not None:
        a = item.approval
        parts.append(
            f"<h2>Recorded approval</h2><p>approval id: {e(a.approval_id)}</p>"
            f"<p>Channel: {e(a.channel_id)}; approved target time: {e(a.due_at)} (UTC)</p>"
            f"<p>Text sha256: {e(a.text_sha256)}; bundle record: {e(a.bundle_record_sha256)}</p>"
        )
    parts.append("</details>")
    return layout("Review post", "".join(parts), demo=demo)


# --- sections ----------------------------------------------------------------------------------


def _demo_fix(item: ReviewItem, token: str, example: DemoExample, gate: Gate) -> str:
    if item.state != ReviewState.UNAPPROVED:
        return ""
    first = item.current.response.segments[0]
    if first.text == example.corrected and gate.approvable:
        return (
            "<section id=fix><h2>Recheck passed</h2><p class=done>"
            "0 Must fix items. The corrected 3/10 sentence passes the citation checks. "
            "Approve is now available.</p><p>SYNTHETIC SAMPLE DATA. "
            "The edited sentence has no new model fact check. Read its cited evidence.</p></section>"
        )
    if first.text != example.original:
        return ""
    fields = "".join(
        f'<input type=hidden name="seg-{e(segment.id)}" value={_q(example.corrected if segment.id == first.id else segment.text)}>'
        for segment in item.current.response.segments
    )
    return (
        "<section id=fix><h2>Fix it</h2><p>SYNTHETIC SAMPLE DATA. "
        "The cited line says 3/10. Apply this scripted correction and rerun the same checks.</p>"
        f"<blockquote>{e(example.corrected)}</blockquote>"
        f"<form method=post action=/story/{e(item.story.story_id)}/edit#fix>"
        f"{_hidden(token, item.seq)}{fields}"
        "<button name=action value=save>Fix it and recheck</button></form></section>"
    )


def _stop_banner(stopped: bool) -> str:
    return f"<div class=error><b>{e(STOP_MESSAGE)}</b></div>" if stopped else ""


def _story_section(item: ReviewItem) -> str:
    link = item.link
    return (
        "<h2>Story</h2><div class=panel><table>"
        f"<tr><td>repository</td><td>{e(item.story.repo_id)} &middot; "
        f"{e(link.get('source_url', ''))}</td></tr>"
        f"<tr><td>revision</td><td>{e(link.get('revision_sha', ''))}</td></tr>"
        f"<tr><td>angle</td><td>{e(link.get('angle', ''))} "
        "<span class=muted>(unverified backlog text; who proposed it is not recorded)</span></td></tr>"
        f"<tr><td>support</td><td>{e(link.get('support', ''))} "
        "<span class=muted>(unverified backlog text)</span></td></tr>"
        f"<tr><td>story id</td><td>{e(item.story.story_id)}</td></tr>"
        f"<tr><td>06D draft</td><td>{e(item.origin.directory.name)} "
        f"(record sha256 {e(item.origin.record_sha256)}; generator "
        f"{e(item.origin.record.generator.label if item.origin.record.generator else '')}, "
        "self-reported)</td></tr>"
        f"<tr><td>story-link.json sha256</td><td>{e(item.link_sha256)}</td></tr>"
        "</table></div>"
    )


def _evidence_section(item: ReviewItem) -> str:
    brief = item.current.brief
    rows = []
    for ref in item.link.get("refs", []):
        if not isinstance(ref, dict):
            continue
        lines = _excerpt(brief, ref.get("path"), ref.get("line_start"), ref.get("line_end"))
        rows.append(
            f"<tr><td>{e(ref.get('path'))}:{e(ref.get('line_start'))}-{e(ref.get('line_end'))}"
            f"<br><span class=muted>file sha256 {e(ref.get('file_sha256'))}</span></td>"
            f"<td>quote: {e(ref.get('quote') or '(none: the cited range)')}"
            f"<pre>{e(lines)}</pre></td></tr>"
        )
    return (
        "<h2>Source citations of the story</h2>"
        "<p class=muted>Located in the pinned snapshot by a deterministic check. Locating a line "
        "does not show that the post interprets it correctly.</p>"
        "<table><tr><th>Cited lines</th><th>Quote and snapshot text</th></tr>"
        + "".join(rows)
        + "</table>"
    )


def _validation_section(item: ReviewItem) -> str:
    amap = item.current.amap
    counts = amap.counts
    parts = [
        "<h2>Editorial checks (re-run on load)</h2>",
        f"<p>Status <b>{e(amap.draft_status.value)}</b>: {counts.blocking} blocking, "
        f"{counts.warnings} warnings; {counts.segments} segments ({counts.source_referenced} "
        f"source-referenced, {counts.author_stated} author-stated, {counts.interpretation} "
        f"interpretation, {counts.framing} framing, {counts.unsupported} unsupported).</p>",
    ]
    parts.append(_findings(amap.document_findings))
    parts.append("<table><tr><th>Segment</th><th>Classification and citations</th></tr>")
    for record in amap.segments:
        refs = "".join(_checked_ref(ref) for ref in record.source_refs)
        authors = "".join(
            f"<div>{e(ref.id)}: {e(ref.text or '(no such item)')}</div>"
            for ref in record.author_refs
        )
        parts.append(
            f"<tr><td>{e(record.id)}<br><span class=muted>{e(record.declared_basis.value)}"
            f"</span></td><td><b>{e(record.classification.value)}</b>: {e(record.text)}"
            f"<div class=muted>Review: {e(record.review_question)}</div>"
            f"{_findings(record.findings)}{refs}{authors}</td></tr>"
        )
    parts.append("</table>")
    return "".join(parts)


def _limitations_section(item: ReviewItem) -> str:
    brief = item.current.brief
    amap = item.current.amap
    items = [
        *(f"Limitation: {text}" for text in brief.contribution.limitations),
        *(f"Do not claim: {text}" for text in brief.contribution.do_not_claim),
        *(f"Needs verification: {q.text} ({q.reason})" for q in amap.verification_needed),
        *amap.notices,
    ]
    return "<h2>Limitations</h2><ul>" + "".join(f"<li>{e(text)}</li>" for text in items) + "</ul>"


def _edit_section(item: ReviewItem, token: str, view: StoryView) -> str:
    segments = item.current.response.segments
    if not segments:
        return "<h2>Edit and revalidate</h2><p>A NO_POST draft has no segments to edit.</p>"
    texts = view.texts or {segment.id: segment.text for segment in segments}
    fields = []
    for segment in segments:
        cited = [f"{ref.path}:{ref.line_start}-{ref.line_end}" for ref in segment.source_refs]
        cited += list(segment.author_refs)
        scopes = "".join(
            _scope_choice(segment.id, index, ref, view.full_range)
            for index, ref in enumerate(segment.source_refs, start=1)
        )
        fields.append(
            f"<p><label for=seg-{e(segment.id)}><b>{e(segment.id)}</b> "
            f"<span class=muted>{e(segment.basis.value)}"
            f"{' &middot; new paragraph' if segment.paragraph_break_before else ''}"
            f"{' &middot; cites ' + e(', '.join(cited)) if cited else ''}</span></label>"
            f"<textarea id=seg-{e(segment.id)} name=seg-{e(segment.id)}>"
            f"{e(texts.get(segment.id, ''))}</textarea>{scopes}</p>"
        )
    preview = ""
    if view.preview is not None:
        amap = view.preview.amap
        state = "unchanged" if not view.preview.changed else "UNSAVED edit"
        preview = (
            f"<div class=panel><p>Revalidated ({e(state)}): <b>{e(amap.draft_status.value)}</b>, "
            f"{amap.counts.blocking} blocking, {amap.counts.warnings} warnings. "
            + (
                "It could be approved after saving."
                if amap.draft_status == DraftStatus.READY_FOR_REVIEW
                else "It cannot be approved while blocking findings remain."
            )
            + "</p>"
            + _findings(amap.document_findings)
            + "".join(
                f"<div>{e(record.id)}: {e(record.classification.value)}"
                f"{_findings(record.findings)}</div>"
                for record in amap.segments
            )
            + (
                f"<p>Approval gate after saving: <b>{_gate_state(view.preview.gate)}</b></p>"
                + "".join(
                    f"<div>{e(s.segment_id)}: {e(note.line())}</div>"
                    for s in view.preview.gate.segments
                    for note in s.blocks
                )
                if view.preview.gate is not None
                else ""
            )
            + f"<p>Edited post:</p><div class=post><pre>{e(view.preview.text)}</pre></div></div>"
        )
    return (
        "<h2>Edit and revalidate</h2>"
        "<p class=muted>Edit the text of each segment. Its basis and citations stay as they are, "
        "because the editorial checker validates each segment against them; adding, removing, "
        "splitting or re-citing segments is not supported here. The one citation change offered "
        "is 'Use full cited range' for a quoted reference: its quote is dropped, so the checks "
        "read the whole cited line range; its file and lines stay the same. Each segment is one "
        "line. Revalidate checks without saving; Save edit writes a new draft bundle (also for a "
        "citation-only change), records it as UNAPPROVED and withdraws any earlier approval of "
        "this story.</p>"
        f"<form method=post action=/story/{e(item.story.story_id)}/edit#edit>"
        f"{_hidden(token, item.seq)}{''.join(fields)}"
        "<button name=action value=check>Revalidate</button>"
        "<button name=action value=save>Save edit</button></form>"
        f"{preview}"
    )


_FLAGGED = ("OUTDATED", "CONTRADICTED", "OVERSTATED")


def _fact_check_section(check: dict | None, *, demo: bool = False) -> str:
    """The second model's verdicts on the draft as generated (edits after it are not re-checked)."""
    if not check:
        return (
            "<h2>Fact check</h2><p class=muted>No fact check for this draft. Read every claim "
            "against the repository yourself before approving.</p>"
        )
    verdicts = check.get("verdicts") or []
    flagged = [v for v in verdicts if v.get("verdict") in _FLAGGED]
    rows = "".join(
        f"<tr><td>{e(str(v.get('segment_id')))}</td><td><b>{e(str(v.get('verdict')))}</b></td>"
        f"<td>{e(str(v.get('reason')))}</td><td>{e(', '.join(v.get('refs') or []))}</td></tr>"
        for v in verdicts
        if v.get("verdict") != "SUPPORTED"
    )
    head = (
        f"<div class=error>{len(flagged)} sentence(s) flagged as outdated, contradicted or "
        "overstated by the files. Fix or reject before approving.</div>"
        if flagged
        else "<p class=done>No sentence flagged as outdated, contradicted or overstated.</p>"
    )
    table = (
        f"<table><tr><th>Segment</th><th>Verdict</th><th>Why</th><th>Lines</th></tr>{rows}</table>"
        if rows
        else ""
    )
    if demo:
        return (
            "<h2>SAMPLE fact check (scripted)</h2><p class=muted>These example verdicts were "
            "written in advance. No model checked this sample.</p>" + head + table
        )
    origin = check.get("inherited_from")
    if isinstance(origin, str):
        basis = check.get("basis") if isinstance(check.get("basis"), dict) else {}
        listed = "".join(
            f"<li>{e(str(sid))}: {e(str(how))}</li>" for sid, how in sorted(basis.items())
        )
        return (
            f"<h2>Fact check</h2><p class=done id=fact-check-basis>Fact-checked, inherited from "
            f"{e(origin)}</p><p class=muted>Auto-repair changed this draft after its fact check. "
            "Every change was checked to be a free repair or a rewrite with its own re-check, so "
            "the original verdicts still apply. It is a judgement, not proof.</p>"
            f"<ul>{listed}</ul>" + head + table
        )
    return (
        "<h2>Fact check</h2><p class=muted>A second model read every file of the snapshot, not "
        "only the cited lines. It is a judgement, not proof; it checked the draft as generated, "
        "not later edits.</p>" + head + table
    )


def _gate_state(gate: Gate) -> str:
    return "open" if gate.approvable else f"BLOCKED ({len(gate.reasons())} reason(s))"


def _gate_section(item: ReviewItem, gate: Gate | None) -> str:
    """Per sentence: what blocks Approve, and the warnings, each with the lines or records that
    matter. First person is shown beside the author's own statement."""
    if gate is None:
        return ""
    statement = "".join(
        f"<div class=muted>Author statement: {e(text)}</div>"
        for text in item.current.brief.contribution.attempted
    )
    texts = {segment.id: segment.text for segment in item.current.response.segments}
    rows = []
    for segment in gate.segments:
        if not segment.blocks and not segment.warnings:
            continue
        cells = "".join(
            f"<div class=error>BLOCKS {e(note.code)}: {e(note.detail)}{_note_refs(note)}</div>"
            for note in segment.blocks
        ) + "".join(
            f"<div>WARNING {e(note.code)}: {e(note.detail)}{_note_refs(note)}</div>"
            + (statement if note.code == "FIRST_PERSON_FROM_STATEMENT" else "")
            for note in segment.warnings
        )
        rows.append(
            f"<tr><td>{e(segment.segment_id)}</td><td>{e(texts.get(segment.segment_id, ''))}"
            f"</td><td>{cells}</td></tr>"
        )
    rows += [
        f"<tr><td>post</td><td></td><td><div class=error>BLOCKS {e(n.code)}: {e(n.detail)}"
        "</div></td></tr>"
        for n in gate.document_blocks
    ]
    head = (
        "<p class=done>Nothing blocks approval. Warnings below still need your reading.</p>"
        if gate.approvable
        else f"<div class=error>Approval is blocked: {len(gate.reasons())} reason(s). Edit the "
        "sentence or reject the draft.</div>"
    )
    table = (
        "<table><tr><th>Segment</th><th>Sentence</th><th>Blocks and warnings</th></tr>"
        + "".join(rows)
        + "</table>"
        if rows
        else ""
    )
    return (
        "<h2>Approval gate</h2><p class=muted>Approve is refused while a sentence is contradicted "
        "or outdated by the files, or fails a mechanical check (numbers, labels, citations). "
        "Overstated or unverifiable sentences and first person are warnings. Mechanical checks "
        "re-run on every edit; the fact check does not.</p>" + head + table
    )


def _note_refs(note: Note) -> str:
    return f" <span class=muted>[{e(', '.join(note.refs))}]</span>" if note.refs else ""


@dataclass(frozen=True)
class ApprovalDefaults:
    """Pre-filled values for the approval form (from `pros review`'s config). The owner can
    change both; the approval sheet still shows exactly what will be bound."""

    channel_id: str
    due_at: str  # UTC, the next free posting slot
    local: str  # the same moment on the owner's clock, for reading


def _approve_section(item, token, view, defaults=None, gate=None) -> str:
    sid = e(item.story.story_id)
    if view.texts is not None and (view.preview is None or view.preview.changed):
        return f'<p>Save or discard the edit first. <a href="/story/{sid}#approve">Discard the edit</a></p>'
    repeat = bool(item.publications or item.duplicate_posts or view.repost)
    label = "Repost" if repeat else "Approve"
    if item.repost_ready and item.intent is None:
        return "<p>A typed repost approval is recorded for this exact post. Send it below.</p>"
    if item.stale or (gate is not None and not gate.approvable):
        return f"<button disabled>{label}</button><p>Fix the Must fix items before {label.lower()}.</p>"

    if view.sheet is not None and view.confirmation is not None:
        sheet = view.sheet
        x_post = view.x_sheet or XPost(None, "not shown")
        return (
            f"<h2>{'Repost' if view.repost else 'Approve'} this exact post?</h2><h3>LinkedIn</h3>"
            f"<div class=post><pre>{e(sheet.text)}</pre></div>"
            f"<p>LinkedIn target time {e(sheet.due_at)} (UTC). Approving sends nothing; Buffer has not scheduled it.</p>"
            f"<details><summary>Destination details</summary><p>Channel: {e(sheet.channel_id)}</p></details>"
            f"<h3>X (same slot, after LinkedIn)</h3>{_x_text(x_post)}"
            f"<form method=post action=/story/{sid}/approve#approve>{_hidden(token, item.seq)}"
            f"<input type=hidden name=confirmation value={_q(view.confirmation)}>"
            + (
                f"<p>Type exactly: <b>{e(repost_phrase(item.story.story_id, sheet))}</b></p>"
                '<input type=text name=typed autocomplete=off aria-label="repost phrase">'
                "<button autofocus name=confirm value=yes>Confirm typed repost</button></form>"
                if view.repost
                else "<button autofocus name=confirm value=yes>Confirm approval</button></form>"
            )
            + f"<form method=post action=/story/{sid}/cancel#approve>{_hidden(token, item.seq)}"
            f"<input type=hidden name=confirmation value={_q(view.confirmation)}><button>Cancel</button></form>"
        )
    channel = (
        view.channel_id
        if view.channel_id is not None
        else (defaults.channel_id if defaults else "")
    )
    due = view.due_at if view.due_at is not None else (defaults.due_at if defaults else "")
    opened = " open" if not channel or not due or view.error and view.focus == "approve" else ""
    focus = " autofocus" if view.focus == "approve" else ""
    return (
        f"<form method=post action=/story/{sid}/{'repost-sheet' if repeat else 'sheet'}#approve>{_hidden(token, item.seq)}"
        f"<details{opened}><summary>Posting destination and time</summary>"
        f"<label>LinkedIn channel id<input{focus} type=text name=channel_id value={_q(channel)} autocomplete=off></label>"
        f"<label>Target time (UTC)<input type=text name=due_at value={_q(due)} autocomplete=off></label>"
        f"<p>{e(defaults.local if defaults else 'Choose the destination and a future time.')}</p></details>"
        f"<button>{'Review repost' if repeat else 'Approve'}</button><p class=muted>Recording your decision sends nothing.</p></form>"
    )


def _x_text(post: XPost) -> str:
    if post.text is None:
        return f"<p><b>X: nothing</b>, {e(post.reason)}.</p>"
    return (
        f"<div class=post><pre>{e(post.text)}</pre></div>"
        f"<p class=muted>{x_weight(post.text)} of {X_MAX_WEIGHT} (X's weighted count; URLs "
        "count 23). Words only from the LinkedIn post above.</p>"
    )


def _x_approval_section(x_state: XState | None) -> str:
    if x_state is None:
        return ""
    if x_state.approval is None:
        reason = x_state.post.reason or "no X approval was recorded with this approval"
        return f"<h2>X</h2><p><b>X: nothing</b>, {e(reason)}.</p>"
    parts = [
        "<h2>X</h2>",
        _x_text(x_state.post),
        f"<p class=muted>X approval id {e(x_state.approval.approval_id)}, channel "
        f"{e(x_state.approval.channel_id)}, same target time.</p>",
    ]
    if x_state.intent is not None:
        parts.append(_intent_panel(x_state.intent))
    else:
        parts.append("<p>X: not submitted. It is sent after Buffer accepts the LinkedIn post.</p>")
    return "".join(parts)


def _schedule_section(
    item: ReviewItem,
    token: str,
    scheduling: bool,
    publish_hint: str | None,
    x_state: XState | None = None,
) -> str:
    """The current approval: its persisted send intent if any, else the one schedule action."""
    approval = item.approval
    if approval is None:
        return ""
    if item.publications and item.intent is None and not item.repost_ready:
        return "<p>This story was already submitted. Only the typed Repost path can create another post.</p>"
    parts = [
        "<h2>Send confirmed repost</h2>" if item.repost_ready else "<h2>Schedule with Buffer</h2>"
    ]
    intent = item.intent
    if intent is not None:
        parts.append(_intent_panel(intent))
        if intent.state != IntentState.REJECTED:  # a refused request created nothing
            parts.append(f"<p class=notice>{e(NOT_CANCELLED)}</p>")
        x_pending = x_state is not None and x_state.approval is not None and x_state.intent is None
        if scheduling and x_pending and intent.state in LEAD_ACCEPTED:
            sid = e(item.story.story_id)
            parts.append(
                "<div class=panel><p>The X post of this approval was not sent. This sends only "
                "the X post, in one createPost request, never retried.</p>"
                f"<p>To send it, type exactly: <b>{e(schedule_phrase(approval.approval_id))}</b>"
                f"</p><form method=post action=/story/{sid}/schedule#schedule>{_hidden(token, item.seq)}"
                f"<input type=hidden name=approval_id value={_q(approval.approval_id)}>"
                '<input type=text name=typed autocomplete=off aria-label="scheduling phrase">'
                "<button>Send the X post</button></form></div>"
            )
        return "".join(parts)
    parts.append(
        "<p>Not submitted: nothing has been sent to Buffer for this approval. The target time "
        "above is not scheduled until Buffer is asked and confirms it.</p>"
    )
    if scheduling:
        sid = e(item.story.story_id)
        parts.append(
            "<div class=panel>"
            "<p>This sends exactly the approved post below to Buffer in one createPost request "
            "(customScheduled). It is never retried or resent, even after a timeout or a refresh. "
            "A Buffer acceptance is not publication.</p>"
            f"<div class=post><pre>{e(approval.text)}</pre></div>"
            "<details><summary>Destination details</summary><table>"
            f"<tr><td>Buffer channel id</td><td>{e(approval.channel_id)}</td></tr>"
            f"<tr><td>target time (UTC)</td><td>{e(approval.due_at)}</td></tr>"
            f"<tr><td>approval id</td><td>{e(approval.approval_id)}</td></tr>"
            "</table></details>"
            f"<p>To schedule, type exactly: <b>{e(schedule_phrase(approval.approval_id))}</b></p>"
            f"<form method=post action=/story/{sid}/schedule#schedule>{_hidden(token, item.seq)}"
            f"<input type=hidden name=approval_id value={_q(approval.approval_id)}>"
            '<input type=text name=typed autocomplete=off aria-label="scheduling phrase">'
            f"<button>{'Send confirmed repost' if item.repost_ready else 'Schedule approved post'}</button></form></div>"
        )
    else:
        parts.append(
            "<p>Scheduling from this page is off. Configure Buffer and restart shipyap review to enable it.</p>"
        )
    return "".join(parts)


def _withdrawn_section(item: ReviewItem) -> str:
    """Send intents of approvals a later decision withdrew: the request was made regardless."""
    intents = item.withdrawn_intents
    if not intents:
        return ""
    warning = ""
    if any(i.state != IntentState.REJECTED for i in intents):
        warning = f"<p class=error>{e(NOT_CANCELLED)}</p>"
    return (
        "<h2>Earlier Buffer submissions (approval withdrawn here)</h2>"
        + warning
        + "".join(_intent_panel(i) for i in intents)
    )


def _intent_panel(intent: Intent) -> str:
    return (
        f"<div class=panel><p>Buffer submission: <span class=badge>{e(intent.state.value)}</span>"
        f" {e(INTENT_GUIDANCE[intent.state])}</p><details><summary>Details</summary><table>"
        f"<tr><td>approval id</td><td>{e(intent.approval_id)}</td></tr>"
        f"<tr><td>Buffer post id</td><td>{e(intent.provider_post_id or 'none recorded')}</td></tr>"
        f"<tr><td>last outcome code</td><td>{e(intent.detail_code or 'none')}</td></tr>"
        f"<tr><td>submitted at (UTC)</td><td>{e(intent.created_at)}</td></tr>"
        f"<tr><td>updated at (UTC)</td><td>{e(intent.updated_at)}</td></tr>"
        "</table></details></div>"
    )


def _buffer_summary(item: ReviewItem) -> str:
    if item.intent is not None:
        return item.intent.state.value
    if item.withdrawn_intents:
        return f"{item.withdrawn_intents[-1].state.value} (approval withdrawn here)"
    return "not submitted" if item.state == ReviewState.APPROVED else ""


def _reject_section(item, token, view=None) -> str:
    sid = item.story.story_id
    opened = " open" if view and view.focus == "reject" else ""
    focus = " autofocus" if opened else ""
    typed = view.typed if view else ""
    return (
        f"<details id=reject{opened}><summary>Reject</summary>"
        f"<p>A rejection is final for this story. Type: <b>{e(reject_phrase(sid))}</b></p>"
        f"<form method=post action=/story/{e(sid)}/reject#reject>{_hidden(token, item.seq)}"
        f'<input{focus} type=text name=typed value={_q(typed)} autocomplete=off aria-label="rejection phrase">'
        "<button>Reject this draft</button></form></details>"
    )


def _history_section(item: ReviewItem) -> str:
    if not item.decisions:
        return "<h2>Decisions</h2><p>None recorded: the draft is UNAPPROVED.</p>"
    rows = "".join(
        f"<tr><td>{d.seq}</td><td>{e(d.decided_at)}</td>"
        f"<td>{_state_badge_text(d.decision)}</td><td>{e(d.draft_bundle)}</td>"
        f"<td>{e(d.text_sha256[:16])}</td><td>{e((d.approval_id or '')[:16])}</td></tr>"
        for d in item.decisions
    )
    return (
        "<h2>Decisions (append-only, newest last)</h2><table><tr><th>#</th><th>At (UTC)</th>"
        "<th>Decision</th><th>Draft bundle</th><th>Text sha256</th><th>Approval id</th></tr>"
        f"{rows}</table>"
    )


# --- small helpers -----------------------------------------------------------------------------


def _hidden(token: str, seq: int) -> str:
    return (
        f"<input type=hidden name=token value={_q(token)}><input type=hidden name=seq value={seq}>"
    )


def _q(value: str) -> str:
    return f'"{e(value)}"'


def _state_badge(state: ReviewState) -> str:
    return f"<span class='badge {e(state.value)}'>{e(state.value)}</span>"


def _state_badge_text(kind: ReviewKind) -> str:
    css = {"REVISED": "UNAPPROVED", "APPROVED": "APPROVED", "REJECTED": "REJECTED"}[kind.value]
    return f"<span class='badge {css}'>{e(kind.value)}</span>"


def _findings(findings: tuple[Finding, ...]) -> str:
    if not findings:
        return ""
    return (
        "<ul>"
        + "".join(
            f"<li class={e(f.severity.value)}>{e(f.severity.value)} {e(f.code.value)}: {e(f.message)}"
            f"{' (' + e(f.detail) + ')' if f.detail else ''}"
            f"{'<div class=muted>' + e(FINDING_NOTES[f.code]) + '</div>' if f.code in FINDING_NOTES else ''}"
            "</li>"
            for f in sorted(findings, key=lambda f: f.severity != Severity.BLOCKING)
        )
        + "</ul>"
    )


def _checked_ref(ref: CheckedSourceRef) -> str:
    return (
        f"<div class=muted>{e(ref.path)}:{ref.line_start}-{ref.line_end} "
        f"{e(ref.ref_check.value)} &middot; {_scope_text(ref.quote, ref.line_start, ref.line_end)}"
        f"</div><pre>{e(chr(10).join(ref.excerpt))}</pre>"
    )


def _scope_text(quote: str | None, start: int, end: int) -> str:
    if quote is None:
        return f"checked against: the entire cited range, lines {start}-{end}"
    return f"checked against: the quoted text only, &ldquo;{e(quote)}&rdquo;"


def _scope_choice(segment_id: str, index: int, ref: SourceRef, chosen: frozenset[str]) -> str:
    where = f"{ref.path}:{ref.line_start}-{ref.line_end}"
    if ref.quote is None:
        return (
            f"<span class=muted>{e(where)}: {_scope_text(None, ref.line_start, ref.line_end)}"
            "</span><br>"
        )
    key = scope_key(segment_id, index)
    checked = " checked" if key in chosen else ""
    return (
        f"<label class=muted><input type=checkbox name=full-{e(key)} value=1{checked}> "
        f"Use full cited range {e(where)} ({_scope_text(ref.quote, ref.line_start, ref.line_end)}"
        " now)</label><br>"
    )


def _excerpt(brief: Brief, path: object, start: object, end: object) -> str:
    if not isinstance(path, str) or not isinstance(start, int) or not isinstance(end, int):
        return ""
    source = brief.source(path)
    if source is None:
        return "(not in the brief)"
    return "\n".join(source.lines[start - 1 : end])


def default_publish_hint(item: ReviewItem, ledger_path: Path) -> str | None:
    """Compatibility for callers; obsolete manual publishing commands are no longer shown."""
    return None
