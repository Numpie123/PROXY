"""
main.py - FastAPI proxy for ARC & IRRMS
Protect with PROXY_SECRET (Railway env var). Exposes:
  POST /api/irrms/fetch
  POST /api/arc/fetch
  GET  /api/debug/ip
  POST /api/debug/check
"""

from fastapi import FastAPI, Request, HTTPException, Header
from pydantic import BaseModel
import os, requests, logging, json, socket
from typing import Optional, Dict, Any
from requests.adapters import HTTPAdapter, Retry
from datetime import datetime, timedelta

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("proxy")

app = FastAPI(title="TM Proxy")

# ------------------------------
# ENV CONFIG
# ------------------------------
PROXY_SECRET = os.environ.get("PROXY_SECRET", "changeme")  # MUST SET IN RAILWAY

ARC_LOGIN_URL = os.environ.get("ARC_LOGIN_URL", "http://49.249.49.218:5000/api/login")
ARC_LIVE_URL  = os.environ.get("ARC_LIVE_URL",  "http://49.249.49.218:5000/api/DefTableLocodetails")

IRRMS_LOGIN_URL = os.environ.get(
    "IRRMS_LOGIN_URL",
    "https://irrms-service.locomatrice.com/RMS/save/session/management/login"
)
IRRMS_DATA_URL  = os.environ.get(
    "IRRMS_DATA_URL",
    "https://irrms-service.locomatrice.com/RMS/get/realTimeData/getAllLocosRealtimeData"
)

# ------------------------------
# RETRY SESSION
# ------------------------------
session = requests.Session()
retries = Retry(
    total=2,
    backoff_factor=0.5,
    status_forcelist=[502, 503, 504],
    allowed_methods=["GET", "POST"]
)
session.mount("http://", HTTPAdapter(max_retries=retries))
session.mount("https://", HTTPAdapter(max_retries=retries))

# ------------------------------
# SECRET CHECK
# ------------------------------
def verify(secret_header: Optional[str]):
    if secret_header is None:
        raise HTTPException(status_code=401, detail="Missing X-Proxy-Secret header")

    if secret_header != PROXY_SECRET:
        raise HTTPException(status_code=403, detail="Invalid Proxy Secret")

# ------------------------------
# Helpers
# ------------------------------
def get_irrms_key_for_shed(shed: str) -> Optional[str]:
    shed = shed.upper()

    mapping = {
        "TATA": os.environ.get("TATA_AUTH"),
        "BKSC": os.environ.get("BKSC_AUTH"),
        "ROU":  os.environ.get("ROU_AUTH"),
        "BNDM": os.environ.get("BNDM_AUTH")
    }

    return mapping.get(shed)

def check_auth(x_proxy_secret: Optional[str]):
    """Ensures Streamlit sends correct secret."""
    verify(x_proxy_secret)

# ------------------------------
# MODELS
# ------------------------------
class IRRMSFetchRequest(BaseModel):
    shed_name: str
    shedId: Optional[int] = 0
    authenticateKey: Optional[str] = None

class ARCFetchRequest(BaseModel):
    page: Optional[int] = 1

# ------------------------------
# ENDPOINTS
# ------------------------------
@app.get("/api/debug/ip")
def get_ip(x_proxy_secret: Optional[str] = Header(None)):
    verify(x_proxy_secret)

    hostname = socket.gethostname()
    try:
        public_ip = session.get("https://api.ipify.org?format=json", timeout=6).json().get("ip")
    except Exception:
        public_ip = None

    return {"hostname": hostname, "public_ip": public_ip}


@app.post("/api/debug/check")
def debug_check(url: str, method: str = "GET", x_proxy_secret: Optional[str] = Header(None)):
    verify(x_proxy_secret)

    try:
        r = session.request(method.upper(), url, timeout=15)
        return {
            "status_code": r.status_code,
            "snippet": r.text[:800],
            "headers": dict(r.headers)
        }
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Proxy request failed: {str(e)}")


# ------------------------------
@app.post("/api/irrms/fetch")
def irrms_fetch(
    body: IRRMSFetchRequest,
    x_proxy_secret: Optional[str] = Header(None)
):
    check_auth(x_proxy_secret)

    shed = body.shed_name.upper()
    auth_key = body.authenticateKey or get_irrms_key_for_shed(shed)

    if not auth_key:
        raise HTTPException(status_code=400, detail=f"No authenticateKey for shed {shed}")

    # -------- 1) LOGIN FIRST --------
    login_payload = {
        "authenticateKey": auth_key,
        "sessionType": "login",
        "uniqueCode": "",
        "userId": "",
        "shed_name": shed
    }

    login_headers = {
        "Content-Type": "application/json; charset=UTF-8",
        "Origin": "https://irrms.locomatrice.com",
        "Referer": "https://irrms.locomatrice.com/",
        "Accept": "application/json, text/plain, */*"
    }

    try:
        login_resp = session.post(
            IRRMS_LOGIN_URL,
            data=json.dumps(login_payload),
            headers=login_headers,
            timeout=15
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"IRRMS login failed: {str(e)}")

    if login_resp.status_code != 200:
        return {
            "status_code": login_resp.status_code,
            "json": {"error": "IRRMS login failed", "body": login_resp.text[:300]}
        }

    # -------- 2) AFTER LOGIN → FETCH DATA --------
    now = datetime.utcnow()
    data_payload = {
        "locoId": 0,
        "locoTypeId": 0,
        "shedId": body.shedId or 0,
        "vendorId": 0,
        "startDate": now.strftime("%Y-%m-%d"),
        "endDate": (now + timedelta(days=1)).strftime("%Y-%m-%d"),
        "actionMode": "temperatureventilation",
        "fromDateTime": (now - timedelta(minutes=15)).strftime("%Y-%m-%d %H:%M:%S"),
        "toDateTime": (now + timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S"),
        "locoNo": "All",
        "refId": "GetSearchData"
    }

    data_headers = {
        "Content-Type": "application/json; charset=UTF-8",
        "Authorization": auth_key,
        "Origin": "https://irrms.locomatrice.com",
        "Referer": "https://irrms.locomatrice.com/"
    }

    try:
        r = session.post(IRRMS_DATA_URL, json=data_payload, headers=data_headers, timeout=25)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))

    # -------- UNIFORM PROXY OUTPUT --------
    try:
        return {"status_code": r.status_code, "json": r.json()}
    except:
        return {"status_code": r.status_code, "text": r.text[:2000]}



# ------------------------------
# ARC FETCH
# ------------------------------
@app.post("/api/arc/fetch")
def arc_fetch(
    body: ARCFetchRequest,
    x_proxy_secret: Optional[str] = Header(None)
):
    verify(x_proxy_secret)

    url = f"{ARC_LIVE_URL}?page={body.page or 1}"

    try:
        r = session.get(url, timeout=20)
        try:
            return {"status_code": r.status_code, "json": r.json()}
        except:
            return {"status_code": r.status_code, "text": r.text[:2000]}
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


# ------------------------------
# TEST ENDPOINTS
# ------------------------------
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
        r = requests.get("https://irrms-service.locomatrice.com", timeout=5)
        return {"status": r.status_code, "body": r.text[:300]}
    except Exception as e:
        return {"error": str(e)}
