#!/usr/bin/env python3
"""
Pull Canadian subscription data from Recurly.
Subscriptions embed account + plan + billing info — no separate accounts fetch needed.
"""

import os, json, time, base64, urllib.request, urllib.error
from datetime import datetime, timezone, timedelta
from collections import defaultdict

API_KEY  = os.getenv("RECURLY_API_KEY", "9e277f4e16a443a088bdd59a08f7b266")
BASE_URL = "https://v3.recurly.com"
AUTH     = base64.b64encode((API_KEY + ":").encode()).decode()
HEADERS  = {
    "Authorization": f"Basic {AUTH}",
    "Accept": "application/vnd.recurly.v2021-02-25+json",
}

def get(path, params=None):
    url = BASE_URL + path
    if params:
        url += "?" + "&".join(f"{k}={v}" for k, v in params.items())
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        print(f"  HTTP {e.code} {path}: {e.read().decode()[:200]}")
        return None

def paginate(path, params=None):
    params = dict(params or {}); params["limit"] = 200
    cursor = None; page = 0
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
        nxt = data.get("next")
        if not nxt:
            break
        cursor = nxt.split("cursor=")[-1].split("&")[0]
        time.sleep(0.05)

def is_canadian(sub):
    """Check billing country on the embedded account."""
    acct = sub.get("account") or {}
    # billing_info may be nested inside account
    bi = acct.get("billing_info") or {}
    addr = bi.get("address") or {}
    country = addr.get("country") or acct.get("address", {}).get("country", "")
    return country.upper() == "CA"

def arr(sub):
    unit     = sub.get("unit_amount") or 0
    qty      = sub.get("quantity") or 1
    plan     = sub.get("plan") or {}
    ilen     = plan.get("interval_length") or 1
    iunit    = (plan.get("interval_unit") or "months").lower()
    months   = ilen if "month" in iunit else (ilen * 12 if "year" in iunit else ilen / 30)
    return round((unit * qty / (months or 1)) * 12, 2)

# ── Active subscriptions ─────────────────────────────────────────────────────
print("\n[1] Active subscriptions...")
active_ca = [s for s in paginate("/subscriptions", {"state": "active"}) if is_canadian(s)]
print(f"  CA active: {len(active_ca)}")

# ── Cancelled/expired (last 12 months) ───────────────────────────────────────
cutoff = (datetime.now(timezone.utc) - timedelta(days=365)).strftime("%Y-%m-%dT%H:%M:%SZ")
churned_ca = []
for state in ("canceled", "expired"):
    print(f"\n[2] {state} subscriptions...")
    for s in paginate("/subscriptions", {"state": state}):
        ended = s.get("ended_at") or s.get("canceled_at") or ""
        if ended >= cutoff and is_canadian(s):
            s["_churn_state"] = state
            churned_ca.append(s)
print(f"  CA churned (last 12m): {len(churned_ca)}")

# ── Aggregate ─────────────────────────────────────────────────────────────────
arr_by_acct   = defaultdict(float)
name_by_acct  = {}
state_by_acct = {}
plans_by_acct = defaultdict(set)
arr_by_plan   = defaultdict(float)
cnt_by_plan   = defaultdict(int)
plan_names    = {}

for s in active_ca:
    acct      = s.get("account") or {}
    code      = acct.get("code") or acct.get("id", "").split("/")[-1]
    company   = acct.get("company") or f"{acct.get('first_name','')} {acct.get('last_name','')}".strip() or code
    bi        = acct.get("billing_info") or {}
    addr      = (bi.get("address") or acct.get("address") or {})
    region    = addr.get("region", "")
    plan      = s.get("plan") or {}
    pc        = plan.get("code", "unknown")
    pn        = plan.get("name", pc)
    a         = arr(s)

    arr_by_acct[code]   += a
    name_by_acct[code]   = company
    state_by_acct[code]  = region
    plans_by_acct[code].add(pn)
    arr_by_plan[pc]     += a
    cnt_by_plan[pc]     += 1
    plan_names[pc]       = pn

total_arr     = sum(arr_by_acct.values())
ca_active_cnt = len(active_ca)
ca_churn_cnt  = len(churned_ca)
churn_rate    = ca_churn_cnt / (ca_active_cnt + ca_churn_cnt) * 100 if (ca_active_cnt + ca_churn_cnt) else 0

top50 = sorted(arr_by_acct.items(), key=lambda x: x[1], reverse=True)[:50]

output = {
    "generated_at": datetime.now(timezone.utc).isoformat(),
    "summary": {
        "ca_active_subscriptions": ca_active_cnt,
        "total_ca_arr_usd": round(total_arr, 2),
        "ca_churned_last_12m": ca_churn_cnt,
        "ca_churn_rate_pct": round(churn_rate, 2),
    },
    "plan_breakdown": sorted([
        {"plan_code": pc, "plan_name": plan_names[pc],
         "subscriptions": cnt_by_plan[pc], "arr_usd": round(arr_by_plan[pc], 2)}
        for pc in arr_by_plan
    ], key=lambda x: x["arr_usd"], reverse=True),
    "top50_by_arr": [
        {"rank": i+1, "account_code": code, "name": name_by_acct[code],
         "province": state_by_acct.get(code, ""), "arr_usd": round(a, 2),
         "plans": list(plans_by_acct[code])}
        for i, (code, a) in enumerate(top50)
    ],
    "churned_last_12m": [
        {"account": (s.get("account") or {}).get("company") or (s.get("account") or {}).get("code"),
         "plan": (s.get("plan") or {}).get("name"),
         "state": s.get("_churn_state"),
         "ended_at": s.get("ended_at") or s.get("canceled_at")}
        for s in churned_ca
    ],
}

with open("/home/user/igprojects/recurly_canada_data.json", "w") as f:
    json.dump(output, f, indent=2)

print("\n── SUMMARY ──────────────────────────────────")
print(json.dumps(output["summary"], indent=2))
print("\n── PLAN BREAKDOWN ───────────────────────────")
for p in output["plan_breakdown"]:
    print(f"  {p['plan_name']:<40} {p['subscriptions']:>4} subs  ARR ${p['arr_usd']:>12,.2f}")
print("\n── TOP 10 BY ARR ────────────────────────────")
for c in output["top50_by_arr"][:10]:
    print(f"  {c['rank']:>2}. {c['name']:<40} {c['province']:<5}  ${c['arr_usd']:>10,.2f}  {', '.join(c['plans'])}")
print("\n✓ Written to recurly_canada_data.json")
