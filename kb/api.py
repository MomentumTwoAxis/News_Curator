"""앤트위키 공개 REST API 클라이언트 (읽기 전용, 인증 불필요)."""
from __future__ import annotations

import time
from typing import Any
from urllib.parse import quote

import httpx

BASE_URL = "https://api.ant.wiki/api/v1"
WEB_URL = "https://www.ant.wiki"
USER_AGENT = "antwiki-news-curator/0.1 (+kb builder)"


class AntWikiAPI:
    def __init__(self, base_url: str = BASE_URL, timeout: float = 30.0, max_retries: int = 3):
        self.base_url = base_url.rstrip("/")
        self.max_retries = max_retries
        self._client = httpx.Client(
            timeout=timeout,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            follow_redirects=True,
        )

    def close(self) -> None:
        self._client.close()

    def get(self, path: str, **params: Any) -> Any:
        url = f"{self.base_url}/{path.lstrip('/')}"
        last_exc: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                r = self._client.get(url, params={k: v for k, v in params.items() if v is not None})
                if r.status_code == 429 or r.status_code >= 500:
                    raise httpx.HTTPStatusError(f"{r.status_code}", request=r.request, response=r)
                r.raise_for_status()
                return r.json()
            except (httpx.HTTPStatusError, httpx.TransportError) as exc:
                last_exc = exc
                status = getattr(getattr(exc, "response", None), "status_code", None)
                if status is not None and 400 <= status < 500 and status != 429:
                    raise
                time.sleep(1.5 * (attempt + 1))
        assert last_exc is not None
        raise last_exc

    # ── 종목 / 위키 ────────────────────────────────────────────
    def stock_names(self) -> dict[str, str]:
        return self.get("market/stock-names")

    def wiki_list(self, limit: int = 100, offset: int = 0, order_by: str = "name", desc: bool = False) -> dict:
        """order_by=view_count 는 페이지 간 순서가 흔들려 중복/누락이 생긴다 → 기본은 name 오름차순(2,670건 안정)."""
        return self.get("wiki/list", limit=limit, offset=offset, order_by=order_by, desc=str(desc).lower())

    def wiki_list_all(self) -> list[dict]:
        items: list[dict] = []
        offset = 0
        while True:
            page = self.wiki_list(limit=100, offset=offset)
            items.extend(page.get("items", []))
            offset += 100
            if offset >= page.get("total", 0) or not page.get("items"):
                return items

    def wiki_full(self, stock_id: str, narrative_limit: int = 6) -> dict:
        """프로필 + infobox + 위키 본문 + 내러티브를 한 번에."""
        return self.get(f"wiki/{stock_id}", narrative_limit=narrative_limit)

    def wiki_content(self, stock_id: str) -> dict:
        return self.get(f"wiki/{stock_id}/content")

    def wiki_sections(self, stock_id: str) -> dict:
        """섹션 메타만(≈2KB). 본문 존재 여부·revision_number 확인용 경량 프로브."""
        return self.get(f"wiki/{stock_id}/sections")

    def one_liner(self, stock_id: str) -> dict | None:
        try:
            return self.get(f"one-liner/stocks/{stock_id}")
        except httpx.HTTPStatusError:
            return None

    # ── 테마 ────────────────────────────────────────────────
    def theme_wiki_index(self) -> dict:
        return self.get("theme-page/wiki-index")

    def theme_hub(self) -> dict:
        return self.get("theme-page/hub")

    def theme_category(self, category_slug: str) -> dict:
        return self.get(f"theme-page/categories/{quote(category_slug, safe='')}")

    def theme_detail(self, category_slug: str, theme_slug: str) -> dict:
        return self.get(
            f"theme-page/categories/{quote(category_slug, safe='')}/themes/{quote(theme_slug, safe='')}"
        )

    def stock_themes(self, stock_id: str, limit: int = 30) -> dict:
        return self.get(f"theme-page/stocks/{stock_id}/themes", limit=limit)

    def hot_themes(self, limit: int = 10) -> dict:
        return self.get("theme-page/hot-themes", limit=limit, include_stocks=True)


def wiki_url(name: str, stock_id: str) -> str:
    return f"{WEB_URL}/wiki/{quote(name + '-' + stock_id, safe='')}"


def theme_url(category_slug: str, theme_slug: str) -> str:
    return f"{WEB_URL}/themes/{quote(category_slug, safe='')}/{quote(theme_slug, safe='')}"
