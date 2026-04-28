"""
Risk hypothesis sandbox — executes LLM-generated Python against tenant data.

The CRM (Phase D LangGraph workflow) POSTs a JSON body of:

  {
    "hypothesis_slug": "usage-drop-7d",
    "code": "<python source defining a function `evaluate(loans, vehicle_states, daily_km, can_history) -> dict`>",
    "data": {
      "loans":          [...],   # tenant's loan slice
      "vehicle_states": [...],   # IoT vehicle_state rows
      "daily_km":       [...],   # daily_distance_per_vehicle rows
      "can_history":    [...]    # optional 24h CAN extracts
    }
  }

We exec() the code in a restricted namespace (no builtins exposed except a
small allowlist), call evaluate() with the data, and return the JSON-able
result. 30s wallclock cap. The container has no network and a read-only fs;
escape from this Python sandbox just gets you a useless box.

NOT considered safe against a determined adversary — it's defense in depth
for content the LLM produces. Real isolation lives at the docker-compose
layer (network: none, read_only: true, cap_drop: ALL, no_new_privileges).
"""
from __future__ import annotations

import json
import logging
import signal
import sys
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
import uvicorn

# pandas / numpy are imported here so the sandboxed code can reach them via
# the namespace we hand it.
import numpy as np
import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("sandbox")

EXEC_TIMEOUT_SEC = 30
MAX_INPUT_BYTES = 5 * 1024 * 1024  # 5 MB ceiling on the data payload

# Builtins we expose to executed code. Keeps `exec`, `eval`, `open`, `import`
# out of the runtime namespace by default.
SAFE_BUILTINS = {
    "abs": abs, "all": all, "any": any, "bool": bool, "dict": dict,
    "enumerate": enumerate, "filter": filter, "float": float, "int": int,
    "len": len, "list": list, "map": map, "max": max, "min": min,
    "print": print, "range": range, "round": round, "set": set,
    "sorted": sorted, "str": str, "sum": sum, "tuple": tuple, "zip": zip,
    "isinstance": isinstance, "type": type,
    "True": True, "False": False, "None": None,
}


class ExecuteRequest(BaseModel):
    hypothesis_slug: str = Field(..., max_length=128)
    code: str = Field(..., max_length=50_000)
    data: dict[str, Any] = Field(default_factory=dict)


class ExecuteResponse(BaseModel):
    ok: bool
    result: dict[str, Any] | None = None
    error: str | None = None
    elapsed_ms: int


app = FastAPI(title="risk-sandbox", version="0.1")


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.post("/execute", response_model=ExecuteResponse)
def execute(req: ExecuteRequest):
    import time
    started = time.perf_counter()

    payload_size = len(json.dumps(req.data).encode())
    if payload_size > MAX_INPUT_BYTES:
        raise HTTPException(status_code=413, detail=f"data payload too large: {payload_size} > {MAX_INPUT_BYTES}")

    namespace: dict[str, Any] = {
        "__builtins__": SAFE_BUILTINS,
        "pd": pd,
        "np": np,
    }

    # Convert each data slice into a DataFrame for ergonomic use in user code.
    for key, rows in (req.data or {}).items():
        if isinstance(rows, list):
            try:
                namespace[key] = pd.DataFrame(rows)
            except Exception:
                namespace[key] = rows
        else:
            namespace[key] = rows

    def timeout_handler(_signum, _frame):
        raise TimeoutError(f"sandbox exec exceeded {EXEC_TIMEOUT_SEC}s")

    signal.signal(signal.SIGALRM, timeout_handler)
    signal.alarm(EXEC_TIMEOUT_SEC)

    try:
        exec(req.code, namespace)
        if "evaluate" not in namespace or not callable(namespace["evaluate"]):
            raise ValueError("code must define a callable named `evaluate`")
        result = namespace["evaluate"](**{k: v for k, v in namespace.items() if k in (req.data or {})})
        if not isinstance(result, dict):
            raise ValueError(f"evaluate() must return a dict; got {type(result).__name__}")
        # Coerce to JSON-able
        result_json = json.loads(json.dumps(result, default=_json_fallback))
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        return ExecuteResponse(ok=True, result=result_json, elapsed_ms=elapsed_ms)
    except TimeoutError as e:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        return ExecuteResponse(ok=False, error=str(e), elapsed_ms=elapsed_ms)
    except Exception as e:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        return ExecuteResponse(ok=False, error=f"{type(e).__name__}: {e}", elapsed_ms=elapsed_ms)
    finally:
        signal.alarm(0)


def _json_fallback(o: Any) -> Any:
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return float(o)
    if isinstance(o, np.ndarray):
        return o.tolist()
    if isinstance(o, pd.Timestamp):
        return o.isoformat()
    if isinstance(o, pd.DataFrame):
        return o.to_dict(orient="records")
    if isinstance(o, pd.Series):
        return o.to_list()
    return str(o)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info", access_log=False)
