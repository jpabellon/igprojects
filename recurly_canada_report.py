#!/usr/bin/env python3
"""
Pull Canadian customer data from Recurly:
  - Active subscriptions billed to CA accounts
  - ARR per customer and total
  - Plan type breakdown
  - Churn: cancelled/expired subscriptions (last 12 months)
"""

import os
import json
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from collections import defaultdict

API_KEY = os.getenv("RECURLY_API_KEY", "9e277f4e16a443a088bdd59a08f7b266")
BASE_URL = "https://v3.recurly.com"
HEADERS = {
    "Authorization": f"Basic {__import__('base64').b64encode((API_KEY + ':').encode()).decode()}",
    "Accept": "application/vnd.recurly.v2021-02-25+json",
    "Content-Type": "application/json",
}

def get(path, params=None):
    """GET a Recurly endpoint, return parsed JSON."""
    url = BASE_URL + path
    if params:
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        url += "?" + qs
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        print(f"  HTTP {e.code} on {path}: {body[:200]}")
        return None

def paginate(path, params=None, limit=200):
    """Yield all records across paginated Recurly list endpoints."""
    params = dict(params or {})
    params["limit"] = limit
    cursor = None
    page = 0
    while True:
        if cursor:
            params["cursor"] = cursor
        data = get(path, params)
        if not data:
            break
        records = data.get("data", [])
        page += 1
        print(f"    page {page}: {len(records)} records")
        yield from records
        next_cursor = data.get("next", None)
        if not next_cursor:
            break
        # next is a full URL; extract cursor param
        if "cursor=" in next_cursor:
            cursor = next_cursor.split("cursor=")[-1].split("&")[0]
        else:
            break
        time.sleep(0.05)  # be gentle on rate limits


# ── 1. Pull all accounts with billing address in Canada ─────────────────────
print("\n[1] Fetching Canadian accounts...")
ca_accounts = {}  # account_code -> account dict
for acct in paginate("/accounts", {"country": "CA"}):
    code = acct.get("code") or acct.get("id")
    ca_accounts[code] = acct

print(f"  Total Canadian accounts: {len(ca_accounts)}")


# ── 2. Active subscriptions for Canadian accounts ────────────────────────────
print("\n[2] Fetching active subscriptions (state=active)...")
active_subs = []
for sub in paginate("/subscriptions", {"state": "active", "limit": 200}):
    acct_ref = sub.get("account", {})
    # account may be embedded or a link
    acct_code = acct_ref.get("code") or (acct_ref.get("id", "").split("/")[-1])
    if acct_code in ca_accounts:
        active_subs.append(sub)

print(f"  Active subscriptions linked to CA accounts: {len(active_subs)}")


# ── 3. If cross-ref above is sparse, also fetch subs directly per account ───
# (Recurly filter by country on /subscriptions may not be supported directly)
if len(active_subs) < 10:
    print("\n[2b] Fallback: fetching subs per CA account...")
    active_subs = []
    for i, (code, _) in enumerate(ca_accounts.items()):
        subs = get(f"/accounts/{code}/subscriptions", {"state": "active", "limit": 200})
        if subs and subs.get("data"):
            for s in subs["data"]:
                s["_account_code"] = code
                active_subs.append(s)
        if i > 0 and i % 50 == 0:
            print(f"    processed {i}/{len(ca_accounts)} accounts...")
            time.sleep(0.1)
    print(f"  Active CA subscriptions: {len(active_subs)}")


# ── 4. Compute ARR per subscription ─────────────────────────────────────────
def compute_arr(sub):
    """Convert subscription unit_amount + interval to annual recurring revenue."""
    unit = sub.get("unit_amount", 0) or 0
    qty  = sub.get("quantity", 1) or 1
    interval_len  = sub.get("plan", {}).get("interval_length", 1) or 1
    interval_unit = sub.get("plan", {}).get("interval_unit", "months") or "months"
    # months_per_billing_cycle
    if interval_unit in ("month", "months"):
        months = interval_len
    elif interval_unit in ("year", "years"):
        months = interval_len * 12
    elif interval_unit in ("day", "days"):
        months = interval_len / 30
    else:
        months = 1
    # ARR = (unit_amount * qty / months_per_cycle) * 12
    if months == 0:
        return 0
    arr = (unit * qty / months) * 12
    return round(arr, 2)


# ── 5. Aggregate active subscription data ───────────────────────────────────
arr_by_account  = defaultdict(float)   # account_code -> ARR
arr_by_plan     = defaultdict(float)   # plan_code -> ARR
count_by_plan   = defaultdict(int)
subs_by_account = defaultdict(list)
plan_details    = {}

for sub in active_subs:
    acct_code = sub.get("_account_code") or \
                sub.get("account", {}).get("code") or \
                (sub.get("account", {}).get("id", "").split("/")[-1])
    plan = sub.get("plan", {})
    plan_code = plan.get("code", "unknown")
    plan_name = plan.get("name", plan_code)
    plan_details[plan_code] = plan_name

    arr = compute_arr(sub)
    arr_by_account[acct_code]  += arr
    arr_by_plan[plan_code]     += arr
    count_by_plan[plan_code]   += 1
    subs_by_account[acct_code].append({
        "plan_code": plan_code,
        "plan_name": plan_name,
        "state": sub.get("state"),
        "arr": arr,
        "unit_amount": sub.get("unit_amount"),
        "currency": sub.get("currency", "USD"),
        "activated_at": sub.get("activated_at"),
    })

total_arr = sum(arr_by_account.values())
print(f"\n  Total CA ARR (active): ${total_arr:,.2f}")


# ── 6. Churned subscriptions (last 12 months) ────────────────────────────────
print("\n[3] Fetching churned subscriptions (state=expired/canceled, last 12m)...")
cutoff = (datetime.now(timezone.utc) - timedelta(days=365)).strftime("%Y-%m-%dT%H:%M:%SZ")

churned_ca = []
for state in ("canceled", "expired"):
    print(f"  state={state}...")
    for sub in paginate("/subscriptions", {"state": state, "ended_at": cutoff, "limit": 200}):
        acct_ref = sub.get("account", {})
        acct_code = acct_ref.get("code") or (acct_ref.get("id", "").split("/")[-1])
        if acct_code in ca_accounts:
            sub["_account_code"] = acct_code
            sub["_state"] = state
            churned_ca.append(sub)

# Also try per-account if global filter didn't work
if len(churned_ca) < 5:
    print("  Fallback: per-account churn check...")
    churned_ca = []
    for code in ca_accounts:
        for state in ("canceled", "expired"):
            subs = get(f"/accounts/{code}/subscriptions", {
                "state": state, "limit": 200
            })
            if subs and subs.get("data"):
                for s in subs["data"]:
                    ended = s.get("ended_at") or s.get("canceled_at") or ""
                    if ended >= cutoff:
                        s["_account_code"] = code
                        s["_state"] = state
                        churned_ca.append(s)
        time.sleep(0.02)

print(f"  Churned CA subscriptions (last 12m): {len(churned_ca)}")

# Global churn for comparison
print("\n[4] Fetching global churn count for comparison...")
global_churned = 0
for state in ("canceled", "expired"):
    data = get(f"/subscriptions", {"state": state, "limit": 1})
    if data:
        global_churned += data.get("total_records", 0) or len(data.get("data", []))

global_active_data = get("/subscriptions", {"state": "active", "limit": 1})
global_active_count = (global_active_data or {}).get("total_records", 0)

ca_active_count = len(active_subs)
ca_churned_count = len(churned_ca)
ca_churn_rate = (ca_churned_count / (ca_active_count + ca_churned_count) * 100) if (ca_active_count + ca_churned_count) > 0 else 0


# ── 7. Top 50 CA customers by ARR ────────────────────────────────────────────
top50 = sorted(arr_by_account.items(), key=lambda x: x[1], reverse=True)[:50]

top50_enriched = []
for code, arr in top50:
    acct = ca_accounts.get(code, {})
    name = acct.get("company") or f"{acct.get('first_name','')} {acct.get('last_name','')}".strip() or code
    billing = acct.get("billing_info", {}) or {}
    address = acct.get("address", {}) or {}
    state   = address.get("region") or billing.get("address", {}).get("region", "")
    plans   = [s["plan_name"] for s in subs_by_account.get(code, [])]
    top50_enriched.append({
        "code": code,
        "name": name,
        "state": state,
        "arr": arr,
        "plans": list(set(plans)),
    })


# ── 8. Output JSON for the report ────────────────────────────────────────────
output = {
    "generated_at": datetime.now(timezone.utc).isoformat(),
    "summary": {
        "total_ca_accounts": len(ca_accounts),
        "active_ca_subscriptions": ca_active_count,
        "total_ca_arr_usd": round(total_arr, 2),
        "churned_ca_last_12m": ca_churned_count,
        "ca_churn_rate_pct": round(ca_churn_rate, 2),
        "global_active_subscriptions": global_active_count,
        "global_churned_last_12m": global_churned,
    },
    "plan_breakdown": [
        {
            "plan_code": pc,
            "plan_name": plan_details.get(pc, pc),
            "subscription_count": count_by_plan[pc],
            "arr_usd": round(arr_by_plan[pc], 2),
        }
        for pc, _ in sorted(arr_by_plan.items(), key=lambda x: x[1], reverse=True)
    ],
    "top50_by_arr": top50_enriched,
    "churned_sample": [
        {
            "account_code": s.get("_account_code"),
            "plan": s.get("plan", {}).get("name"),
            "state": s.get("_state"),
            "ended_at": s.get("ended_at") or s.get("canceled_at"),
        }
        for s in churned_ca[:50]
    ],
}

out_path = "/home/user/igprojects/recurly_canada_data.json"
with open(out_path, "w") as f:
    json.dump(output, f, indent=2)

print(f"\n✓ Results written to {out_path}")
print(json.dumps(output["summary"], indent=2))
print("\nPlan breakdown:")
for p in output["plan_breakdown"][:10]:
    print(f"  {p['plan_name']:<35} {p['subscription_count']:>4} subs   ARR ${p['arr_usd']:>12,.2f}")
print("\nTop 10 by ARR:")
for i, c in enumerate(output["top50_by_arr"][:10], 1):
    print(f"  {i:>2}. {c['name']:<40} {c['state']:<15}  ARR ${c['arr']:>10,.2f}  Plans: {', '.join(c['plans'])}")
