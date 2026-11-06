"""Orchestration of `draft-model`.

Everything that can be refused without a request is refused first, and nothing is written until
the unchanged Assignment 03 path publishes the bundle:

1. `plan_model_draft` runs offline. It checks the model allowlist and budget format, preflights
   `--out`, then verifies the snapshot, the brief bundle and that the brief was built from the
   snapshot (`editorial.service.prepare_build`, exactly as for `draft-build`). brief.md must be
   byte-identical to the rendering of the verified brief.json, so the file an operator can read is
   exactly what would be sent. Then come the size limit and the offline estimate. No key is read.
2. `run_model_draft` asks the provider to count the request's input tokens (a separate request
   that generates nothing; any failure stops the run). If the estimated maximum exceeds the budget
   it stops; otherwise it makes exactly one generation request. The reply must have ended normally
   with exactly one text block of strict JSON. The tool adds its own fields, and
   `editorial.service.build_from_response` parses, checks and publishes it as `draft-build` would.

Nothing here approves, verifies or publishes. A model's NO_POST, and a reply with blocking
findings, become bundles (exit 1), exactly as for any other drafter.
"""

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from pydantic import ValidationError

from shipyap.drafting.errors import (
    CallReport,
    DraftingError,
    DraftingErrorCode,
    GenerationRejected,
    Phase,
)
from shipyap.drafting.models import Drafter, ModelReply, ModelRequest
from shipyap.drafting.normalise import ResponseRepair, normalise_response
from shipyap.drafting.policy import (
    EFFORT,
    MAX_MODEL_BRIEF_BYTES,
    MAX_OUTPUT_TOKENS,
    MODELS,
    Estimate,
    charge_estimate,
    counted_estimate,
    offline_estimate,
    parse_budget,
)
from shipyap.drafting.prompt import (
    MODEL_OUTPUT_SCHEMA,
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    generator_label,
    prompt_version,
    safe_model_id,
    wrap_response,
)
from shipyap.editorial.bundle import preflight_out
from shipyap.editorial.errors import EditorialError, EditorialErrorCode
from shipyap.editorial.inputs import RESPONSE_CAP_BYTES
from shipyap.editorial.models import Brief, GeneratorKind, SourceRef
from shipyap.editorial.render import render_brief_md
from shipyap.editorial.service import (
    BuildResult,
    PreparedBrief,
    build_from_response,
    prepare_build,
)
from shipyap.evidence.report import canonical_json
from shipyap.evidence.strict_json import StrictJsonError, loads_strict

_TRUNCATED = frozenset({"max_tokens", "model_context_window_exceeded"})


@dataclass(frozen=True)
class DraftPlan:
    model: str
    request: ModelRequest
    brief_md_sha256: str
    brief_md_bytes: int
    request_bytes: int  # UTF-8 bytes of the system prompt, brief.md and the output schema
    budget_usd: Decimal
    offline: Estimate


@dataclass(frozen=True)
class ModelDraftResult:
    build: BuildResult
    preflight: Estimate
    call: CallReport


def plan_model_draft(
    snapshot_dir: Path,
    brief_dir: Path,
    out: Path,
    *,
    model: str,
    budget: str,
    system_prompt: str = SYSTEM_PROMPT,
) -> tuple[PreparedBrief, DraftPlan]:
    if model not in MODELS:
        raise DraftingError(DraftingErrorCode.MODEL_UNKNOWN)
    budget_usd = parse_budget(budget)
    if budget_usd is None:
        raise DraftingError(DraftingErrorCode.BUDGET_INVALID)
    preflight_out(out)
    prepared = prepare_build(snapshot_dir, brief_dir)
    if len(prepared.brief_md) > MAX_MODEL_BRIEF_BYTES:
        raise DraftingError(DraftingErrorCode.BRIEF_TOO_LARGE_FOR_MODEL)
    brief_md = render_brief_md(prepared.brief, prepared.brief_sha256)
    if brief_md.encode("utf-8") != prepared.brief_md:
        # The record protects brief.md only against accidents (EDIT-02); the model must receive
        # what the verified brief.json renders to, and what the operator can read.
        raise EditorialError(EditorialErrorCode.BRIEF_INCOMPLETE)
    request = ModelRequest(
        model=model,
        system_prompt=system_prompt,
        user_text=brief_md,
        output_schema=MODEL_OUTPUT_SCHEMA,
        max_tokens=MAX_OUTPUT_TOKENS,
        effort=EFFORT,
    )
    request_bytes = sum(
        len(part.encode("utf-8"))
        for part in (system_prompt, brief_md, json.dumps(MODEL_OUTPUT_SCHEMA))
    )
    plan = DraftPlan(
        model=model,
        request=request,
        brief_md_sha256=hashlib.sha256(prepared.brief_md).hexdigest(),
        brief_md_bytes=len(prepared.brief_md),
        request_bytes=request_bytes,
        budget_usd=budget_usd,
        offline=offline_estimate(model, request_bytes),
    )
    return prepared, plan


def run_model_draft(
    prepared: PreparedBrief, plan: DraftPlan, drafter: Drafter, out: Path
) -> ModelDraftResult:
    estimate = counted_estimate(plan.model, drafter.count_input_tokens(plan.request))
    if estimate.max_cost_usd > plan.budget_usd:
        raise DraftingError(DraftingErrorCode.BUDGET_EXCEEDED, phase=Phase.COUNT, estimate=estimate)

    reply = drafter.generate(plan.request)
    call = CallReport(
        model=safe_model_id(reply.model) or plan.model,
        request_id=reply.request_id,
        usage=reply.usage,
        estimated_charge_usd=charge_estimate(plan.model, reply.usage),
    )
    try:
        raw = response_bytes(
            reply, prepared.brief_sha256, prompt_version(plan.request.system_prompt)
        )
        build = build_from_response(prepared, checked_input(raw, prepared.brief).data, out)
    except (DraftingError, EditorialError) as exc:
        raise GenerationRejected(exc, call) from None
    return ModelDraftResult(build=build, preflight=estimate, call=call)


def tidy_model_questions(data: bytes) -> bytes:
    """A model-written response with every reference that breaks the schema removed from its
    verification questions (for example one spanning more than 10 lines). The questions stay:
    they are notes for the reviewer and never post text. Post segments are not touched, and
    anything that is not a model-written response object is returned unchanged for the strict
    parser to judge."""
    try:
        value = loads_strict(data)
    except (StrictJsonError, ValueError):
        return data
    generator = value.get("generator") if isinstance(value, dict) else None
    questions = value.get("verification_needed") if isinstance(value, dict) else None
    if (
        not isinstance(generator, dict)
        or generator.get("kind") != GeneratorKind.EXTERNAL_MODEL.value
        or not isinstance(questions, list)
    ):
        return data
    changed = False
    for question in questions:
        refs = question.get("source_refs") if isinstance(question, dict) else None
        if not isinstance(refs, list):
            continue
        kept = []
        for ref in refs:
            try:
                SourceRef.model_validate(ref)
            except ValidationError:
                changed = True
                continue
            kept.append(ref)
        question["source_refs"] = kept
    return canonical_json(value).encode("utf-8") if changed else data


def checked_input(data: bytes, brief: Brief) -> ResponseRepair:
    """What a kept model response is checked as: its questions tidied, then the deterministic
    repairs of `normalise_response` against the lines the brief sent. The kept response.json is
    never rewritten; the draft bundle holds the repaired copy, and anyone can re-derive it."""
    files = {source.path: source.lines for source in brief.sources}
    return normalise_response(tidy_model_questions(data), files)


def response_bytes(reply: ModelReply, brief_sha256: str, version: str = PROMPT_VERSION) -> bytes:
    """The response.json a model reply becomes, or the error that refuses it. Pure."""

    def malformed(code: DraftingErrorCode) -> DraftingError:
        return DraftingError(code, phase=Phase.GENERATE, request_id=reply.request_id)

    if reply.stop_reason == "refusal":
        raise malformed(DraftingErrorCode.MODEL_REFUSED)
    if reply.stop_reason in _TRUNCATED:
        raise malformed(DraftingErrorCode.MODEL_TRUNCATED)
    model = safe_model_id(reply.model)
    if reply.stop_reason != "end_turn" or len(reply.texts) != 1 or model is None:
        raise malformed(DraftingErrorCode.MODEL_REPLY_MALFORMED)
    # A lone surrogate survives as invalid UTF-8 and is refused by the strict decoder below.
    text = reply.texts[0].encode("utf-8", "surrogatepass")
    if len(text) > RESPONSE_CAP_BYTES:
        raise EditorialError(EditorialErrorCode.RESPONSE_TOO_LARGE)
    try:
        value = loads_strict(text)
    except StrictJsonError:
        raise EditorialError(EditorialErrorCode.RESPONSE_INVALID_JSON) from None
    response = tidy_model_questions(
        wrap_response(value, brief_sha256, generator_label(model, reply.request_id, version))
    )
    # The canonical form escapes non-ASCII text, so it can be several times larger than the reply.
    # Every response.json written must stay within what draft-build reads (A03's inclusive cap).
    if len(response) > RESPONSE_CAP_BYTES:
        raise EditorialError(EditorialErrorCode.RESPONSE_TOO_LARGE)
    return response
