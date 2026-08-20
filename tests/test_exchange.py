from app.exchange import FALLBACK_USD_RATE_URL, NBRB_USD_RATE_URL, official_byn_per_usd


class FallbackClient:
    def get_json(self, url: str) -> dict:
        if url == NBRB_USD_RATE_URL:
            raise RuntimeError("NBRB temporarily unavailable")
        assert url == FALLBACK_USD_RATE_URL
        return {"rates": {"BYN": 2.9}}


def test_uses_secondary_rate_when_nbrb_is_temporarily_unavailable() -> None:
    assert official_byn_per_usd(FallbackClient()) == 2.9
