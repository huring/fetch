"""Shared HTTP helpers for source adapters: retry/backoff and a common error type."""
from __future__ import annotations

import json
import re
from typing import Any, Dict, Optional

import requests
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential


class SourceError(Exception):
    """Raised when a source fails to return usable results after retries."""


@retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=0.5, min=0.5, max=4),
    retry=retry_if_exception_type(requests.RequestException),
)
def get_json(
    url: str,
    params: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
    timeout: int = 15,
) -> Any:
    response = requests.get(url, params=params, headers=headers, timeout=timeout)
    response.raise_for_status()
    return response.json()


@retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=0.5, min=0.5, max=4),
    retry=retry_if_exception_type(requests.RequestException),
)
def get_text(
    url: str,
    params: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
    timeout: int = 15,
) -> str:
    response = requests.get(url, params=params, headers=headers, timeout=timeout)
    response.raise_for_status()
    return response.text


_JSONLD_RE = re.compile(r'<script type="application/ld\+json"[^>]*>(.*?)</script>', re.S)


def extract_jsonld_description(html: str) -> str:
    """Best-effort extraction of the "description" field from an embedded
    schema.org JSON-LD block (``<script type="application/ld+json">``), the
    mechanism both Blocket's and Vinted's item detail pages use to carry a
    textual description. Returns "" if the block is missing or malformed -
    never raises, since a failed enrichment shouldn't block scoring, it just
    falls back to title-only for that listing."""
    match = _JSONLD_RE.search(html)
    if not match:
        return ""
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        return ""
    return data.get("description") or ""
