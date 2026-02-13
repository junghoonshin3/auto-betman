from __future__ import annotations

import pytest

from src.auth import TransientNetworkError, _is_transient_network_error, is_logged_in


class _FakePage:
    def __init__(self, goto_outcomes: list[object], logged_in: bool = True) -> None:
        self._goto_outcomes = list(goto_outcomes)
        self._logged_in = logged_in
        self.goto_calls = 0

    async def goto(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        self.goto_calls += 1
        outcome = self._goto_outcomes.pop(0) if self._goto_outcomes else None
        if isinstance(outcome, Exception):
            raise outcome
        return None

    async def evaluate(self, script: str):
        if script == "window.stop()":
            return None
        return self._logged_in


def test_is_transient_network_error_detection() -> None:
    assert _is_transient_network_error("net::ERR_CONNECTION_REFUSED")
    assert _is_transient_network_error("Timeout 30000ms exceeded")
    assert _is_transient_network_error("Connection reset by peer")
    assert not _is_transient_network_error("selector not found")


async def test_is_logged_in_retries_then_success() -> None:
    page = _FakePage([Exception("net::ERR_CONNECTION_REFUSED"), None], logged_in=True)

    result = await is_logged_in(page, retries=2, base_delay=0.0)

    assert result is True
    assert page.goto_calls == 2


async def test_is_logged_in_raises_after_transient_retry_exhausted() -> None:
    page = _FakePage([Exception("Timeout 30000ms exceeded") for _ in range(3)], logged_in=False)

    with pytest.raises(TransientNetworkError):
        await is_logged_in(page, retries=2, base_delay=0.0)


async def test_is_logged_in_non_transient_error_returns_false() -> None:
    page = _FakePage([Exception("selector evaluation failed")], logged_in=False)

    result = await is_logged_in(page, retries=2, base_delay=0.0)

    assert result is False
    assert page.goto_calls == 1
