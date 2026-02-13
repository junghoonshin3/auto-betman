from __future__ import annotations

from src.bot import _build_analysis_embed
from src.models import PurchaseAnalysis


def test_analysis_embed_title_and_fields() -> None:
    embed = _build_analysis_embed(
        PurchaseAnalysis(
            months=12,
            purchase_amount=120000,
            winning_amount=34500,
        )
    )

    assert "최근 12개월" in (embed.title or "")
    fields = {field.name: field.value for field in embed.fields}
    assert fields["구매금액"] == "120,000원"
    assert fields["적중금액"] == "34,500원"
    assert set(fields.keys()) == {"구매금액", "적중금액"}
