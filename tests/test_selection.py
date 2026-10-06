from __future__ import annotations

from rebrowse.models import EndpointDescriptor, HttpMethod, VerificationStatus
from rebrowse.selection import usable_endpoints


def _ep(url, status=VerificationStatus.UNVERIFIED, reliability=0.5):
    return EndpointDescriptor(
        method=HttpMethod.GET, url_template=url,
        verification_status=status, reliability_score=reliability,
    )


def test_failed_endpoints_are_dropped():
    eps = [
        _ep("https://x/a", VerificationStatus.FAILED, 0.9),
        _ep("https://x/b", VerificationStatus.VERIFIED, 0.6),
    ]
    out = usable_endpoints(eps)
    assert [e.url_template for e in out] == ["https://x/b"]


def test_ranking_verified_first_then_reliability():
    eps = [
        _ep("https://x/low", VerificationStatus.UNVERIFIED, 0.2),
        _ep("https://x/verified", VerificationStatus.VERIFIED, 0.5),
        _ep("https://x/high", VerificationStatus.UNVERIFIED, 0.9),
    ]
    out = [e.url_template for e in usable_endpoints(eps)]
    assert out == ["https://x/verified", "https://x/high", "https://x/low"]


def test_all_failed_falls_back_to_full_list():
    eps = [
        _ep("https://x/a", VerificationStatus.FAILED, 0.3),
        _ep("https://x/b", VerificationStatus.FAILED, 0.8),
    ]
    out = usable_endpoints(eps)
    assert {e.url_template for e in out} == {"https://x/a", "https://x/b"}
    assert out[0].url_template == "https://x/b"
