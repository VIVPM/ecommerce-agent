# Grafana Cloud provisioning

Reproducible setup for this app's Grafana observability — a dashboard, two alert
rules, an email contact point, and a notification route — all built on the
`chat_messages_total` counter that `app/observability.py` exports.

(The leads-coordinator built the same set directly via the API from throwaway
temp files; this keeps it in the repo instead.)

## Files
- `dashboard.json` — "E-commerce Agent — Overview", 4 panels (message rate by
  status, success rate, errors, messages by tool). Importable in the UI as-is
  (Dashboards → Import → paste), or pushed by the script.
- `provision.py` — creates the folder, dashboard, `ecommerce-agent-email`
  contact point, two alert rules, and appends a notification route.

## Apply
Needs these in `backend/app/.env` (already set):
`GRAFANA_URL`, `GRAFANA_API_TOKEN` (a `glsa_` service-account token — **not** the
OTLP push auth), `GRAFANA_ALERT_EMAIL`, `GRAFANA_PROM_UID` (default
`grafanacloud-prom`).

```bash
cd backend
python grafana/provision.py --dry-run   # print every payload, send nothing
python grafana/provision.py --apply      # create it all in Grafana
```

## Alerts — provisioned but SILENT
Same rules as the coordinator, but muted so they never email (the "no messages"
rule is noisy for a low-traffic demo). `provision.py` sets:
- `CREATE_ALERTS = True` — the two rules + email contact point + route are created.
- `MUTE_ALERTS = True` — an always-on mute timing is attached to the route, so the
  rules still evaluate and show in the UI but send no notifications.

The rules:
- **no messages (30m)** — fires when nothing is processed in 30m or the series is absent.
- **message errors** — fires when any message fails (`status="error"`) in 10m.

To actually receive emails, set `MUTE_ALERTS = False` and re-run `--apply`.
To tear the whole thing down (rules + mute timing + route + contact point):
```bash
python grafana/provision.py --remove-alerts   # dashboard stays
```
`--apply` is idempotent — existing rules/contact point are skipped, so re-running
only adds/updates the mute.

## Shared-stack safety
This Grafana stack is shared with the leads-coordinator. Folder, dashboard,
contact point and alert rules are all additive. The notification policy is the
one shared object, and it's read-modify-**write**: the script fetches the existing
tree and appends our route (idempotently), never replacing it. `--dry-run` shows
exactly what it will do first.
