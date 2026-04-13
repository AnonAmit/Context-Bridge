"""CDP Extractor for live IDE memory extraction.
Uses Chrome DevTools Protocol to bypass proprietary encryption on restricted target platforms like Antigravity.
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.request
import urllib.error
from typing import Any
from datetime import datetime

try:
    import websockets
    from bs4 import BeautifulSoup
except ImportError:
    websockets = None
    BeautifulSoup = None

from context_bridge.models import BridgeSession

def _get_target_ws() -> str | None:
    try:
        req = urllib.request.Request("http://localhost:9222/json")
        with urllib.request.urlopen(req, timeout=2) as response:
            data = json.loads(response.read().decode("utf-8"))
    except Exception:
        return None

    valid = [t for t in data if t.get("type") in ("page", "app", "webview") and "webSocketDebuggerUrl" in t]
    for target in valid:
        if "Antigravity" in target.get("title", "") or "Visual Studio Code" in target.get("title", ""):
            return target["webSocketDebuggerUrl"]
    return valid[0]["webSocketDebuggerUrl"] if valid else None


async def _extract_dom_via_ws(ws_url: str) -> str | None:
    payload = {
        "id": 1,
        "method": "Runtime.evaluate",
        "params": {"expression": "document.documentElement.outerHTML", "returnByValue": True}
    }
    try:
        async with websockets.connect(ws_url, max_size=None) as ws:
            await ws.send(json.dumps(payload))
            while True:
                response = json.loads(await ws.recv())
                if response.get("id") == 1:
                    result = response.get("result", {}).get("result", {})
                    if result.get("type") == "string":
                        return result.get("value")
                    return None
    except Exception:
        return None


def extract_live_session() -> BridgeSession | None:
    """Executes the CDP websocket hook and maps the raw DOM into a BridgeSession."""
    if not websockets or not BeautifulSoup:
        raise RuntimeError("Missing 'websockets' or 'beautifulsoup4' dependencies for CDP.")

    ws_url = _get_target_ws()
    if not ws_url:
        return None

    dom_content = asyncio.run(_extract_dom_via_ws(ws_url))
    if not dom_content:
        return None

    # Parse and structure the DOM output
    soup = BeautifulSoup(dom_content, "html.parser")
    content_tags = soup.find_all(['h1', 'h2', 'h3', 'h4', 'p', 'li', 'pre', 'code'])
    
    seen = set()
    cleaned = []
    
    for tag in content_tags:
        text = tag.get_text(separator=' ', strip=True)
        if len(text) < 10 or text in seen:
            continue
        seen.add(text)
        
        if tag.name.startswith("h"):
            cleaned.append(f"\n## {text}\n")
        elif tag.name in ["pre"]:
            cleaned.append(f"\n```\n{text}\n```\n")
        elif tag.name == "li":
            cleaned.append(f"- {text}")
        else:
            cleaned.append(f"{text}\n")
            
    markdown_messages = "\n".join(cleaned)
    if not markdown_messages.strip():
        markdown_messages = "<No valid chat structures identified in the IDE DOM>"

    return BridgeSession(
        id=f"cdp-{datetime.now().strftime('%Y%m%d%H%M%S')}",
        title="Live CDP Assumed Context",
        date=datetime.now().isoformat(),
        source_ide="antigravity-cdp",
        project_root="Unknown (Live Heap)",
        messages=markdown_messages,
        model="CDP Memory Extraction",
    )
