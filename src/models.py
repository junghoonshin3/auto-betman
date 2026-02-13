from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class MatchBet:
    match_number: int
    sport: str
    league: str
    home_team: str
    away_team: str
    bet_selection: str
    odds: float
    match_datetime: str
    result: str | None = None
    score: str = ""
    game_result: str = ""


@dataclass
class BetSlip:
    slip_id: str
    game_type: str
    round_number: str
    status: str
    purchase_datetime: str
    total_amount: int
    potential_payout: int
    combined_odds: float
    result: str | None = None
    actual_payout: int = 0
    matches: list[MatchBet] = field(default_factory=list)

    @property
    def title(self) -> str:
        return f"{self.game_type} {self.round_number}".strip()
