"""A small Python client for the ABP dashboard API (roadmap P5).

    from abp_sdk import AbpClient
    abp = AbpClient("http://127.0.0.1:8765", token="...")          # the dashboard token or a paired device's key
    abp.status()
    abp.model_info("openrouter", "qwen/qwen3.8-27b:free")
    abp.call("GET /api/agent/permissions")                           # any operation, by "METHOD /path" or its operationId
    abp.call("GET /api/export/{table}", path={"table": "jobs"})

The client is driven by the server's own OpenAPI document (`GET /openapi.json`, also kept in
docs/api/openapi.json), so every one of the API's operations can be called with `call()` without a
generated method for each; the named methods below are conveniences for the common ones. Only what
the API returns is returned: nothing is cached and nothing is retried.
"""
from __future__ import annotations

from typing import Any, Optional

import httpx

__all__ = ["AbpClient", "AbpError"]


class AbpError(Exception):
    def __init__(self, status: int, detail: Any):
        super().__init__(f"{status}: {detail}")
        self.status, self.detail = status, detail


class AbpClient:
    def __init__(self, base_url: str = "http://127.0.0.1:8765", token: str = "", *, timeout: float = 30.0,
                 http: Optional[httpx.Client] = None):
        self.base_url = base_url.rstrip("/")
        self._http = http or httpx.Client(timeout=timeout)
        self._headers = {"X-Dashboard-Token": token} if token else {}
        self._ops: Optional[dict[str, dict]] = None

    # ---- raw ----------------------------------------------------------------------------------
    def request(self, method: str, path: str, *, params: Optional[dict] = None, json: Any = None) -> Any:
        clean = {k: v for k, v in (params or {}).items() if v is not None}
        r = self._http.request(method.upper(), f"{self.base_url}{path}", params=clean, json=json, headers=self._headers)
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail", r.text)
            except (ValueError, AttributeError):
                detail = r.text
            raise AbpError(r.status_code, detail)
        if not r.content:
            return None
        return r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text

    # ---- by operationId -------------------------------------------------------------------------
    def operations(self) -> dict[str, dict]:
        """operationId (and "METHOD /path") -> {method, path, path_params, query_params, has_body, summary}."""
        if self._ops is None:
            spec = self.request("GET", "/openapi.json")
            ops: dict[str, dict] = {}
            for path, item in (spec.get("paths") or {}).items():
                for method, op in item.items():
                    if method not in ("get", "post", "put", "delete", "patch") or "operationId" not in op:
                        continue
                    params = op.get("parameters") or []
                    info = {
                        "method": method, "path": path, "summary": op.get("summary", ""), "has_body": "requestBody" in op,
                        "path_params": [p["name"] for p in params if p.get("in") == "path"],
                        "query_params": [p["name"] for p in params if p.get("in") == "query"]}
                    ops[op["operationId"]] = info
                    ops[f"{method.upper()} {path}"] = info
            self._ops = ops
        return self._ops

    def call(self, operation_id: str, *, path: Optional[dict] = None, query: Optional[dict] = None, body: Any = None) -> Any:
        op = self.operations().get(operation_id)
        if op is None:
            raise KeyError(f"no operation {operation_id!r} (see .operations())")
        url = op["path"]
        for name in op["path_params"]:
            if name not in (path or {}):
                raise KeyError(f"{operation_id} needs path parameter {name!r}")
            url = url.replace("{" + name + "}", str(path[name]))
            url = url.replace("{" + name + ":path}", str(path[name]))
        return self.request(op["method"], url, params=query, json=body)

    # ---- conveniences ------------------------------------------------------------------------------
    def status(self) -> Any:
        """The overview: job counts, desktop state, config version, default backend."""
        return self.request("GET", "/api/overview")

    def model_info(self, provider: str, model: str) -> dict:
        """Everything known about a model, and how much of its allowance is left."""
        return self.request("GET", "/api/models/info", params={"provider": provider, "model": model})

    def model_usage(self, days: int = 1) -> dict:
        return self.request("GET", "/api/models/usage", params={"days": days})

    def find_models(self, *, provider: str = "", query: str = "", free_only: bool = False, min_context: int = 0,
                    needs: str = "", limit: int = 20) -> list[dict]:
        return self.request("GET", "/api/models/find", params={
            "provider": provider, "query": query, "free_only": free_only, "min_context": min_context,
            "needs": needs, "limit": limit})["models"]

    def set_model_limits(self, key: str, **limits: Any) -> dict:
        """Your own limits for a model or a pattern, e.g. set_model_limits("openrouter/*:free", rpm=20, rpd=1000)."""
        return self.request("PUT", "/api/models/limits", json={"key": key, **limits})

    def permissions(self) -> dict:
        return self.request("GET", "/api/agent/permissions")

    def export_session(self, session_key: str, fmt: str = "md") -> str:
        """A conversation as Markdown or JSON text (secrets removed). Needs the dashboard token itself."""
        return self.request("GET", f"/api/agent/sessions/{session_key}/export", params={"format": fmt})

    def skills_quarantine(self) -> list[dict]:
        return self.request("GET", "/api/skills/quarantine")["packs"]

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "AbpClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
