#!/usr/bin/env python3
"""
Simple control API for running migration scripts.

Endpoints:
  GET  /health                  — liveness check
  GET  /scripts                 — list available scripts
  POST /run/{script}            — start script (dry_run=true by default)
  GET  /jobs/{job_id}           — get job status + logs
  GET  /jobs/{job_id}/logs      — stream full logs

Auth: Bearer token via API_SECRET env var.
"""

import datetime
import os
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Header
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

load_dotenv()

API_SECRET     = os.getenv("API_SECRET", "")
PLANFIX_HOST   = os.getenv("PLANFIX_HOST",  "https://itcomms.planfix.com")
PLANFIX_TOKEN  = os.getenv("PLANFIX_TOKEN", "")

PF_PAYMENT_DATE_FIELD = 128157
TAX_DATATAG_ID        = 9611
TAX_TYPE_FIELD_ID     = 58393
TAX_RATE_TO_KEY       = {0: 3, 12: 2, 16: 1}

_pf = requests.Session()
_pf.headers.update({"Content-Type": "application/json"})


def pf_req(method: str, path: str, **kwargs) -> dict:
    _pf.headers["Authorization"] = f"Bearer {PLANFIX_TOKEN}"
    r = _pf.request(method, f"{PLANFIX_HOST}/rest{path}", **kwargs)
    if not r.ok:
        raise HTTPException(status_code=502, detail=f"Planfix {method} {path} → {r.status_code}: {r.text[:300]}")
    return r.json() if r.text.strip() else {}

# Available scripts and their descriptions
SCRIPTS = {
    "fill_supplier":          "fill_supplier.py --live",
    "fill_supplier_megaplan": "fill_supplier_from_megaplan.py --live",
    "create_contacts":        "create_contacts_and_fill.py --live",
    "enrich_contacts":        "enrich_contacts.py --live",
    "fill_payment_date":      "fill_payment_date.py --live",
    "fill_invoice_payment":   "fill_invoice_payment.py --live",
    "run_migration":          "run_migration.py",
}

# In-memory job store  { job_id: {"status", "script", "started_at", "output", "returncode"} }
jobs: dict[str, dict] = {}
jobs_lock = threading.Lock()

app = FastAPI(title="ITCOMMS Migration API", version="1.0")


# ---------------------------------------------------------------------------
# Auth

def check_auth(authorization: Optional[str]) -> None:
    if not API_SECRET:
        return  # no secret configured — open (dev mode)
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Bearer token")
    if authorization.removeprefix("Bearer ").strip() != API_SECRET:
        raise HTTPException(status_code=403, detail="Invalid token")


# ---------------------------------------------------------------------------
# Background runner

def _run_script(job_id: str, cmd: list[str]) -> None:
    buf: list[str] = []
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            cwd="/app",
        )
        for line in proc.stdout:
            buf.append(line)
            with jobs_lock:
                jobs[job_id]["output"] = "".join(buf)
        proc.wait()
        with jobs_lock:
            jobs[job_id]["status"] = "done" if proc.returncode == 0 else "failed"
            jobs[job_id]["returncode"] = proc.returncode
    except Exception as e:
        with jobs_lock:
            jobs[job_id]["status"] = "error"
            jobs[job_id]["output"] = str(e)


# ---------------------------------------------------------------------------
# Routes

@app.get("/health")
def health():
    return {"ok": True, "jobs_running": sum(1 for j in jobs.values() if j["status"] == "running")}


@app.get("/scripts")
def list_scripts(authorization: Optional[str] = Header(None)):
    check_auth(authorization)
    return SCRIPTS


@app.post("/run/{script_name}")
def run_script(
    script_name: str,
    dry_run: bool = True,
    authorization: Optional[str] = Header(None),
):
    check_auth(authorization)
    if script_name not in SCRIPTS:
        raise HTTPException(status_code=404, detail=f"Unknown script: {script_name}. Available: {list(SCRIPTS)}")

    # Build command — strip --live flag for dry_run
    base_cmd = SCRIPTS[script_name]
    if dry_run:
        base_cmd = base_cmd.replace(" --live", "")
    cmd = ["python", "-u"] + base_cmd.split()

    job_id = str(uuid.uuid4())[:8]
    with jobs_lock:
        jobs[job_id] = {
            "job_id":     job_id,
            "script":     script_name,
            "cmd":        " ".join(cmd),
            "status":     "running",
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
            "output":     "",
            "returncode": None,
        }

    t = threading.Thread(target=_run_script, args=(job_id, cmd), daemon=True)
    t.start()

    return {"job_id": job_id, "cmd": " ".join(cmd), "status": "running"}


@app.get("/jobs")
def list_jobs(authorization: Optional[str] = Header(None)):
    check_auth(authorization)
    with jobs_lock:
        return [
            {k: v for k, v in j.items() if k != "output"}
            for j in sorted(jobs.values(), key=lambda x: x["started_at"], reverse=True)
        ]


@app.get("/jobs/{job_id}")
def get_job(job_id: str, authorization: Optional[str] = Header(None)):
    check_auth(authorization)
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.get("/jobs/{job_id}/logs", response_class=PlainTextResponse)
def get_logs(job_id: str, authorization: Optional[str] = Header(None)):
    check_auth(authorization)
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job["output"]


class InvoiceFillRequest(BaseModel):
    pf_task_id:   int
    payment_date: Optional[str] = None   # "YYYY-MM-DD"
    tax_rate:     Optional[int] = None   # 0, 12, 16


@app.post("/invoice/fill")
def fill_invoice(body: InvoiceFillRequest, authorization: Optional[str] = Header(None)):
    """Записывает дату оплаты и/или TAX аналитику в задачу Planfix."""
    check_auth(authorization)
    result = {"pf_task_id": body.pf_task_id, "date": None, "tax": None, "errors": []}

    if body.payment_date:
        try:
            d = datetime.date.fromisoformat(body.payment_date)
            ts = int(datetime.datetime(d.year, d.month, d.day, tzinfo=datetime.timezone.utc).timestamp())
            pf_req("POST", f"/task/{body.pf_task_id}", json={
                "customFieldData": [{"field": {"id": PF_PAYMENT_DATE_FIELD}, "value": ts}]
            })
            result["date"] = body.payment_date
        except HTTPException as e:
            result["errors"].append(f"date: {e.detail}")

    if body.tax_rate is not None:
        directory_key = TAX_RATE_TO_KEY.get(body.tax_rate)
        if directory_key is None:
            result["errors"].append(f"tax: unknown rate {body.tax_rate}")
        else:
            try:
                pf_req("POST", f"/task/{body.pf_task_id}/datatags/", json={
                    "dataTag": {"id": TAX_DATATAG_ID},
                    "items": [{"customFieldData": [
                        {"field": {"id": TAX_TYPE_FIELD_ID}, "value": directory_key}
                    ]}]
                })
                result["tax"] = f"{body.tax_rate}%"
            except HTTPException as e:
                result["errors"].append(f"tax: {e.detail}")

    return result


@app.get("/logs/{filename}", response_class=PlainTextResponse)
def read_log_file(filename: str, lines: int = 100, authorization: Optional[str] = Header(None)):
    """Read tail of a log file from /app/logs/"""
    check_auth(authorization)
    path = Path("/app/logs") / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Log file not found: {filename}")
    all_lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(all_lines[-lines:])


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
