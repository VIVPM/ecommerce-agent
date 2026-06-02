"""Create this app's Grafana dashboard, alert rules and email contact point.

Everything keys off the chat_messages_total counter (see app/observability.py).
Needs GRAFANA_URL, GRAFANA_API_TOKEN (a glsa_ service-account token, not the OTLP
push auth), GRAFANA_ALERT_EMAIL and GRAFANA_PROM_UID in backend/app/.env.

    python grafana/provision.py --dry-run        # print payloads, send nothing
    python grafana/provision.py --apply
    python grafana/provision.py --remove-alerts

The stack is shared with another project, so every write is additive and the
notification policy is read-modify-write.
"""
import json
import os
import ssl
import sys
import urllib.request
import urllib.error
from pathlib import Path

from dotenv import load_dotenv

# The system CA store here has an expired root, so urllib fails where requests
# (which bundles certifi) works. Point urllib at certifi too.
try:
    import certifi
    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except Exception:
    _SSL_CTX = ssl.create_default_context()

load_dotenv(Path(__file__).resolve().parent.parent / "app" / ".env")

URL = (os.getenv("GRAFANA_URL") or "").rstrip("/")
TOKEN = os.getenv("GRAFANA_API_TOKEN") or ""
EMAIL = os.getenv("GRAFANA_ALERT_EMAIL") or ""
PROM_UID = os.getenv("GRAFANA_PROM_UID", "grafanacloud-prom")

FOLDER_UID = "ecommerce-agent"
FOLDER_TITLE = "E-commerce Agent"
CONTACT_POINT = "ecommerce-agent-email"
RULE_GROUP = "ecommerce-agent"
MUTE_NAME = "ecommerce-agent-muted"

# MUTE_ALERTS attaches an always-on mute timing to the route: the rules still
# evaluate and show in the UI, but never email. False to actually notify.
CREATE_ALERTS = True
MUTE_ALERTS = True

# 00:00-24:00 with no day/month limit matches always, i.e. permanently muted.
MUTE_TIMING = {"name": MUTE_NAME,
               "time_intervals": [{"times": [{"start_time": "00:00", "end_time": "24:00"}]}]}

DRY = "--apply" not in sys.argv and "--remove-alerts" not in sys.argv
REMOVE = "--remove-alerts" in sys.argv

HERE = Path(__file__).resolve().parent


def _req(method, path, body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    h = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
    h.update(headers or {})
    req = urllib.request.Request(f"{URL}{path}", data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=30, context=_SSL_CTX) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:300]


def _show(label, method, path, body):
    print(f"\n### {label}  ({method} {path})")
    print(json.dumps(body, indent=2) if body is not None else "(no body)")


def alert_rule(title, expr, for_dur, no_data, summary, severity, evaluator):
    """Build an alert rule: query A, thresholded by C.

    `evaluator` is the condition on A, e.g. ("gt", [0]) or ("lt", [1]).
    """
    ev_type, ev_params = evaluator
    return {
        "title": title,
        "ruleGroup": RULE_GROUP,
        "folderUID": FOLDER_UID,
        "condition": "C",
        "for": for_dur,
        "orgID": 1,
        "noDataState": no_data,
        "execErrState": "Error",
        "labels": {"service": "ecommerce-agent", "severity": severity},
        "annotations": {"summary": summary},
        "data": [
            {
                "refId": "A",
                "relativeTimeRange": {"from": 1800, "to": 0},
                "datasourceUid": PROM_UID,
                "model": {"refId": "A", "instant": True, "expr": expr},
            },
            {
                "refId": "C",
                "datasourceUid": "__expr__",
                "model": {
                    "refId": "C", "type": "threshold", "expression": "A",
                    "conditions": [{"evaluator": {"type": ev_type, "params": ev_params}}],
                },
            },
        ],
    }


DASHBOARD = json.loads((HERE / "dashboard.json").read_text(encoding="utf-8"))

FOLDER = {"uid": FOLDER_UID, "title": FOLDER_TITLE}

CONTACT = {
    "name": CONTACT_POINT,
    "type": "email",
    "settings": {"addresses": EMAIL, "singleEmail": True},
    "disableResolveMessage": False,
}

RULES = [
    # Fires when the 30m count is ~0 or the series is missing. Noisy on a
    # low-traffic demo, which is why it ships muted.
    alert_rule(
        "E-commerce Agent — no messages (30m)",
        "sum(increase(chat_messages_total[30m]))",
        "0m", "Alerting",
        "No chat messages processed in 30m — the backend may be down or unreachable.",
        "warning", ("lt", [1]),
    ),
    # Fires when any message failed in the last 10m.
    alert_rule(
        "E-commerce Agent — message errors",
        'sum(increase(chat_messages_total{status="error"}[10m]))',
        "0m", "OK",
        "Chat messages are failing (status=error) in the last 10m.",
        "critical", ("gt", [0]),
    ),
]


def ensure_route(tree):
    """Add or update our route in the shared policy tree, leaving other routes alone."""
    routes = tree.setdefault("routes", [])
    ours = next((r for r in routes if r.get("receiver") == CONTACT_POINT), None)
    changed = False
    if ours is None:
        ours = {"receiver": CONTACT_POINT,
                "object_matchers": [["service", "=", "ecommerce-agent"]],
                "continue": False}
        routes.append(ours)
        changed = True
    target = [MUTE_NAME] if MUTE_ALERTS else []
    if ours.get("mute_time_intervals", []) != target:
        if target:
            ours["mute_time_intervals"] = target
        else:
            ours.pop("mute_time_intervals", None)
        changed = True
    return tree, changed


def remove_alerting(prov):
    """Delete the alert rules, route, mute timing and contact point. Dashboard stays.

    Order matters: Grafana won't delete a contact point a policy still references.
    """
    st, rules = _req("GET", "/api/v1/provisioning/alert-rules")
    wanted = {r["title"] for r in RULES}
    if st < 300 and isinstance(rules, list):
        for rule in rules:
            if rule.get("title") in wanted and rule.get("uid"):
                s, _ = _req("DELETE", f"/api/v1/provisioning/alert-rules/{rule['uid']}", headers=prov)
                print(f"deleted alert '{rule['title']}': {s}")
    else:
        print(f"could not list alert rules ({st})")

    st, tree = _req("GET", "/api/v1/provisioning/policies")
    if st < 300 and isinstance(tree, dict):
        routes = tree.get("routes", []) or []
        kept = [r for r in routes if r.get("receiver") != CONTACT_POINT]
        if len(kept) != len(routes):
            tree["routes"] = kept
            s, _ = _req("PUT", "/api/v1/provisioning/policies", tree, prov)
            print(f"removed notification route: {s}")
        else:
            print("no notification route to remove.")

    # Mute timing — only after its route reference is gone.
    s, _ = _req("DELETE", f"/api/v1/provisioning/mute-timings/{MUTE_NAME}", headers=prov)
    print(f"deleted mute timing: {s}")

    st, cps = _req("GET", "/api/v1/provisioning/contact-points")
    if st < 300 and isinstance(cps, list):
        for cp in cps:
            if cp.get("name") == CONTACT_POINT and cp.get("uid"):
                s, body = _req("DELETE", f"/api/v1/provisioning/contact-points/{cp['uid']}", headers=prov)
                print(f"deleted contact point: {s}" + ("" if s < 300 else f" -> {body}"))
    print("\nEmail alerting removed. Dashboard left intact.")


def main():
    if not (URL and TOKEN):
        print("Set GRAFANA_URL and GRAFANA_API_TOKEN first "
              "(GRAFANA_API_TOKEN is a glsa_ service-account token, not the OTLP push auth).")
        sys.exit(1)

    prov = {"X-Disable-Provenance": "true"}  # so the created objects stay editable in the UI

    if REMOVE:
        remove_alerting(prov)
        return

    if DRY:
        print("DRY RUN — nothing sent. Re-run with --apply to create these.\n"
              f"Target: {URL}  (Prometheus uid: {PROM_UID})")
        _show("Folder", "POST", "/api/folders", FOLDER)
        _show("Dashboard", "POST", "/api/dashboards/db",
              {"dashboard": {**DASHBOARD, "id": None}, "folderUid": FOLDER_UID, "overwrite": True})
        if CREATE_ALERTS:
            _show("Contact point", "POST", "/api/v1/provisioning/contact-points", CONTACT)
            for r in RULES:
                _show(f"Alert rule: {r['title']}", "POST", "/api/v1/provisioning/alert-rules", r)
            if MUTE_ALERTS:
                _show("Mute timing (always-on)", "POST", "/api/v1/provisioning/mute-timings", MUTE_TIMING)
            print(f"\n### Route: receiver={CONTACT_POINT}, match service=ecommerce-agent"
                  + (f", mute_time_intervals=[{MUTE_NAME}]  (SILENT — evaluates, never emails)"
                     if MUTE_ALERTS else "  (LIVE — emails)"))
        else:
            print("\n### Alerts + contact point + route: SKIPPED (CREATE_ALERTS=False).")
        return

    # --- apply ---
    st, _ = _req("POST", "/api/folders", FOLDER)
    print(f"folder: {st}" + ("" if st < 300 else " (exists / see body)"))

    st, _ = _req("POST", "/api/dashboards/db",
                 {"dashboard": {**DASHBOARD, "id": None}, "folderUid": FOLDER_UID, "overwrite": True})
    print(f"dashboard: {st}")

    if not CREATE_ALERTS:
        print("alerts: skipped (CREATE_ALERTS=False — dashboard only).")
        print("\nDone. Grafana -> Dashboards -> 'E-commerce Agent — Overview'.")
        return

    # Contact point — idempotent (POST creates a duplicate each run otherwise).
    st, cps = _req("GET", "/api/v1/provisioning/contact-points")
    names = {c.get("name") for c in cps} if (st < 300 and isinstance(cps, list)) else set()
    if CONTACT_POINT in names:
        print("contact point: exists (skip)")
    else:
        s, _ = _req("POST", "/api/v1/provisioning/contact-points", CONTACT, prov)
        print(f"contact point: {s}")

    # Alert rules — idempotent by title (POST is NOT, it would duplicate).
    st, existing = _req("GET", "/api/v1/provisioning/alert-rules")
    have = {r["title"] for r in existing} if (st < 300 and isinstance(existing, list)) else set()
    for r in RULES:
        if r["title"] in have:
            print(f"alert '{r['title']}': exists (skip)")
        else:
            s, body = _req("POST", "/api/v1/provisioning/alert-rules", r, prov)
            print(f"alert '{r['title']}': {s}" + ("" if s < 300 else f" -> {body}"))

    # Mute timing — created before the route that references it.
    if MUTE_ALERTS:
        st, mts = _req("GET", "/api/v1/provisioning/mute-timings")
        mnames = {m.get("name") for m in mts} if (st < 300 and isinstance(mts, list)) else set()
        if MUTE_NAME in mnames:
            s, _ = _req("PUT", f"/api/v1/provisioning/mute-timings/{MUTE_NAME}", MUTE_TIMING, prov)
            print(f"mute timing: {s} (update)")
        else:
            s, body = _req("POST", "/api/v1/provisioning/mute-timings", MUTE_TIMING, prov)
            print(f"mute timing: {s}" + ("" if s < 300 else f" -> {body}"))

    # Route — find/create + attach (or clear) the mute.
    st, tree = _req("GET", "/api/v1/provisioning/policies")
    if st < 300 and isinstance(tree, dict):
        tree, changed = ensure_route(tree)
        if changed:
            s, body = _req("PUT", "/api/v1/provisioning/policies", tree, prov)
            print(f"route {'muted' if MUTE_ALERTS else 'set'}: {s}" + ("" if s < 300 else f" -> {body}"))
        else:
            print("route already correct — no change.")
    else:
        print(f"could not read notification policy ({st}).")

    tail = "MUTED — rules evaluate but won't email." if MUTE_ALERTS else "LIVE — rules will email."
    print(f"\nDone ({tail}). Grafana -> Dashboards -> 'E-commerce Agent — Overview'; Alerting -> Alert rules.")


if __name__ == "__main__":
    main()
