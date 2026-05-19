#!/usr/bin/env python3
"""nomnom MCP server — exposes the restaurant DB to MCP clients via stdio.

No external deps. Pure stdlib. Run as:
    uv run python scripts/mcp_server.py

Then wire into Claude Code via .claude/mcp.json.
"""
from __future__ import annotations

import json
import sys
import os
from pathlib import Path
from typing import Any

# Ensure scripts/ is on path so we can import db, query, etc.
SCRIPT_DIR = Path(__file__).parent.resolve()
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import db  # noqa: E402
import geocode  # noqa: E402

DB_PATH_ENV = os.environ.get("NOMNOM_DB_PATH")
if DB_PATH_ENV:
    db.DB_PATH = Path(DB_PATH_ENV)


def _mcp_error(code: int, message: str, req_id: Any = None) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def _mcp_result(result: Any, req_id: Any) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _send(obj: dict) -> None:
    payload = json.dumps(obj) + "\n"
    sys.stdout.write(payload)
    sys.stdout.flush()


TOOLS = [
    {
        "name": "nomnom_search",
        "description": "Search restaurants near a location (free-form address or lat/lng).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "near": {"type": "string", "description": "Free-form place, e.g. 'Bologna, Italy' or 'Rome'"},
                "lat": {"type": "number", "description": "Latitude (alternative to 'near')"},
                "lng": {"type": "number", "description": "Longitude (alternative to 'near')"},
                "radius_km": {"type": "number", "default": 10, "description": "Search radius in kilometres"},
                "category": {
                    "type": "string",
                    "enum": ["restaurant", "bar", "wine_shop", "shop"],
                    "description": "Optional category filter"
                },
                "sources": {
                    "type": "string",
                    "description": "Comma-separated source whitelist, e.g. 'michelin,splendido'"
                },
                "cuisine": {"type": "string", "description": "Substring match on cuisine field"},
                "keyword": {"type": "string", "description": "Free-text keyword search across name/description/tags"},
                "limit": {"type": "number", "default": 20, "description": "Max results to return"}
            },
            "required": [],
        }
    },
    {
        "name": "nomnom_status",
        "description": "Show sync status and row counts per source.",
        "inputSchema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "nomnom_get_place",
        "description": "Fetch full details for a specific place by ID.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "number", "description": "Place database ID"}
            },
            "required": ["id"]
        }
    }
]


def cmd_status() -> dict:
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM sync_log ORDER BY source").fetchall()
        counts = conn.execute(
            "SELECT source, COUNT(*) c FROM places GROUP BY source ORDER BY source"
        ).fetchall()

    sources = []
    for r in rows:
        sources.append({
            "source": r["source"],
            "last_run_at": r["last_run_at"],
            "status": r["last_status"],
            "rows_added": r["rows_added"],
            "rows_updated": r["rows_updated"],
            "message": r["last_message"]
        })
    totals = {c["source"]: c["c"] for c in counts}
    return {"sources": sources, "totals": totals}


def cmd_search(args: dict) -> dict:
    near = args.get("near")
    lat = args.get("lat")
    lng = args.get("lng")
    radius_km = args.get("radius_km", 10)
    category = args.get("category")
    sources_str = args.get("sources")
    cuisine = args.get("cuisine")
    keyword = args.get("keyword")
    limit = args.get("limit", 20)

    if near:
        loc = geocode.geocode(near)
        if not loc:
            return {"error": f"Could not geocode: {near!r}"}
        lat, lng = loc["lat"], loc["lng"]
    elif lat is None or lng is None:
        return {"error": "Provide either 'near' or both 'lat' and 'lng'."}

    sources = None
    if sources_str:
        sources = [s.strip() for s in sources_str.split(",") if s.strip()]

    with db.connect() as conn:
        results = db.search_near(
            conn,
            lat=lat,
            lng=lng,
            radius_km=radius_km,
            category=category,
            sources=sources,
            cuisine=cuisine,
            keyword=keyword,
            limit=limit,
        )

    return {"results": results, "location": {"lat": lat, "lng": lng}}


def cmd_get_place(args: dict) -> dict:
    place_id = args.get("id")
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM places WHERE id = ?", (place_id,)).fetchone()
        if not row:
            return {"error": f"Place {place_id} not found."}
        return {"place": dict(row)}


def handle_tool_call(name: str, arguments: dict) -> dict:
    if name == "nomnom_status":
        return {"content": [{"type": "text", "text": json.dumps(cmd_status(), default=str, indent=2)}]}
    elif name == "nomnom_search":
        res = cmd_search(arguments)
        if "error" in res:
            return {"content": [{"type": "text", "text": res["error"]}], "isError": True}
        return {"content": [{"type": "text", "text": json.dumps(res, default=str, indent=2)}]}
    elif name == "nomnom_get_place":
        res = cmd_get_place(arguments)
        if "error" in res:
            return {"content": [{"type": "text", "text": res["error"]}], "isError": True}
        return {"content": [{"type": "text", "text": json.dumps(res, default=str, indent=2)}]}
    else:
        return {"content": [{"type": "text", "text": f"Unknown tool: {name}"}], "isError": True}


def run() -> None:
    for line in sys.stdin:
        line = line.rstrip("\n\r")
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            _send(_mcp_error(-32700, "Parse error"))
            continue

        req_id = req.get("id")
        method = req.get("method")

        if method == "initialize":
            _send(_mcp_result({
                "protocolVersion": "2024-11-05",
                "serverInfo": {"name": "nomnom", "version": "0.1.0"},
                "capabilities": {"tools": {}, "resources": {}, "prompts": {}}
            }, req_id))

        elif method == "notifications/initialized":
            pass  # no response needed

        elif method == "tools/list":
            _send(_mcp_result({"tools": TOOLS}, req_id))

        elif method == "tools/call":
            params = req.get("params", {})
            result = handle_tool_call(params.get("name"), params.get("arguments", {}))
            _send(_mcp_result(result, req_id))

        elif method is not None:
            # unknown method
            _send(_mcp_error(-32601, f"Method not found: {method}", req_id))
        else:
            pass


if __name__ == "__main__":
    run()
