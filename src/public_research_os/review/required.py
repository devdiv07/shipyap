"""Validate the stored fact-check record before granting approval."""

from public_research_os.automation.factcheck import VERDICTS


def completed_fact_check(check, response) -> bool:
    if not isinstance(check, dict) or not isinstance(check.get("verdicts"), list):
        return False
    segments = {segment.id for segment in response.segments}
    if not segments:
        return False
    found = set()
    for verdict in check["verdicts"]:
        if not isinstance(verdict, dict):
            return False
        sid = verdict.get("segment_id")
        if (
            not isinstance(sid, str)
            or sid not in segments
            or sid in found
            or not isinstance(verdict.get("verdict"), str)
            or verdict.get("verdict") not in VERDICTS
            or not isinstance(verdict.get("reason"), str)
            or not isinstance(verdict.get("refs"), (list, tuple))
            or not all(isinstance(ref, str) for ref in verdict["refs"])
        ):
            return False
        found.add(sid)
    return found == segments
