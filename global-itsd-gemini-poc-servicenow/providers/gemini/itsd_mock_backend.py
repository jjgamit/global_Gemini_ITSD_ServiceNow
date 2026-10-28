"""Simulated Global IT Service Desk backend for the POC.

All employees, approved-KB examples, self-service outcomes, tickets, and regional
routing labels are mock data. No corporate directory, identity provider, account,
ServiceNow instance, or live queue is accessed or changed.
"""
from datetime import datetime, timezone
from itertools import count

MOCK_EMPLOYEES = {
    "EMP1001": {
        "employee_id": "EMP1001", "first_name": "Pards", "last_name": "Saludo", "full_name": "Pards Saludo", "email": "jbsaludo@trends.com.ph", "department": "Finance", "region": "APAC",
        "account_status": "LOCKED", "data_source": "SIMULATED",
    },
    "EMP1002": {
        "employee_id": "EMP1002", "first_name": "Maria", "last_name": "Reyes", "full_name": "Maria Reyes", "email": "maria.reyes@example.com", "department": "Human Resources", "region": "EMEA",
        "account_status": "ACTIVE", "data_source": "SIMULATED",
    },
    "EMP1003": {
        "employee_id": "EMP1003", "first_name": "Daniel", "last_name": "Cruz", "full_name": "Daniel Cruz", "email": "daniel.cruz@example.com", "department": "Technology", "region": "AMER",
        "account_status": "ACTIVE", "data_source": "SIMULATED",
    },
}

# Example articles labelled as approved-for-POC. Replace with actual approved SNOW KB
# article IDs and wording before any production use.
MOCK_KB = [
    {"article_id": "KB-POC-001", "title": "Password reset using the approved self-service portal", "category": "password_reset", "keywords": ["password", "reset", "forgot", "expired", "change password"], "steps": ["Open the organization's approved password self-service portal from the corporate bookmark or intranet.", "Choose the password reset option and follow the portal's identity-verification process.", "Set a new password that meets the displayed policy, then sign in again.", "If the portal rejects the request or is unavailable, stop and contact the IT Service Desk."], "safety_note": "The assistant must never ask the caller to speak or send a password, OTP, MFA code, or recovery code."},
    {"article_id": "KB-POC-002", "title": "Unlocking a locked employee account", "category": "account_lockout", "keywords": ["locked", "lockout", "unlock", "cannot login", "sign in", "too many attempts"], "steps": ["Stop repeated sign-in attempts to avoid extending the lockout.", "Use the approved account-unlock or password self-service portal if available to you.", "Complete the portal's verification steps yourself; do not share verification codes with the voice assistant.", "If self-service is unavailable or the account remains locked, ask the IT Service Desk for assistance."], "safety_note": "This mock flow does not unlock any real account or verify identity."},
    {"article_id": "KB-POC-003", "title": "Requesting access to a business application", "category": "access_request", "keywords": ["access", "permission", "application", "role", "entitlement", "request access"], "steps": ["Confirm the application name and the business reason for access.", "Submit an access request through the approved ServiceNow catalog or access portal.", "Select the appropriate access role and provide the required business justification.", "Wait for the required manager or application-owner approval before access is granted."], "safety_note": "The POC can create only a simulated request; it does not grant access or submit a real request."},
]

MOCK_REGIONAL_ROUTES = {
    "APAC": {"routing_target": "Global ITSD - APAC", "region": "APAC", "queue_id": "", "data_source": "SIMULATED"},
    "EMEA": {"routing_target": "Global ITSD - EMEA", "region": "EMEA", "queue_id": "", "data_source": "SIMULATED"},
    "AMER": {"routing_target": "Global ITSD - AMER", "region": "AMER", "queue_id": "", "data_source": "SIMULATED"},
}
_MOCK_TICKETS = []
_ticket_sequence = count(1)


def lookup_employee(employee_id: str) -> dict:
    normalized_id = str(employee_id or "").strip().upper()
    employee = MOCK_EMPLOYEES.get(normalized_id)
    if employee is None:
        return {"found": False, "employee_id": normalized_id, "message": "No matching employee in the simulated demo dataset.", "data_source": "SIMULATED"}
    return {"found": True, "employee": employee.copy(), "message": "Mock employee record found. This is not identity verification.", "data_source": "SIMULATED"}


def search_kb(query: str, category: str = "general") -> dict:
    terms = set(str(query or "").lower().replace("-", " ").split())
    category = str(category or "general").lower().strip()
    ranked = []
    for article in MOCK_KB:
        if category not in ("", "general") and article["category"] != category:
            continue
        haystack = " ".join([article["title"], article["category"], *article["keywords"]]).lower()
        score = sum(1 for term in terms if len(term) > 2 and term in haystack)
        if score or category == article["category"]:
            ranked.append((score, article))
    ranked.sort(key=lambda item: item[0], reverse=True)
    articles = [{k: v for k, v in article.items() if k != "keywords"} for score, article in ranked[:3]]
    return {"found": bool(articles), "articles": articles, "count": len(articles), "data_source": "SIMULATED_APPROVED_KB_FOR_POC", "note": "Mock KB content only; replace with actual approved ServiceNow knowledge articles before production."}


def run_self_service(employee_id: str, action: str, application: str = "", resolved=None) -> dict:
    action = str(action or "").lower().strip()
    employee_id = str(employee_id or "").strip().upper()
    action_to_category = {"password_reset": "password_reset", "account_unlock": "account_lockout", "access_request": "access_request"}
    if action not in action_to_category:
        return {"ok": False, "message": "Unsupported mock self-service action.", "data_source": "SIMULATED"}
    kb = next(a for a in MOCK_KB if a["category"] == action_to_category[action])
    if action == "access_request" and not str(application or "").strip():
        return {"ok": False, "needs_application": True, "message": "Ask which application access is needed before continuing.", "kb_article_id": kb["article_id"], "data_source": "SIMULATED"}
    outcome = "resolved" if resolved is True else ("not_resolved" if resolved is False else "guidance_provided")
    return {"ok": True, "action": action, "employee_id": employee_id, "application": str(application or "").strip(), "kb_article_id": kb["article_id"], "kb_title": kb["title"], "steps": kb["steps"], "outcome": outcome, "resolved": resolved if isinstance(resolved, bool) else None, "real_system_changed": False, "data_source": "SIMULATED"}


def create_mock_ticket(record_type: str, employee_id: str, region: str, short_description: str, description: str, category: str = "general", kb_article_id: str = "") -> dict:
    record_type = str(record_type or "incident").lower().strip()
    if record_type not in ("incident", "request"):
        record_type = "incident"
    seq = next(_ticket_sequence)
    prefix = "INC-POC-" if record_type == "incident" else "REQ-POC-"
    ticket = {"number": f"{prefix}{seq:05d}", "record_type": record_type, "employee_id": str(employee_id or "").strip().upper(), "region": str(region or "").strip().upper(), "short_description": str(short_description or "")[:160], "description": str(description or "")[:1500], "category": str(category or "general")[:80], "kb_article_id": str(kb_article_id or "")[:40], "state": "NEW - SIMULATED", "created_at_utc": datetime.now(timezone.utc).isoformat(), "data_source": "SIMULATED", "real_servicenow_record_created": False}
    _MOCK_TICKETS.append(ticket)
    return {"created": True, "ticket": ticket.copy(), "note": "POC record exists only in this running process memory. No real ServiceNow incident/request was created."}


def get_regional_route(region: str) -> dict:
    normalized = str(region or "").strip().upper()
    route = MOCK_REGIONAL_ROUTES.get(normalized)
    if not route:
        return {"found": False, "region": normalized, "message": "No mock regional route configured. Ask the caller or use the configured ITSD fallback process.", "data_source": "SIMULATED"}
    return {"found": True, **route, "note": "Routing label only. Actual live call routing must be configured and tested in Avaya Infinity."}


def list_mock_tickets() -> list:
    """Return current process-local demo tickets (for tests only)."""
    return [ticket.copy() for ticket in _MOCK_TICKETS]
