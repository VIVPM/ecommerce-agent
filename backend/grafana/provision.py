"""Provision this app's Grafana Cloud observability: a dashboard, an email
contact point, two alert rules, and a notification route — the same set the
leads-coordinator built (there it was done from throwaway temp files; here it's
a reproducible script committed to the repo).

Everything keys off the chat_messages_total counter (see app/observability.py):
  * Alert "no messages" — nothing processed in 30m (a stalled/dead backend).
  * Alert "errors"      — any message failed in the last 10m.

Reads from env (backend/app/.env or the shell):
  GRAFANA_URL          e.g. https://calmcarriage2405.grafana.net
  GRAFANA_API_TOKEN    a Grafana service-account token (glsa_...), NOT the OTLP push token
  GRAFANA_ALERT_EMAIL  where alerts are emailed
  GRAFANA_PROM_UID     Prometheus datasource uid (default: grafanacloud-prom)

Usage:
  python -m app... no — run from backend/:  python grafana/provision.py --dry-run
  python grafana/provision.py --apply       # actually creates everything

SAFETY: this Grafana stack is shared with the leads-coordinator. Folder,
dashboard, contact point and alert rules are all ADDITIVE. The notification
policy is the one shared object; it is read-modify-WRITE — the existing tree is
fetched and our route is appended (idempotently), never replaced. --dry-run
prints the exact modified tree so you can review before --apply.
"""
import json
import os
import ssl
import sys
import urllib.request
import urllib.error
from pathlib import Path

from dotenv import load_dotenv

# The base Python's system CA store can carry an expired root (the urllib default),
# even though `requests`-based clients work — they bundle an up-to-date certifi.
# Point urllib at certifi too so HTTPS to grafana.net verifies. Falls back to the
# default context if certifi isn't present.
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

# The alert rules are provisioned (same as the coordinator), but MUTE_ALERTS keeps
# them SILENT: an always-on mute timing is attached to the notification route, so
# the rules still evaluate and show in the UI but never email. Set MUTE_ALERTS=False
# to actually notify. CREATE_ALERTS=False would skip the rules entirely (dashboard
# only). To tear the alerting down: python grafana/provision.py --remove-alerts
CREATE_ALERTS = True
MUTE_ALERTS = True

# An interval with only times 00:00–24:00 and no day/month limits matches ALL the
# time — i.e. permanently muted.
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
    """A Grafana provisioning alert rule: query A -> threshold C.

    `evaluator` is the condition on A, e.g. ("gt", [0]) fires when A > 0,
    ("lt", [1]) fires when A < 1 (i.e. ~zero — used for the 'no messages' rule)."""
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
    # Fires when the 30m message count is ~0 (A < 1) OR the series is absent
    # (noData=Alerting) — i.e. quiet or backend down. Noisy for a LOW-TRAFFIC
    # demo (fires overnight); tune the window or disable if that's you.
    alert_rule(
        "E-commerce Agent — no messages (30m)",
        "sum(increase(chat_messages_total[30m]))",
        "0m", "Alerting",
        "No chat messages processed in 30m — the backend may be down or unreachable.",
        "warning", ("lt", [1]),
    ),
    # Fires when any message failed in the last 10m (A > 0). No data = no errors = OK.
    alert_rule(
        "E-commerce Agent — message errors",
        'sum(increase(chat_messages_total{status="error"}[10m]))',
        "0m", "OK",
        "Chat messages are failing (status=error) in the last 10m.",
        "critical", ("gt", [0]),
    ),
]


def ensure_route(tree):
    """Ensure our route exists AND carries the mute timing when MUTE_ALERTS.
    Read-modify-write on the shared tree — only our route is touched, idempotently."""
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
    """Undo the email alerting already created: delete the two alert rules, drop
    our notification route, then delete the contact point (in that order — Grafana
    won't delete a contact point a policy still references). The dashboard stays."""
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
