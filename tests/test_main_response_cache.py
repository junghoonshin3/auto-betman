from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from src.main import (
    AnalysisCacheEntry,
    PurchasesCacheEntry,
    UserSession,
    _resolve_analysis_with_cache,
    _resolve_purchases_with_cache,
)
from src.models import BetSlip, MatchBet, PurchaseAnalysis


def _sample_slips() -> list[BetSlip]:
    return [
        BetSlip(
            slip_id="S-1",
            game_type="프로토",
            round_number="1회차",
            status="발매중",
            purchase_datetime="2026.02.13 12:00",
            total_amount=5000,
            potential_payout=0,
            combined_odds=0.0,
            result=None,
            actual_payout=0,
            matches=[
                MatchBet(
                    match_number=1,
                    sport="축구",
                    league="K",
                    home_team="A",
                    away_team="B",
                    bet_selection="승",
                    odds=1.8,
                    match_datetime="2026.02.13 13:00",
                )
            ],
        )
    ]


def _session() -> UserSession:
    return UserSession(
        context=object(),
        login_ok=True,
        storage_state_path=Path("/tmp/session.json"),
        meta_lock=asyncio.Lock(),
    )


async def test_purchases_cache_hit_within_ttl() -> None:
    session = _session()
    slips = _sample_slips()
    session.purchases_cache = PurchasesCacheEntry(slips=slips, token="t1", fetched_at_monotonic=50.0)

    probe = AsyncMock(return_value="t1")
    full = AsyncMock(return_value=slips)

    result = await _resolve_purchases_with_cache(session, probe, full, now_monotonic=lambda: 100.0)
    assert result == slips
    probe.assert_not_awaited()
    full.assert_not_awaited()


async def test_purchases_cache_unchanged_when_probe_token_same() -> None:
    session = _session()
    slips = _sample_slips()
    session.purchases_cache = PurchasesCacheEntry(slips=slips, token="same-token", fetched_at_monotonic=0.0)

    probe = AsyncMock(return_value="same-token")
    full = AsyncMock(return_value=_sample_slips())

    result = await _resolve_purchases_with_cache(session, probe, full, now_monotonic=lambda: 100.0)
    assert result == slips
    probe.assert_awaited_once()
    full.assert_not_awaited()
    assert session.purchases_cache is not None
    assert session.purchases_cache.fetched_at_monotonic == 100.0


async def test_purchases_cache_refreshes_when_probe_token_changed() -> None:
    session = _session()
    old_slips = _sample_slips()
    new_slips = _sample_slips()
    new_slips[0].slip_id = "S-2"
    session.purchases_cache = PurchasesCacheEntry(slips=old_slips, token="old", fetched_at_monotonic=0.0)

    probe = AsyncMock(return_value="new")
    full = AsyncMock(return_value=new_slips)

    result = await _resolve_purchases_with_cache(session, probe, full, now_monotonic=lambda: 100.0)
    assert result == new_slips
    probe.assert_awaited_once()
    full.assert_awaited_once()
    assert session.purchases_cache is not None
    assert session.purchases_cache.token == "new"


async def test_purchases_uses_stale_cache_on_transient_error() -> None:
    session = _session()
    slips = _sample_slips()
    session.purchases_cache = PurchasesCacheEntry(slips=slips, token="t1", fetched_at_monotonic=50.0)

    probe = AsyncMock(side_effect=RuntimeError("timeout"))
    full = AsyncMock(return_value=slips)

    result = await _resolve_purchases_with_cache(session, probe, full, now_monotonic=lambda: 120.0)
    assert result == slips
    full.assert_not_awaited()


async def test_purchases_stale_cache_expired_raises() -> None:
    session = _session()
    slips = _sample_slips()
    session.purchases_cache = PurchasesCacheEntry(slips=slips, token="t1", fetched_at_monotonic=0.0)

    probe = AsyncMock(side_effect=RuntimeError("timeout"))
    full = AsyncMock(return_value=slips)

    with pytest.raises(RuntimeError, match="timeout"):
        await _resolve_purchases_with_cache(session, probe, full, now_monotonic=lambda: 700.0)


async def test_analysis_cache_hit_within_ttl() -> None:
    session = _session()
    result = PurchaseAnalysis(months=1, purchase_amount=1000, winning_amount=500)
    session.analysis_cache_by_month[1] = AnalysisCacheEntry(result=result, token="1:1000:500", fetched_at_monotonic=50.0)

    probe = AsyncMock(return_value=("1:1000:500", result))
    full = AsyncMock(return_value=result)

    actual = await _resolve_analysis_with_cache(session, 1, probe, full, now_monotonic=lambda: 100.0)
    assert actual == result
    probe.assert_not_awaited()
    full.assert_not_awaited()


async def test_analysis_refreshes_when_token_changed() -> None:
    session = _session()
    old = PurchaseAnalysis(months=1, purchase_amount=1000, winning_amount=500)
    new = PurchaseAnalysis(months=1, purchase_amount=2000, winning_amount=600)
    session.analysis_cache_by_month[1] = AnalysisCacheEntry(result=old, token="1:1000:500", fetched_at_monotonic=0.0)

    probe = AsyncMock(return_value=("1:2000:600", new))
    full = AsyncMock(return_value=new)

    actual = await _resolve_analysis_with_cache(session, 1, probe, full, now_monotonic=lambda: 100.0)
    assert actual == new
    full.assert_awaited_once()


async def test_analysis_uses_month_specific_cache() -> None:
    session = _session()
    m1 = PurchaseAnalysis(months=1, purchase_amount=1000, winning_amount=500)
    m2 = PurchaseAnalysis(months=2, purchase_amount=2000, winning_amount=700)
    session.analysis_cache_by_month[1] = AnalysisCacheEntry(result=m1, token="1:1000:500", fetched_at_monotonic=50.0)
    session.analysis_cache_by_month[2] = AnalysisCacheEntry(result=m2, token="2:2000:700", fetched_at_monotonic=50.0)

    probe = AsyncMock()
    full = AsyncMock()

    actual_1 = await _resolve_analysis_with_cache(session, 1, probe, full, now_monotonic=lambda: 100.0)
    actual_2 = await _resolve_analysis_with_cache(session, 2, probe, full, now_monotonic=lambda: 100.0)

    assert actual_1 == m1
    assert actual_2 == m2
    probe.assert_not_awaited()
    full.assert_not_awaited()


async def test_analysis_uses_stale_cache_on_transient_error() -> None:
    session = _session()
    cached = PurchaseAnalysis(months=1, purchase_amount=1000, winning_amount=500)
    session.analysis_cache_by_month[1] = AnalysisCacheEntry(result=cached, token="1:1000:500", fetched_at_monotonic=50.0)

    probe = AsyncMock(side_effect=RuntimeError("timeout"))
    full = AsyncMock(return_value=cached)

    actual = await _resolve_analysis_with_cache(session, 1, probe, full, now_monotonic=lambda: 120.0)
    assert actual == cached
    full.assert_not_awaited()


async def test_purchases_does_not_return_stale_cache_when_session_expired() -> None:
    session = _session()
    slips = _sample_slips()
    session.purchases_cache = PurchasesCacheEntry(slips=slips, token="t1", fetched_at_monotonic=50.0)
    session.last_session_expired_at = 80.0

    probe = AsyncMock(side_effect=RuntimeError("timeout"))
    full = AsyncMock(return_value=slips)

    with pytest.raises(RuntimeError, match="세션이 만료되었습니다"):
        await _resolve_purchases_with_cache(session, probe, full, now_monotonic=lambda: 120.0)


async def test_analysis_does_not_return_stale_cache_when_session_expired() -> None:
    session = _session()
    cached = PurchaseAnalysis(months=1, purchase_amount=1000, winning_amount=500)
    session.analysis_cache_by_month[1] = AnalysisCacheEntry(result=cached, token="1:1000:500", fetched_at_monotonic=50.0)
    session.last_session_expired_at = 80.0

    probe = AsyncMock(side_effect=RuntimeError("timeout"))
    full = AsyncMock(return_value=cached)

    with pytest.raises(RuntimeError, match="세션이 만료되었습니다"):
        await _resolve_analysis_with_cache(session, 1, probe, full, now_monotonic=lambda: 120.0)
