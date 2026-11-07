"""Plain labels over existing review results. No judging, stores or side effects."""

import re
from html import escape

from shipyap.editorial.models import DraftStatus
from shipyap.publication.ledger import IntentState
from shipyap.review.gate import Gate, judge
from shipyap.review.service import ReviewItem, ReviewState

REASONS = {
    "REF_PATH_NOT_IN_SNAPSHOT": "The cited file is missing from the saved evidence.",
    "REF_RANGE_INVALID": "The cited line range is unavailable.",
    "REF_QUOTE_NOT_FOUND": "The quoted words do not match the cited lines.",
    "AUTHOR_REF_NOT_FOUND": "The cited author statement is missing.",
    "NUMBER_NOT_IN_EVIDENCE": "A number is missing from the evidence this sentence cites.",
    "FIRST_PERSON_UNATTRIBUTED": "This first-person claim needs your own recorded statement.",
    "COMPARISON_UNSUPPORTED": "The comparison is not backed by its cited evidence.",
    "HASHTAG_NOT_ALLOWED": "This hashtag is not in your saved author settings.",
    "LINK_NOT_ALLOWED": "This link is not allowed in the saved post.",
    "TOOL_ITEM_CITED": "This sentence cites a tool note instead of repository evidence.",
    "TOOL_PHRASE": "This sentence contains wording from the tool's instructions.",
    "AUTHOR_NOT_STATEMENT": "This author claim does not cite your own statement.",
    "POST_TOO_LONG": "The post exceeds the posting length limit.",
    "FIRST_PERSON_FROM_STATEMENT": "Check that these first-person words match your statement.",
    "PROMOTIONAL_LANGUAGE": "Check whether this wording sounds promotional.",
    "COMPARISON_ATTRIBUTED": "Check that the quoted comparison is presented fairly.",
    "SPELLED_NUMBER": "Check this written-out number against the cited evidence.",
    "MARKDOWN_SYNTAX": "Check how this formatting will look in the published post.",
    "VERIFICATION_REF_INVALID": "A reference for this review question is unavailable.",
    "TOOL_WORD": "Check whether wording from the tool belongs in this post.",
    "AUTHOR_RECLASSIFIED": "This reads as interpretation rather than your recorded statement.",
    "NO_INSIGHT_REFERENCE": "Check that the post explains the useful takeaway.",
    "NO_SOURCE_REFERENCE": "The post needs a reference to repository evidence.",
    "NO_LIMITATION_REFERENCE": "Check that the post states its scope and limitations.",
    "OUTDATED": "A later record supersedes what this sentence says.",
    "FACT_CHECK_OUTDATED": "The stored fact check found a later record that supersedes this claim.",
    "FACT_CHECK_CONTRADICTED": "The stored fact check found evidence against this claim.",
    "FACT_CHECK_OVERSTATED": "The stored fact check found this wording stronger than the evidence.",
    "FACT_CHECK_UNVERIFIABLE": "The stored fact check could not verify this claim.",
    "NOT_RECHECKED": "This sentence changed after the fact check; read its evidence again.",
    "STALE": "This draft was made under older rules and cannot be approved.",
    "NO_POST": "There is no post text to approve.",
    "NEEDS_REVISION": "The saved draft needs changes before approval.",
}


def effective_gate(item: ReviewItem, gate: Gate | None, fact_check=None) -> Gate:
    return (
        gate
        if gate is not None
        else judge(
            item.current.amap,
            item.current.response,
            fact_check=fact_check,
            checked=item.base.response,
        )
    )


def attention_count(gate: Gate) -> int:
    return sum(bool(s.blocks or s.warnings) for s in gate.segments) + bool(
        gate.document_blocks or gate.document_warnings
    )


def publication_status(item: ReviewItem) -> str | None:
    sent = [p for p in item.publications if p.state == "SENT"]
    if sent:
        post = min(sent, key=lambda p: p.created_at)
        return f"Published {post.updated_at[:10]} (UTC confirmation date)"
    scheduled = [p for p in item.publications if p.state == "SCHEDULED"]
    if scheduled:
        post = min(scheduled, key=lambda p: p.due_at)
        return f"Scheduled {post.due_at} (UTC)"
    return None


def status(item: ReviewItem, gate: Gate | None = None) -> str:
    durable = publication_status(item)
    if durable:
        return durable
    if item.state == ReviewState.REJECTED:
        return "Rejected"
    intent = item.intent
    if intent is not None:
        if intent.state == IntentState.SCHEDULED:
            return f"Scheduled {item.approval.due_at} (UTC)"
        if intent.state == IntentState.SENT:
            return "Published"
        return {
            IntentState.ACCEPTED: "Buffer accepted; schedule not confirmed",
            IntentState.SENDING: "Send outcome uncertain — check Buffer",
            IntentState.AMBIGUOUS: "Send outcome uncertain — check Buffer",
            IntentState.REJECTED: "Buffer refused the post",
            IntentState.NOT_SCHEDULED: "Not scheduled in Buffer",
            IntentState.PROVIDER_ERROR: "Buffer reports a problem",
            IntentState.MISMATCH: "Buffer's copy differs — check Buffer",
        }[intent.state]
    if item.state == ReviewState.APPROVED:
        return "Approved, not scheduled"
    gate = effective_gate(item, gate)
    if any(note.code == "FACT_CHECK_REQUIRED" for note in gate.document_blocks):
        return "Not fact-checked - cannot approve"
    if item.stale or not gate.approvable or attention_count(gate):
        return "Needs attention"
    if item.current.amap.draft_status == DraftStatus.READY_FOR_REVIEW:
        return "Ready to approve"
    return "Needs attention"


def flagged(item: ReviewItem, gate: Gate, records=()) -> str:
    checked = {s.id: s for s in item.current.amap.segments}
    cards = []
    for segment in gate.segments:
        if not (segment.blocks or segment.warnings):
            continue
        source = checked[segment.segment_id]
        lines = []
        for ref in source.source_refs:
            excerpt = "\n".join(
                f"{ref.line_start + index}: {line}" for index, line in enumerate(ref.excerpt)
            )
            scope = "quoted words only" if ref.quote is not None else "the whole cited range"
            lines.append(
                f"<p>{escape(ref.path)}:{ref.line_start}-{ref.line_end} · checked against {scope}</p>"
                f"<pre>{escape(excerpt or 'Cited evidence unavailable.')}</pre>"
            )
            if ref.quote is not None:
                lines.append(f"<p>Checked quote: <q>{escape(ref.quote)}</q></p>")
        for author in source.author_refs:
            lines.append(
                f"<p>Your statement: {escape(author.text or 'Statement unavailable.')}</p>"
            )
        refs = dict.fromkeys(r for n in (*segment.blocks, *segment.warnings) for r in n.refs)
        for ref in refs:
            match = re.fullmatch(r"(.+?):(\d+)(?:-(\d+))?", ref)
            path = match[1] if match else ref
            candidates = {tuple(record.lines) for record in records if record.path == path}
            if len(candidates) == 1:
                content = candidates.pop()
                start = int(match[2]) if match else 1
                end = int(match[3] or match[2]) if match else min(len(content), 20)
                selected = content[start - 1 : end] if 1 <= start <= end <= len(content) else ()
                text = "\n".join(f"{start + i}: {line}" for i, line in enumerate(selected))
                label = ref if match else f"{ref} (record excerpt, first {end} lines)"
            else:
                label, text = ref, "Related evidence unavailable or ambiguous."
            lines.append(
                f"<p>{escape(label)}</p><pre>{escape(text or 'Evidence unavailable.')}</pre>"
            )
        reasons = _reasons(segment.blocks, "Must fix") + _reasons(segment.warnings, "Check this")
        cards.append(
            f'<article class="panel flagged" id="flag-{escape(segment.segment_id)}">'
            f'<div class="claim-evidence"><div><blockquote>{escape(source.text)}</blockquote>{reasons}</div><div>'
            + ("".join(lines) or "<p>No cited evidence is available for this sentence.</p>")
            + "</div></div></article>"
        )
    if gate.document_blocks or gate.document_warnings:
        cards.append(
            '<article class="panel flagged"><h2>Whole post</h2>'
            + _reasons(gate.document_blocks, "Must fix")
            + _reasons(gate.document_warnings, "Check this")
            + "</article>"
        )
    return "".join(cards)


def _reasons(notes, label):
    return "".join(
        f"<p><strong>{label}</strong> — "
        f"{escape(REASONS.get(n.code, 'Read this finding and its evidence in Details.'))}</p>"
        for n in notes
    )
