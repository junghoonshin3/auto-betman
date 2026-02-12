from __future__ import annotations

import json
from pathlib import Path

import discord

from src.discord_bot import _build_embed, _load_notified_ids, _save_notified_ids
from src.models import BetSlip, MatchBet


class TestBuildEmbed:
    def test_basic_embed(self, sample_bet_slip: BetSlip):
        embed = _build_embed(sample_bet_slip)
        assert isinstance(embed, discord.Embed)
        assert embed.title == "프로토 승부식 제100회"
        assert embed.colour == discord.Colour.green()

    def test_embed_fields_present(self, sample_bet_slip: BetSlip):
        embed = _build_embed(sample_bet_slip)
        field_names = [f.name for f in embed.fields]
        assert "상태" in field_names
        assert "구매일시" in field_names

    def test_embed_match_info(self, sample_bet_slip: BetSlip):
        embed = _build_embed(sample_bet_slip)
        # Match field should contain team names
        match_fields = [f for f in embed.fields if "K리그1" in (f.name or "")]
        assert len(match_fields) == 1
        assert "전북현대" in match_fields[0].value
        assert "울산현대" in match_fields[0].value

    def test_embed_footer(self, sample_bet_slip: BetSlip):
        embed = _build_embed(sample_bet_slip)
        assert "5,000원" in embed.footer.text
        assert "10,500원" in embed.footer.text

    def test_embed_closed_status_colour(self):
        slip = BetSlip(
            slip_id="x",
            game_type="토토",
            round_number="1회",
            status="발매마감",
            purchase_datetime="",
            total_amount=0,
            potential_payout=0,
            combined_odds=0,
        )
        embed = _build_embed(slip)
        assert embed.colour == discord.Colour.orange()

    def test_embed_unknown_status_colour(self):
        slip = BetSlip(
            slip_id="x",
            game_type="토토",
            round_number="1회",
            status="unknown",
            purchase_datetime="",
            total_amount=0,
            potential_payout=0,
            combined_odds=0,
        )
        embed = _build_embed(slip)
        assert embed.colour == discord.Colour.blurple()


class TestNotifiedIds:
    def test_load_empty(self, tmp_path: Path):
        path = tmp_path / "notified.json"
        assert _load_notified_ids(path) == set()

    def test_save_and_load(self, tmp_path: Path):
        path = tmp_path / "notified.json"
        ids = {"abc", "def", "ghi"}
        _save_notified_ids(path, ids)
        loaded = _load_notified_ids(path)
        assert loaded == ids

    def test_load_corrupted(self, tmp_path: Path):
        path = tmp_path / "notified.json"
        path.write_text("not valid json", encoding="utf-8")
        assert _load_notified_ids(path) == set()

    def test_save_creates_directory(self, tmp_path: Path):
        path = tmp_path / "subdir" / "notified.json"
        _save_notified_ids(path, {"test"})
        assert path.exists()
        loaded = _load_notified_ids(path)
        assert loaded == {"test"}
