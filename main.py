"""
main.py - FastAPI proxy for ARC & IRRMS
Protect with PROXY_SECRET (Railway env var). Exposes simple endpoints:
  POST /api/irrms/fetch  -> forwards to IRRMS getAllLocosRealtimeData
  POST /api/arc/fetch    -> forwards to ARC DefTableLocodetails (page param optional)
  GET  /api/debug/ip     -> returns server public IP (useful to check blocking)
  POST /api/debug/check  -> make a sample request to target and return status/snippet
"""

from fastapi import FastAPI, Request, HTTPException, Header
from pydantic import BaseModel
import os, requests, logging, json, socket
from typing import Optional, Dict, Any
from requests.adapters import HTTPAdapter, Retry

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("proxy")

app = FastAPI(title="TM Proxy")

# Config from env (set these in Railway secrets)
PROXY_SECRET = os.environ.get("PROXY_SECRET", "changeme")   # MUST set in Railway
ARC_LOGIN_URL = os.environ.get("ARC_LOGIN_URL", "http://49.249.49.218:5000/api/login")
ARC_LIVE_URL  = os.environ.get("ARC_LIVE_URL", "http://49.249.49.218:5000/api/DefTableLocodetails")
IRRMS_LOGIN_URL = os.environ.get("IRRMS_LOGIN_URL", "https://irrms-service.locomatrice.com/RMS/save/session/management/login")
IRRMS_DATA_URL  = os.environ.get("IRRMS_DATA_URL", "https://irrms-service.locomatrice.com/RMS/get/realTimeData/getAllLocosRealtimeData")

# Optional mapping: SHED_NAME -> authenticateKey stored in env like IRRMS_TATA_KEY
# Example env var IRRMS_TATA_KEY=xxxxx
def get_irrms_key_for_shed(shed_name: str) -> Optional[str]:
    if not shed_name:
        return None
    keyname = f"IRRMS_{shed_name.upper()}_KEY"
    return os.environ.get(keyname)

# Requests session with retries
session = requests.Session()
retries = Retry(total=2, backoff_factor=0.5, status_forcelist=[502,503,504], allowed_methods=["GET","POST"])
session.mount("https://", HTTPAdapter(max_retries=retries))
session.mount("http://", HTTPAdapter(max_retries=retries))


# ------------ Helpers -------------
def check_auth(authorization: Optional[str]):
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing Authorization header")
    # Accept "Bearer <secret>"
    if authorization.startswith("Bearer "):
        token = authorization.split(" ", 1)[1].strip()
    else:
        token = authorization.strip()
    if token != PROXY_SECRET:
        raise HTTPException(status_code=403, detail="Invalid proxy secret")


class IRRMSFetchRequest(BaseModel):
    shed_name: str
    shedId: Optional[int] = 0
    authenticateKey: Optional[str] = None  # optional override; better to use stored env keys
    fromDateTime: Optional[str] = None
    toDateTime: Optional[str] = None
    extra: Optional[Dict[str, Any]] = None


class ARCFetchRequest(BaseModel):
    page: Optional[int] = 1


# ------------- Endpoints ---------------

@app.get("/api/debug/ip")
def get_server_ip(authorization: Optional[str] = Header(None)):
    """Return server hostname and local IP (useful for debug)."""
    check_auth(authorization)
    hostname = socket.gethostname()
    try:
        # get public IP by hitting ipify (optional; may be blocked)
        public_ip = session.get("https://api.ipify.org?format=json", timeout=6).json().get("ip")
    except Exception:
        public_ip = None
    return {"hostname": hostname, "public_ip": public_ip}


@app.post("/api/debug/check")
def debug_check(url: str, method: str = "GET", authorization: Optional[str] = Header(None)):
    """Make a simple request to `url` and return status + small snippet."""
    check_auth(authorization)
    try:
        r = session.request(method.upper(), url, timeout=15)
        text_snippet = (r.text[:800] + "...") if r.text else ""
        return {"status_code": r.status_code, "snippet": text_snippet, "headers": dict(r.headers)}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Error during fetch: {str(e)}")


@app.post("/api/irrms/fetch")
def irrms_fetch(body: IRRMSFetchRequest, authorization: Optional[str] = Header(None)):
    """
    Proxy endpoint for IRRMS. Body should include shed_name and optional authenticateKey.
    If authenticateKey not provided, the proxy will look for IRRMS_<SHED>_KEY env var.
    """
    check_auth(authorization)

    key = body.authenticateKey or get_irrms_key_for_shed(body.shed_name)
    if not key:
        raise HTTPException(status_code=400, detail="No authenticateKey provided and no env key found")

    headers = {
        "Content-Type": "application/json; charset=UTF-8",
        "Origin": "https://irrms.locomatrice.com",
        "Referer": "https://irrms.locomatrice.com/",
        "Accept": "application/json, text/plain, */*",
        "Authorization": key
    }

    now = body.extra.get("now") if (body.extra and "now" in body.extra) else None
    # prepare payload similar to your app
    payload = {
        "locoId": 0, "locoTypeId": 0, "shedId": body.shedId or 0, "vendorId": 0,
        "startDate": body.extra.get("startDate") if body.extra and body.extra.get("startDate") else None,
        "endDate": body.extra.get("endDate") if body.extra and body.extra.get("endDate") else None,
        "actionMode": "temperatureventilation",
        "fromDateTime": body.fromDateTime,
        "toDateTime": body.toDateTime,
        "locoNo": "All", "refId": "GetSearchData"
    }
    # remove None values to avoid odd payload
    payload = {k: v for k, v in payload.items() if v is not None}
    try:
        r = session.post(IRRMS_DATA_URL, headers=headers, json=payload, timeout=25)
        # return status and JSON if possible (and body snippet)
        try:
            return {"status_code": r.status_code, "json": r.json()}
        except Exception:
            return {"status_code": r.status_code, "text": r.text[:2000]}
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/api/arc/fetch")
def arc_fetch(body: ARCFetchRequest, authorization: Optional[str] = Header(None)):
    """
    Proxy endpoint for ARC. Currently supports simple page fetch: GET ARC_LIVE_URL?page=N
    """
    check_auth(authorization)
    page = body.page or 1
    url = f"{ARC_LIVE_URL}?page={page}"
    try:
        r = session.get(url, timeout=20)
        try:
            return {"status_code": r.status_code, "json": r.json()}
        except Exception:
            return {"status_code": r.status_code, "text": r.text[:2000]}
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))

@app.get("/debug/test_arc")
def test_arc():
    import requests
    try:
        r = requests.get("http://49.249.49.218:5000", timeout=5)
        return {"status": r.status_code, "body": r.text[:300]}
    except Exception as e:
        return {"error": str(e)}
@app.get("/debug/test_irrms")
def test_irrms():
    import requests
    try:
        r = requests.get("https://irrms-service.locomatrice.com", timeout=5)
        return {"status": r.status_code, "body": r.text[:300]}
    except Exception as e:
        return {"error": str(e)}

