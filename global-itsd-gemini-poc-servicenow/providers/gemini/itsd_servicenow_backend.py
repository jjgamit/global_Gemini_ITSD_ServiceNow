"""ServiceNow-backed Global IT Service Desk backend for the POC.

Drop-in replacement for itsd_mock_backend.py: same function names, same return
shapes, so bot_gemini.py only needs small changes (see the patch notes).

Env vars (Railway):
  SN_INSTANCE          https://dev400307.service-now.com   (no trailing slash)
  SN_USER / SN_PASS    integration account (itil role; user_admin only if unlock is on)
  SN_TIMEOUT_S         request timeout in seconds (default 6, keep it short for voice)
  ITSD_DEFAULT_REGION  region used when a user has no APAC/EMEA/AMER Location (default APAC)
  ITSD_ALLOW_UNLOCK    "true" lets account_unlock really clear locked_out (default false)

What is real vs. simulated:
  lookup_employee      real   (sys_user)
  create_mock_ticket   real   (incident / sc_request)
  search_kb            real   (kb_knowledge), falls back to the approved mock KB
  run_self_service     guidance from the approved KB; unlock is real only if enabled
  get_regional_route   label only (same as the mock)
"""
import logging
import os
import re
from datetime import datetime, timezone
from html import unescape

import requests

try:
    from providers.gemini import itsd_mock_backend as _mock
except ImportError:  # running from inside providers/gemini for quick tests
    import itsd_mock_backend as _mock

logger = logging.getLogger(__name__)

TIMEOUT = float(os.getenv("SN_TIMEOUT_S", "6"))
_UNAVAILABLE = (
    "The IT ticketing system is temporarily unavailable. Apologize briefly and "
    "offer to transfer the caller to a live IT Service Desk agent."
)
_SAFETY_NOTE = "Never ask the caller to speak or send a password, OTP, MFA code, or recovery code."

_session = requests.Session()
_session.headers.update({"Accept": "application/json", "Content-Type": "application/json"})

_SYS_ID_CACHE = {}   # employee id / user name -> sys_user sys_id
_TICKETS = []        # tickets created in this process (tests / diagnostics)


class ServiceNowError(Exception):
    pass


# ------------------------------------------------------------------ plumbing
def _base():
    return os.getenv("SN_INSTANCE", "").strip().rstrip("/")


def _request(method, path, **kwargs):
    base = _base()
    user, pw = os.getenv("SN_USER", ""), os.getenv("SN_PASS", "")
    if not (base and user and pw):
        raise ServiceNowError("SN_INSTANCE, SN_USER and SN_PASS must all be set")
    try:
        r = _session.request(method, f"{base}{path}", auth=(user, pw), timeout=TIMEOUT, **kwargs)
    except requests.RequestException as exc:
        raise ServiceNowError(f"ServiceNow unreachable: {exc}") from exc
    if r.status_code == 401:
        raise ServiceNowError("ServiceNow auth failed (401) - check SN_USER / SN_PASS")
    if r.status_code == 403:
        raise ServiceNowError(f"ServiceNow forbidden (403) - missing role? {r.text[:200]}")
    if not r.ok:
        raise ServiceNowError(f"ServiceNow error {r.status_code}: {r.text[:200]}")
    try:
        return r.json().get("result")
    except ValueError as exc:
        raise ServiceNowError("Non-JSON reply (the instance may be hibernating)") from exc


def _allow_unlock():
    return os.getenv("ITSD_ALLOW_UNLOCK", "false").strip().lower() in ("1", "true", "yes", "on")


# ----------------------------------------------------------------- employees
_USER_FIELDS = "sys_id,employee_number,user_name,first_name,last_name,name,email,department,location,active,locked_out"


def _find_user(identifier):
    # Keep only safe characters: blocks '^' (encoded-query injection) and stray spaces
    # that speech transcription can add inside an ID.
    ident = re.sub(r"[^\w.@+\-]", "", str(identifier or ""))
    if not ident:
        return None
    res = _request(
        "GET",
        "/api/now/table/sys_user",
        params={
            "sysparm_query": f"employee_number={ident}^ORuser_name={ident}^ORemail={ident}",
            "sysparm_limit": 1,
            "sysparm_display_value": "true",
            "sysparm_fields": _USER_FIELDS,
        },
    )
    return res[0] if res else None


def _status(u):
    if str(u.get("active", "true")).lower() == "false":
        return "DISABLED"
    if str(u.get("locked_out", "false")).lower() == "true":
        return "LOCKED"
    return "ACTIVE"


def _region(u):
    loc = str(u.get("location") or "").upper()
    for r in ("APAC", "EMEA", "AMER"):
        if r in loc:
            return r
    return os.getenv("ITSD_DEFAULT_REGION", "APAC").strip().upper()


def _shape_employee(u):
    return {
        "employee_id": u.get("employee_number") or u.get("user_name") or "",
        "first_name": u.get("first_name") or "",
        "last_name": u.get("last_name") or "",
        "full_name": u.get("name") or "",
        "email": u.get("email") or "",
        "department": u.get("department") or "",
        "region": _region(u),
        "account_status": _status(u),
        "data_source": "SERVICENOW",
    }


def _sys_id_for(identifier):
    key = str(identifier or "").strip().upper()
    if key in _SYS_ID_CACHE:
        return _SYS_ID_CACHE[key]
    u = _find_user(identifier)
    if not u:
        return None
    _SYS_ID_CACHE[key] = u["sys_id"]
    return u["sys_id"]


def lookup_employee(employee_id: str) -> dict:
    normalized = str(employee_id or "").strip()
    try:
        u = _find_user(normalized)
    except ServiceNowError as exc:
        logger.error("lookup_employee failed: %s", exc)
        return {"found": False, "error": True, "employee_id": normalized,
                "message": _UNAVAILABLE, "data_source": "SERVICENOW"}
    if not u:
        return {"found": False, "employee_id": normalized,
                "message": "No matching employee found in ServiceNow.", "data_source": "SERVICENOW"}
    emp = _shape_employee(u)
    _SYS_ID_CACHE[normalized.upper()] = u["sys_id"]
    if emp["employee_id"]:
        _SYS_ID_CACHE[emp["employee_id"].upper()] = u["sys_id"]
    return {"found": True, "employee": emp,
            "message": "ServiceNow employee record found. This is not identity verification.",
            "data_source": "SERVICENOW"}


# ----------------------------------------------------------------- knowledge
def _steps_from_html(html):
    text = re.sub(r"<(br|/p|/li|/div|/h\d|/tr)[^>]*>", "\n", html or "", flags=re.I)
    text = unescape(re.sub(r"<[^>]+>", " ", text))
    lines = [re.sub(r"\s+", " ", ln).strip()[:300] for ln in text.split("\n")]
    return [ln for ln in lines if ln][:8]


def search_kb(query: str, category: str = "general") -> dict:
    terms = [t for t in re.findall(r"[A-Za-z0-9]+", str(query or "").lower()) if len(t) > 2][:3]
    if terms:
        likes = "^OR".join(f"short_descriptionLIKE{t}^ORtextLIKE{t}" for t in terms)
        try:
            rows = _request(
                "GET",
                "/api/now/table/kb_knowledge",
                params={
                    "sysparm_query": f"workflow_state=published^{likes}",
                    "sysparm_limit": 3,
                    "sysparm_fields": "number,short_description,text",
                },
            ) or []
        except ServiceNowError as exc:
            logger.warning("KB search failed, using approved mock KB: %s", exc)
            rows = []
        articles = [
            {
                "article_id": r.get("number"),
                "title": r.get("short_description"),
                "category": str(category or "general"),
                "steps": _steps_from_html(r.get("text")),
                "safety_note": _SAFETY_NOTE,
            }
            for r in rows
        ]
        if articles:
            return {"found": True, "articles": articles, "count": len(articles),
                    "data_source": "SERVICENOW_KB", "note": "Published ServiceNow knowledge articles."}
    # Nothing in ServiceNow (a PDI has no password-reset articles) -> approved POC articles.
    return _mock.search_kb(query, category)


# -------------------------------------------------------------- self service
def run_self_service(employee_id: str, action: str, application: str = "", resolved=None) -> dict:
    result = _mock.run_self_service(employee_id, action, application, resolved)
    if not (result.get("ok") and str(action or "").lower().strip() == "account_unlock" and _allow_unlock()):
        return result
    try:
        sys_id = _sys_id_for(employee_id)
        if sys_id:
            _request("PATCH", f"/api/now/table/sys_user/{sys_id}", json={"locked_out": "false"})
            result["real_system_changed"] = True
            result["data_source"] = "SERVICENOW"
            result["note"] = "Account was unlocked in ServiceNow."
    except ServiceNowError as exc:
        logger.error("unlock failed: %s", exc)
        result["note"] = "Automatic unlock failed; create an incident or transfer to a live agent."
    return result


# ------------------------------------------------------------------- tickets
def create_mock_ticket(record_type: str, employee_id: str, region: str, short_description: str,
                       description: str, category: str = "general", kb_article_id: str = "") -> dict:
    record_type = str(record_type or "incident").lower().strip()
    if record_type not in ("incident", "request"):
        record_type = "incident"
    emp_id = str(employee_id or "").strip()
    region = str(region or "").strip().upper()
    short = str(short_description or "")[:160]
    body = (
        f"{str(description or '')[:1200]}\n\n--\n"
        f"Employee ID: {emp_id}\nRegion: {region}\nCategory: {category}\n"
        f"KB article: {kb_article_id}\nCreated by the Global ITSD voice bot (PoC)."
    )
    try:
        sys_id = _sys_id_for(emp_id)
        rec, table = None, "incident"
        if record_type == "request":
            try:
                payload = {"short_description": short, "description": body}
                if sys_id:
                    payload["requested_for"] = sys_id
                rec = _request("POST", "/api/now/table/sc_request", json=payload)
                table = "sc_request"
            except ServiceNowError as exc:
                logger.warning("sc_request failed, logging an incident instead: %s", exc)
        if rec is None:
            payload = {"short_description": short, "description": body,
                       "category": "inquiry", "contact_type": "phone", "urgency": "3"}
            if sys_id:
                payload["caller_id"] = sys_id
            rec = _request("POST", "/api/now/table/incident", json=payload)
            table = "incident"
    except ServiceNowError as exc:
        logger.error("create ticket failed: %s", exc)
        return {"created": False, "message": _UNAVAILABLE, "data_source": "SERVICENOW"}

    number = rec["number"]
    ticket = {
        "number": number,
        "record_type": record_type,
        "employee_id": emp_id,
        "region": region,
        "short_description": short,
        "category": str(category or "general")[:80],
        "kb_article_id": str(kb_article_id or "")[:40],
        "state": "NEW",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "data_source": "SERVICENOW",
        "real_servicenow_record_created": True,
        "sys_id": rec.get("sys_id", ""),
        "servicenow_url": f"{_base()}/{table}.do?sysparm_query=number={number}",
    }
    _TICKETS.append(ticket)
    return {"created": True, "ticket": ticket.copy(),
            "note": f"A real ServiceNow {table} record was created: {number}."}


create_ticket = create_mock_ticket  # friendlier alias


def get_regional_route(region: str) -> dict:
    return _mock.get_regional_route(region)


def list_mock_tickets() -> list:
    return [t.copy() for t in _TICKETS]


if __name__ == "__main__":
    # Quick test:  python itsd_servicenow_backend.py
    print(lookup_employee("EMP1001"))
    print(search_kb("password reset", "password_reset").get("data_source"))
