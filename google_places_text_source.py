"""Google Places Text Search (New) connector shell.

This module is intentionally not wired into formlist.py or CSV output.
Places API usage, storage, export, customer distribution, billing, and ISV
terms need explicit approval before real use.

Docs basis checked by parent task:
- POST https://places.googleapis.com/v1/places:searchText
- textQuery and X-Goog-FieldMask are required.
- nextPageToken must be included in the field mask to page.
- websiteUri / phone fields are Enterprise SKU fields.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


ENDPOINT = "https://places.googleapis.com/v1/places:searchText"
DEFAULT_FIELD_MASK = (
    "places.id",
    "places.displayName",
    "places.formattedAddress",
    "places.websiteUri",
    "places.nationalPhoneNumber",
    "nextPageToken",
)
MAX_PAGE_SIZE = 20


@dataclass(frozen=True)
class PlacesTextConfig:
    area: str
    term: str
    api_key: str = ""
    page_size: int = MAX_PAGE_SIZE
    max_pages: int = 3
    request_budget: int = 3
    timeout: int = 20
    language_code: str = "ja"
    region_code: str = "JP"
    field_mask: tuple[str, ...] = DEFAULT_FIELD_MASK


@dataclass(frozen=True)
class PlaceCandidate:
    place_id: str
    name: str
    address: str = ""
    website_uri: str = ""
    phone: str = ""


@dataclass
class PlacesTextResult:
    status: str
    rows: list[PlaceCandidate] = field(default_factory=list)
    reason: str = ""
    requests: int = 0
    pages: int = 0
    next_page_token: str = ""


HttpTransport = Callable[[str, str, dict[str, str], dict[str, Any], int], Any]


def text_query(area: str, term: str) -> str:
    return " ".join(part.strip() for part in (term, area) if part and part.strip())


def request_body(config: PlacesTextConfig, page_token: str = "") -> dict[str, Any]:
    body: dict[str, Any] = {
        "textQuery": text_query(config.area, config.term),
        "languageCode": config.language_code,
        "regionCode": config.region_code,
        "pageSize": max(1, min(int(config.page_size), MAX_PAGE_SIZE)),
    }
    if page_token:
        body["pageToken"] = page_token
    return body


def headers(config: PlacesTextConfig) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": config.api_key,
        "X-Goog-FieldMask": ",".join(config.field_mask),
    }


def _candidate(place: dict[str, Any]) -> PlaceCandidate:
    display = place.get("displayName") or {}
    return PlaceCandidate(
        place_id=str(place.get("id") or ""),
        name=str(display.get("text") or ""),
        address=str(place.get("formattedAddress") or ""),
        website_uri=str(place.get("websiteUri") or ""),
        phone=str(place.get("nationalPhoneNumber") or ""),
    )


def _json(resp: Any) -> dict[str, Any]:
    data = resp.json()
    return data if isinstance(data, dict) else {}


def search_text(config: PlacesTextConfig, transport: HttpTransport) -> PlacesTextResult:
    """Run a bounded in-memory Text Search (New) query.

    No API key is read from env/files. No response body or key is logged.
    Callers must inject a transport, which lets tests use anonymous mocks.
    """
    if not config.api_key:
        return PlacesTextResult(status="blocked", reason="api_key_required")
    if "nextPageToken" not in config.field_mask:
        return PlacesTextResult(status="blocked", reason="field_mask_missing_nextPageToken")

    seen_ids: set[str] = set()
    seen_tokens: set[str] = set()
    rows: list[PlaceCandidate] = []
    next_token = ""
    requests = pages = 0

    while True:
        if pages >= config.max_pages or requests >= config.request_budget:
            return PlacesTextResult(
                status="truncated",
                rows=rows,
                reason="page_or_request_budget_reached",
                requests=requests,
                pages=pages,
                next_page_token=next_token,
            )
        if next_token:
            if next_token in seen_tokens:
                return PlacesTextResult(
                    status="error",
                    rows=rows,
                    reason="next_page_token_loop",
                    requests=requests,
                    pages=pages,
                    next_page_token=next_token,
                )
            seen_tokens.add(next_token)

        resp = transport("POST", ENDPOINT, headers(config), request_body(config, next_token), config.timeout)
        requests += 1
        code = int(getattr(resp, "status_code", 0) or 0)
        if code in (401, 403):
            return PlacesTextResult(status="blocked", rows=rows, reason=f"auth_or_policy_{code}", requests=requests, pages=pages)
        if code == 429:
            return PlacesTextResult(status="blocked", rows=rows, reason="rate_or_quota_429", requests=requests, pages=pages)
        if code < 200 or code >= 300:
            return PlacesTextResult(status="error", rows=rows, reason=f"http_{code}", requests=requests, pages=pages)

        data = _json(resp)
        pages += 1
        for place in data.get("places") or []:
            candidate = _candidate(place)
            if not candidate.place_id or candidate.place_id in seen_ids:
                continue
            seen_ids.add(candidate.place_id)
            rows.append(candidate)

        next_token = str(data.get("nextPageToken") or "")
        if not next_token:
            return PlacesTextResult(status="complete", rows=rows, requests=requests, pages=pages)
