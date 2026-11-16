"""El listado de GCS se detiene al llegar al tope, sin pedir la página siguiente."""

from mesa_trading.gcs import _list


def test_list_stops_during_pagination_when_the_cap_is_hit():
    calls = []

    def fetch(url: str) -> dict:
        calls.append(url)
        if "pageToken" in url:
            raise AssertionError(url)
        assert "maxResults=6" in url
        return {
            "items": [{"name": f"logs/n{index}.jsonl"} for index in range(6)],
            "nextPageToken": "pagina-2",
        }

    names, truncated = _list("bucket", "logs/dt=2026-11-16/", "tok", max_results=6, fetch=fetch)
    assert truncated is True
    assert len(names) == 6
    assert len(calls) == 1


def test_list_is_not_truncated_when_the_prefix_ends():
    def fetch(url: str) -> dict:
        assert "maxResults=6" in url
        return {"items": [{"name": "logs/solo.jsonl"}]}

    names, truncated = _list("bucket", "logs/", "tok", max_results=6, fetch=fetch)
    assert truncated is False
    assert names == ["logs/solo.jsonl"]
