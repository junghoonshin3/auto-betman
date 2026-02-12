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

    def summary(self) -> str:
        return (
            f"[{self.league}] {self.home_team} vs {self.away_team} "
            f"→ **{self.bet_selection}** (배당 {self.odds:.2f})"
        )


@dataclass
class GameSchedule:
    match_seq: int          # 경기번호 (161)
    sport: str              # 종목 (축구, 농구)
    league: str             # 리그 (ACL2, KBL, NBA)
    game_type: str          # 게임유형 (일반, 핸디캡, 언더오버, SUM)
    home_team: str          # 홈팀
    away_team: str          # 원정팀
    odds: dict[str, float]  # 배당률 {"승": 2.75, "무": 2.95, "패": 2.17}
    deadline: str           # 마감시간 ("02.12 (목) 19:00")
    game_datetime: str      # 경기시간
    stadium: str            # 장소
    handicap: str           # 핸디캡 값 ("H +1.0", "U/O 2.5", "")


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
        return f"{self.game_type} {self.round_number}"
