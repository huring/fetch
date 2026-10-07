"""Shared HTTP helpers for source adapters: retry/backoff and a common error type."""
from __future__ import annotations

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
    headers: Optional[Dict[str, str]] = None,
    timeout: int = 15,
) -> str:
    response = requests.get(url, headers=headers, timeout=timeout)
    response.raise_for_status()
    return response.text
