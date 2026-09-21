"""browse-API web-search plugin — bundled, auto-loaded.

Mirrors plugins/web/brave_free/: provider.py holds the class,
__init__.py::register(ctx) registers an instance.
"""
from __future__ import annotations

from plugins.web.browse_api.provider import BrowseApiWebSearchProvider


def register(ctx) -> None:
    ctx.register_web_search_provider(BrowseApiWebSearchProvider())
