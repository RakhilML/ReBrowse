"""Rank endpoints for selection using `verify` health."""

from __future__ import annotations

from rebrowse.models import EndpointDescriptor, VerificationStatus


def _rank_key(ep: EndpointDescriptor) -> tuple[int, float]:
    verified = 1 if ep.verification_status == VerificationStatus.VERIFIED else 0
    return (verified, ep.reliability_score)


def usable_endpoints(endpoints: list[EndpointDescriptor]) -> list[EndpointDescriptor]:
    not_failed = [e for e in endpoints if e.verification_status != VerificationStatus.FAILED]
    pool = not_failed if not_failed else list(endpoints)
    return sorted(pool, key=_rank_key, reverse=True)
