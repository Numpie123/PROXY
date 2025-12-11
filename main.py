"""
main.py - FastAPI proxy for ARC & IRRMS (FINAL FIXED VERSION)
Protect with PROXY_SECRET (Railway env var).
"""

from fastapi import FastAPI, Request, HTTPException, Header
from pydantic import BaseModel
from typing import Optional, Dict, Any
import os, requests, json, logging, socket
from datetime import datetime, timedelta
from requests.adapters import HTTPAdapter, Retry

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("proxy")

app = FastAPI(title="TM Proxy")

# -------------------------------------------------------------------
# ENVIRONMENT VARIABLES
# -------------------------------------------------------------------
PROXY_SECRET = os.environ.get("PROXY_SECRET", "changeme")

ARC_LOGIN_URL = os.environ.get("ARC_LOGIN_URL",
                               "http://49.249.49.218:5000/api/login")
ARC_LIVE_URL  = os.environ.get("ARC_LIVE_URL",
                               "http://49.249.49.218:5000/api/DefTableLocodetails")

IRRMS_LOGIN_URL = os.environ.get("IRRMS_LOGIN_URL",
                                 "https://irrms-service.locomatrice.com/RMS/save/session/management/login")
IRRMS_DATA_URL  = os.environ.get("IRRMS_DATA_URL",
                                 "https://irrms-service.locomatrice.com/RMS/get/realTimeData/getAllLocosRealtimeData")

# IRRMS AUTH KEYS (from Railway env)
IRRMS_KEYS = {
    "TATA": os.environ.get("TATA_AUTH"),
    "BKSC": os.environ.get("BKSC_AUTH"),
    "ROU":  os.environ.get("ROU_AUTH"),
    "BNDM": os.environ.get("BNDM_AUTH")
}

# -------------------------------------------------------------------
# REQUEST SESSION WITH RETRIES
# -------------------------------------------------------------------
session = requests.Session()
retries = Retry(
    total=2, backoff_factor=0.5,
    status_forcelist=[502, 503, 504],
    allowed_methods=["GET", "POST"]
)
session.mount("http://", HTTPAdapter(max_retries=retries))
session.mount("https://", HTTPAdapter(max_retries=retries))

# -------------------------------------------------------------------
# AUTH CHECK
# -------------------------------------------------------------------
def check_auth(secret_header: Optional[str]):
    if secret_header is None:
        raise HTTPException(status_code=401,
                            detail="Missing X-Proxy-Secret")
    if secret_header != PROXY_SECRET:
        raise HTTPException(status_code=403,
                            detail="Invalid X-Proxy-Secret")


def get_irrms_key_for_shed(shed: str):
    return IRRMS_KEYS.get(shed.upper())


# -------------------------------------------------------------------
# REQUEST MODELS
# -------------------------------------------------------------------
class IRRMSFetchRequest(BaseModel):
    shed_name: str
    shedId: Optional[int] = 0
    authenticateKey: Optional[str] = None


class ARCFetchRequest(BaseModel):
    page: Optional[int] = 1


# -------------------------------------------------------------------
# DEBUG ENDPOINTS
# -------------------------------------------------------------------
@app.get("/api/debug/ip")
def debug_ip(x_proxy_secret: Optional[str] = Header(None)):
    check_auth(x_proxy_secret)

    hostname = socket.gethostname()
    try:
        public_ip = session.get("https://api.ipify.org?format=json",
                                timeout=5).json().get("ip")
    except:
        public_ip = None

    return {
        "hostname": hostname,
        "public_ip": public_ip
    }


@app.post("/api/debug/check")
def debug_check(url: str,
                method: str = "GET",
                x_proxy_secret: Optional[str] = Header(None)):
    check_auth(x_proxy_secret)

    try:
        r = session.request(method.upper(), url, timeout=12)
        return {
            "status_code": r.status_code,
            "snippet": r.text[:800],
            "headers": dict(r.headers)
        }
    except Exception as e:
        raise HTTPException(status_code=500,
                            detail=f"Proxy failed: {e}")


# -------------------------------------------------------------------
# IRRMS FETCH (FINAL FIXED VERSION)
# -------------------------------------------------------------------
@app.post("/api/irrms/fetch")
def irrms_fetch(body: IRRMSFetchRequest,
                x_proxy_secret: Optional[str] = Header(None)):

    check_auth(x_proxy_secret)

    shed = body.shed_name
    shed_id = body.shedId or 0

    # Get decrypt key
    auth_key = body.authenticateKey or get_irrms_key_for_shed(shed)
    if not auth_key:
        raise HTTPException(status_code=400,
                            detail="No authenticateKey found for shed")

    # Required IRRMS headers
    headers = {
        "Content-Type": "application/json; charset=UTF-8",
        "Origin": "https://irrms.locomatrice.com",
        "Referer": "https://irrms.locomatrice.com/",
        "Accept": "application/json, text/plain, */*",
        "Authorization": auth_key
    }

    # IRRMS strictly requires DD-MM-YYYY HH:MM:SS format
    now = datetime.utcnow()
    from_dt = (now - timedelta(minutes=15)).strftime("%d-%m-%Y %H:%M:%S")
    to_dt   = (now + timedelta(minutes=5)).strftime("%d-%m-%Y %H:%M:%S")

    payload = {
        "locoId": 0,
        "locoTypeId": 0,
        "shedId": shed_id,
        "vendorId": 0,
        "startDate": now.strftime("%d-%m-%Y"),
        "endDate": (now + timedelta(days=1)).strftime("%d-%m-%Y"),
        "actionMode": "temperatureventilation",
        "fromDateTime": from_dt,
        "toDateTime": to_dt,
        "locoNo": "All",
        "refId": "GetSearchData"
    }

    try:
        r = session.post(IRRMS_DATA_URL,
                         json=payload, headers=headers, timeout=20)

        try:
            return {"status_code": r.status_code, "json": r.json()}
        except:
            return {"status_code": r.status_code, "text": r.text[:2000]}

    except Exception as e:
        raise HTTPException(status_code=500,
                            detail=f"IRRMS request failed: {e}")


# -------------------------------------------------------------------
# ARC FETCH
# -------------------------------------------------------------------
@app.post("/api/arc/fetch")
def arc_fetch(body: ARCFetchRequest,
              x_proxy_secret: Optional[str] = Header(None)):

    check_auth(x_proxy_secret)

    page = body.page or 1
    url = f"{ARC_LIVE_URL}?page={page}"

    try:
        r = session.get(url, timeout=15)

        try:
            return {"status_code": r.status_code, "json": r.json()}
        except:
            return {"status_code": r.status_code, "text": r.text[:2000]}

    except Exception as e:
        raise HTTPException(status_code=500,
                            detail=f"ARC request failed: {e}")


# -------------------------------------------------------------------
# SIMPLE TESTS
# -------------------------------------------------------------------
@app.get("/debug/test_arc")
def test_arc():
    try:
        r = requests.get("http://49.249.49.218:5000", timeout=5)
        return {"status": r.status_code, "body": r.text[:300]}
    except Exception as e:
        return {"error": str(e)}

@app.get("/debug/test_irrms")
def test_irrms():
    try:
        r = requests.get("https://irrms-service.locomatrice.com",
                          timeout=5)
        return {"status": r.status_code, "body": r.text[:300]}
    except Exception as e:
        return {"error": str(e)}
