"""Small authenticated TMDB/TVDB clients for optional metadata preflight."""

from __future__ import annotations

import json
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

from internal.errors import WorkflowError


TMDB_BASE = "https://api.themoviedb.org/3"
TVDB_BASE = "https://api4.thetvdb.com/v4"
TRANSIENT_CODES = {429, 500, 502, 503, 504}


class MetadataHttpError(WorkflowError):
    def __init__(self, code: str, message: str, *, transient: bool = False) -> None:
        super().__init__(code, message)
        self.transient = transient


def credential_presence() -> dict[str, bool]:
    """Return booleans only; secrets must never enter manifests or logs."""

    return {
        "tmdb": bool(os.environ.get("ARCHIVE_TMDB_TOKEN") or os.environ.get("ARCHIVE_TMDB_API_KEY")),
        "tvdb": bool(os.environ.get("ARCHIVE_TVDB_API_KEY")),
        "tvdbPin": bool(os.environ.get("ARCHIVE_TVDB_PIN")),
    }


class JsonHttpClient:
    def __init__(
        self,
        *,
        proxy: str | None,
        timeout: float = 10.0,
        retries: int = 2,
        opener: Any | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        handlers = [urllib.request.ProxyHandler({"http": proxy, "https": proxy})] if proxy else [urllib.request.ProxyHandler({})]
        self.opener = opener or urllib.request.build_opener(*handlers)
        self.timeout = timeout
        self.retries = retries
        self.sleeper = sleeper

    def request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        payload: dict[str, Any] | None = None,
        auth_code: str,
    ) -> dict[str, Any]:
        query = urllib.parse.urlencode({key: value for key, value in (params or {}).items() if value is not None})
        selected_url = f"{url}?{query}" if query else url
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        request_headers = {"Accept": "application/json", "User-Agent": "archive-plex-anime/metadata"}
        request_headers.update(headers or {})
        if body is not None:
            request_headers["Content-Type"] = "application/json"
        request = urllib.request.Request(selected_url, data=body, headers=request_headers, method=method)
        for attempt in range(self.retries + 1):
            try:
                with self.opener.open(request, timeout=self.timeout) as response:
                    raw = response.read()
                value = json.loads(raw.decode("utf-8-sig"))
                if not isinstance(value, dict):
                    raise MetadataHttpError("METADATA_RESPONSE_INVALID", "Metadata API returned a non-object response")
                return value
            except urllib.error.HTTPError as exc:
                if exc.code in {401, 403}:
                    raise MetadataHttpError(auth_code, "Metadata API rejected the configured credential") from exc
                transient = exc.code in TRANSIENT_CODES
                if transient and attempt < self.retries:
                    retry_after = exc.headers.get("Retry-After") if exc.headers else None
                    try:
                        delay = min(float(retry_after), 5.0) if retry_after else 0.25 * (2**attempt)
                    except ValueError:
                        delay = 0.25 * (2**attempt)
                    self.sleeper(delay)
                    continue
                raise MetadataHttpError(
                    "METADATA_HTTP_TRANSIENT" if transient else "METADATA_HTTP_FAILED",
                    f"Metadata API request failed with HTTP {exc.code}",
                    transient=transient,
                ) from exc
            except (urllib.error.URLError, TimeoutError, socket.timeout, OSError) as exc:
                if attempt < self.retries:
                    self.sleeper(0.25 * (2**attempt))
                    continue
                raise MetadataHttpError("METADATA_NETWORK_UNAVAILABLE", "Metadata API is unavailable through the configured proxy", transient=True) from exc
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise MetadataHttpError("METADATA_RESPONSE_INVALID", "Metadata API returned invalid UTF-8 JSON") from exc
        raise AssertionError("unreachable")


class TmdbClient:
    def __init__(self, http: JsonHttpClient) -> None:
        self.http = http
        self.token = os.environ.get("ARCHIVE_TMDB_TOKEN", "").strip()
        self.api_key = os.environ.get("ARCHIVE_TMDB_API_KEY", "").strip()
        if not self.token and not self.api_key:
            raise MetadataHttpError("TMDB_CREDENTIAL_REQUIRED", "TMDB credential is not configured")

    def _request(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        query = dict(params or {})
        if not self.token:
            query["api_key"] = self.api_key
        return self.http.request("GET", f"{TMDB_BASE}{path}", params=query, headers=headers, auth_code="TMDB_AUTH_FAILED")

    def search(self, media_type: str, query: str, *, language: str, year: int | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"query": query, "language": language, "include_adult": "false"}
        if year:
            params["first_air_date_year" if media_type == "tv" else "year"] = year
        value = self._request(f"/search/{media_type}", params)
        return [item for item in value.get("results", []) if isinstance(item, dict)]

    def details(self, media_type: str, metadata_id: int, *, language: str) -> dict[str, Any]:
        return self._request(f"/{media_type}/{metadata_id}", {"language": language, "append_to_response": "external_ids,alternative_titles"})

    def season(self, series_id: int, season_number: int, *, language: str) -> dict[str, Any]:
        return self._request(f"/tv/{series_id}/season/{season_number}", {"language": language})


class TvdbClient:
    def __init__(self, http: JsonHttpClient) -> None:
        self.http = http
        self.api_key = os.environ.get("ARCHIVE_TVDB_API_KEY", "").strip()
        self.pin = os.environ.get("ARCHIVE_TVDB_PIN", "").strip()
        self.token = ""
        if not self.api_key:
            raise MetadataHttpError("TVDB_CREDENTIAL_REQUIRED", "TVDB credential is not configured")

    def _token(self) -> str:
        if not self.token:
            payload = {"apikey": self.api_key}
            if self.pin:
                payload["pin"] = self.pin
            value = self.http.request("POST", f"{TVDB_BASE}/login", payload=payload, auth_code="TVDB_AUTH_FAILED")
            self.token = str(value.get("data", {}).get("token") or "")
            if not self.token:
                raise MetadataHttpError("TVDB_AUTH_FAILED", "TVDB login did not return a token")
        return self.token

    def _request(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.http.request(
            "GET", f"{TVDB_BASE}{path}", params=params,
            headers={"Authorization": f"Bearer {self._token()}"}, auth_code="TVDB_AUTH_FAILED",
        )

    def search(self, query: str, media_type: str, *, year: int | None = None) -> list[dict[str, Any]]:
        value = self._request("/search", {"query": query, "type": "series" if media_type == "tv" else "movie", "year": year})
        return [item for item in value.get("data", []) if isinstance(item, dict)]

    def series(self, series_id: int) -> dict[str, Any]:
        return self._request(f"/series/{series_id}/extended", {"meta": "translations", "short": "true"}).get("data", {})

    def movie(self, movie_id: int) -> dict[str, Any]:
        return self._request(f"/movies/{movie_id}/extended", {"meta": "translations", "short": "true"}).get("data", {})

    def episodes(self, series_id: int, order: str) -> list[dict[str, Any]]:
        episodes: list[dict[str, Any]] = []
        for page in range(20):
            value = self._request(f"/series/{series_id}/episodes/{order}", {"page": page})
            data = value.get("data", {})
            current = data.get("episodes", data if isinstance(data, list) else [])
            episodes.extend(item for item in current if isinstance(item, dict))
            links = value.get("links", {}) if isinstance(value.get("links"), dict) else {}
            if not links.get("next"):
                break
        return episodes
