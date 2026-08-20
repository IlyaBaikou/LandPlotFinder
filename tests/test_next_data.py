import json

import pytest

from app.sources.next_data import extract_next_data


def test_extract_next_data() -> None:
    payload = {"props": {"pageProps": {"objects": [{"code": 42}]}}}
    html = (
        '<html><script id="__NEXT_DATA__" type="application/json">'
        f"{json.dumps(payload)}"
        "</script></html>"
    )

    assert extract_next_data(html) == payload


def test_extract_next_data_fails_when_page_shape_changed() -> None:
    with pytest.raises(ValueError, match="__NEXT_DATA__"):
        extract_next_data("<html></html>")
