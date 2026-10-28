"""
servicenow_backend.py
ServiceNow (PDI) backend for the Global IT Service Desk voice bot.
Drop-in style replacement for itsd_mock_backend.py.

Env vars (set in Railway):
  SN_INSTANCE  e.g. https://dev12345.service-now.com   (no trailing slash)
  SN_USER      integration user, e.g. svc_itsd_bot
  SN_PASS      its password

Service account roles on the PDI:
  itil        -> read users/KB, create/read incidents
  user_admin  -> only needed if you want unlock_account() to really unlock
"""
import os
import requests

BASE = os.environ.get("SN_INSTANCE", "").rstrip("/")
AUTH = (os.environ.get("SN_USER", ""), os.environ.get("SN_PASS", ""))
HEADERS = {"Accept": "application/json", "Content-Type": "application/json"}
TIMEOUT = 10  # seconds; a hibernating PDI will fail fast instead of hanging a call


class ServiceNowError(Exception):
    pass


def _request(method, path, **kwargs):
    if not BASE:
        raise ServiceNowError("SN_INSTANCE is not set")
    try:
        r = requests.request(
            method, f"{BASE}{path}", auth=AUTH, headers=HEADERS, timeout=TIMEOUT, **kwargs
        )
    except requests.RequestException as e:
        # Usually a hibernating instance or network problem
        raise ServiceNowError(f"ServiceNow unreachable: {e}") from e
    if r.status_code == 401:
        raise ServiceNowError("ServiceNow auth failed (check SN_USER / SN_PASS)")
    if not r.ok:
        raise ServiceNowError(f"ServiceNow error {r.status_code}: {r.text[:200]}")
    return r.json().get("result")


def _status(user):
    if str(user.get("active", "true")).lower() == "false":
        return "disabled"
    if str(user.get("locked_out", "false")).lower() == "true":
        return "locked"
    return "active"


def _shape_employee(u):
    return {
        "sys_id": u.get("sys_id"),
        "employee_id": u.get("employee_number") or u.get("user_name"),
        "name": u.get("name"),
        "department": u.get("department") or "",
        "region": u.get("location") or "",
        "email": u.get("email") or "",
        "account_status": _status(u),
    }


# ---------------------------------------------------------------- employees
def lookup_employee(identifier):
    """Find a user by employee number, user name or email. Returns dict or None."""
    ident = str(identifier).strip()
    query = f"employee_number={ident}^ORuser_name={ident}^ORemail={ident}"
    res = _request(
        "GET",
        "/api/now/table/sys_user",
        params={
            "sysparm_query": query,
            "sysparm_limit": 1,
            "sysparm_display_value": "true",
            "sysparm_fields": "sys_id,employee_number,user_name,name,department,location,email,active,locked_out",
        },
    )
    return _shape_employee(res[0]) if res else None


# ------------------------------------------------------------------ tickets
def create_ticket(employee_sys_id, short_description, description="", category="inquiry", urgency=3):
    """Create an incident. Returns {number, sys_id, url}."""
    res = _request(
        "POST",
        "/api/now/table/incident",
        json={
            "caller_id": employee_sys_id,
            "short_description": short_description,
            "description": description,
            "category": category,
            "urgency": str(urgency),
            "contact_type": "phone",
        },
    )
    return {
        "number": res["number"],
        "sys_id": res["sys_id"],
        "url": ticket_url(res["number"]),
    }


def get_ticket(number):
    res = _request(
        "GET",
        "/api/now/table/incident",
        params={
            "sysparm_query": f"number={number}",
            "sysparm_limit": 1,
            "sysparm_display_value": "true",
            "sysparm_fields": "number,short_description,state,priority,assigned_to,sys_updated_on",
        },
    )
    return res[0] if res else None


def ticket_url(number):
    """Agent-facing link that opens the incident in ServiceNow."""
    return f"{BASE}/incident.do?sysparm_query=number={number}"


# ---------------------------------------------------------------- knowledge
def search_kb(query, limit=3):
    res = _request(
        "GET",
        "/api/now/table/kb_knowledge",
        params={
            "sysparm_query": f"workflow_state=published^short_descriptionLIKE{query}^ORtextLIKE{query}",
            "sysparm_limit": limit,
            "sysparm_fields": "number,short_description,text",
        },
    )
    return res or []


# ----------------------------------------------------------------- password
def unlock_account(employee_sys_id):
    """
    PoC 'password reset': a PDI has no real directory, so we unlock the account
    (needs user_admin) and the caller should also log an incident for the audit trail.
    """
    _request("PATCH", f"/api/now/table/sys_user/{employee_sys_id}", json={"locked_out": "false"})
    return True


# ------------------------------------------------- Infinity handoff helpers
def handoff_context(employee, concern_type, reason, issue_summary, ticket=None):
    """Dict for byobotLiveAgentHandoff.context (what the agent screenpop reads)."""
    ctx = {
        "employee_name": employee["name"],
        "employee_id": employee["employee_id"],
        "department": employee["department"],
        "region": employee["region"],
        "email": employee["email"],
        "account_status": employee["account_status"],
        "concern_type": concern_type,
        "reason": reason,
        "issue_summary": issue_summary,
    }
    if ticket:
        ctx["incident_number"] = ticket["number"]
        ctx["servicenow_url"] = ticket["url"]
    return ctx


def to_crm_data(d):
    """'key: value | key: value' text for custom.crmData (plain text, not JSON)."""
    return " | ".join(f"{k}: {v}" for k, v in d.items() if v not in (None, ""))


if __name__ == "__main__":
    # Quick smoke test:  python servicenow_backend.py
    emp = lookup_employee("abel.tuter")  # a demo user that ships with the PDI
    print(emp)
    if emp:
        t = create_ticket(emp["sys_id"], "PoC test from voice bot", "Created by servicenow_backend.py")
        print(t)
