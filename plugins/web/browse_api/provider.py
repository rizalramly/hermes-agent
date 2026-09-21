"""browse-API — self-hosted web search + direct URL extract (plugin form).

Search bridges to a self-hosted `browse-API` (Serper-shaped, SearXNG); extract
fetches a URL directly with httpx and the **corporate CA bundle** so it works
through the Zscaler TLS-intercepting proxy (the Python default certifi bundle
lacks the Zscaler root, so external HTTPS otherwise fails cert verification).

Config::

    web:
      search_backend: browse-api      # search via the browse-API service
      extract_backend: browse-api      # fetch a URL's readable text directly

Env::

    BROWSE_API_URL=http://127.0.0.1:8000   # browse-API service (search)
    BROWSE_API_KEY=...                      # its X-API-KEY
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt   # CA bundle incl. Zscaler
"""
from __future__ import annotations

import html as _html
import logging
import os
import re
from typing import Any, Dict, List

from agent.web_search_provider import WebSearchProvider, get_provider_env

logger = logging.getLogger(__name__)

# CA bundle that includes the corporate (Zscaler) root — the host's system bundle
# has it; certifi does not. Honour SSL_CERT_FILE if set, else the system bundle.
_CA_BUNDLE = os.getenv("SSL_CERT_FILE") or "/etc/ssl/certs/ca-certificates.crt"
_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
       "Chrome/124.0 Safari/537.36")


def _verify():
    return _CA_BUNDLE if os.path.exists(_CA_BUNDLE) else True


_PDF_MAX_PAGES = int(os.getenv("BROWSE_API_PDF_MAX_PAGES", "60"))
_TEXT_TYPES = ("text/", "application/xhtml", "application/xml", "application/json")


def _pdf_to_text(data: bytes, limit: int) -> str:
    """PDF bytes -> layout-preserving text via poppler's ``pdftotext``.

    Annual reports and analyst briefings are PDFs; without this the agent got
    raw ``%PDF-1.7 … obj`` bytes and could not read a single figure.
    ``-layout`` keeps table columns aligned so financial tables stay readable.
    """
    import shutil
    import subprocess
    import tempfile

    if not shutil.which("pdftotext"):
        return "[PDF: text extraction unavailable on this host (pdftotext missing)]"
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=True) as tmp:
        tmp.write(data)
        tmp.flush()
        try:
            proc = subprocess.run(
                ["pdftotext", "-layout", "-l", str(_PDF_MAX_PAGES), tmp.name, "-"],
                capture_output=True, text=True, timeout=45,
            )
        except subprocess.TimeoutExpired:
            return "[PDF: text extraction timed out]"
    text = proc.stdout if proc.returncode == 0 else ""
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text:
        return "[PDF: no extractable text (scanned image?)]"
    return text[:limit]


def _html_to_text(raw: str, limit: int = 20000) -> str:
    raw = re.sub(r"(?is)<(script|style|noscript|template)[^>]*>.*?</\1>", " ", raw)
    raw = re.sub(r"(?is)<br\s*/?>", "\n", raw)
    raw = re.sub(r"(?is)</(p|div|li|tr|h[1-6])>", "\n", raw)
    text = re.sub(r"(?s)<[^>]+>", " ", raw)
    text = _html.unescape(text)
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()[:limit]


class BrowseApiWebSearchProvider(WebSearchProvider):
    """Search via browse-API; extract via direct httpx fetch (Zscaler-aware CA)."""

    @property
    def name(self) -> str:
        return "browse-api"

    @property
    def display_name(self) -> str:
        return "browse-API (self-hosted)"

    def _base_url(self) -> str:
        return (get_provider_env("BROWSE_API_URL") or "").rstrip("/")

    def is_available(self) -> bool:
        # Extract works with just the CA bundle; search also needs the service.
        return bool(os.path.exists(_CA_BUNDLE) or (self._base_url() and get_provider_env("BROWSE_API_KEY")))

    def supports_search(self) -> bool:
        return True

    def supports_extract(self) -> bool:
        return True

    # ── search (browse-API service) ──────────────────────────────────────────
    def search(self, query: str, limit: int = 5) -> Dict[str, Any]:
        import httpx

        base_url = self._base_url()
        api_key = get_provider_env("BROWSE_API_KEY")
        if not base_url or not api_key:
            return {"success": False, "error": "BROWSE_API_URL / BROWSE_API_KEY not set"}
        num = max(1, min(int(limit), 20))
        try:
            resp = httpx.post(f"{base_url}/search", json={"q": query, "num": num},
                              headers={"X-API-KEY": api_key, "Accept": "application/json"},
                              timeout=25)
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 403:
                return {"success": False, "error": "browse-API policy blocked this query (403)"}
            return {"success": False, "error": f"browse-API HTTP {exc.response.status_code}"}
        except httpx.RequestError as exc:
            return {"success": False, "error": f"Could not reach browse-API at {base_url}: {exc}"}
        organic = (resp.json() or {}).get("organic") or []
        web = [{"title": str(r.get("title", "")), "url": str(r.get("link", "") or r.get("url", "")),
                "description": str(r.get("snippet", "") or r.get("description", "")), "position": i + 1}
               for i, r in enumerate(organic[:limit])]
        return {"success": True, "data": {"web": web}}

    # ── extract (direct fetch, Zscaler-aware CA) ─────────────────────────────
    def extract(self, urls: List[str], **kwargs: Any) -> List[Dict[str, Any]]:
        """Fetch each URL and return readable text.

        Returns [{"url","title","content","success"} | {"url","error","success":False}].
        """
        import httpx

        if isinstance(urls, str):
            urls = [urls]
        out: List[Dict[str, Any]] = []
        for url in urls:
            try:
                r = httpx.get(url, timeout=40, verify=_verify(), follow_redirects=True,
                              headers={"User-Agent": _UA,
                                       "Accept": "text/html,application/xhtml+xml,application/pdf;q=0.9,*/*;q=0.5"})
                r.raise_for_status()
                ctype = (r.headers.get("content-type") or "").split(";")[0].strip().lower()
                body = r.content
                if ctype == "application/pdf" or body[:5] == b"%PDF-":
                    title = url.rsplit("/", 1)[-1] or url
                    content = _pdf_to_text(body, 30000)
                    kind = "pdf"
                elif not ctype or ctype.startswith(_TEXT_TYPES):
                    m = re.search(r"(?is)<title[^>]*>(.*?)</title>", r.text)
                    title = _html.unescape(re.sub(r"\s+", " ", m.group(1)).strip()) if m else url
                    content = _html_to_text(r.text)
                    kind = "html"
                else:
                    # Images, archives, office files… — say what it is instead of
                    # dumping binary into the model's context.
                    title = url.rsplit("/", 1)[-1] or url
                    content = f"[{ctype or 'binary'} file, {len(body)} bytes — not text; nothing to read]"
                    kind = ctype
                out.append({"url": url, "title": title, "content": content, "success": True})
                logger.info("browse-api extract %s (%s) -> %d chars", url, kind, len(content))
            except Exception as exc:  # noqa: BLE001
                logger.warning("browse-api extract %s failed: %s", url, exc)
                out.append({"url": url, "error": str(exc)[:200], "success": False})
        return out

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": "browse-API (self-hosted)",
            "badge": "self-hosted",
            "tag": "On-prem search (SearXNG) + direct URL extract (Zscaler-aware CA).",
            "env_vars": [
                {"key": "BROWSE_API_URL", "prompt": "browse-API base URL (e.g. http://127.0.0.1:8000)"},
                {"key": "BROWSE_API_KEY", "prompt": "browse-API X-API-KEY"},
            ],
        }
