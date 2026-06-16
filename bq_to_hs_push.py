#!/usr/bin/env python3
# bq_to_hs_push.py
# VERSION: 0.2.0
# CHANGES FROM v0.1.0:
#   - Retry logic: 3 attempts with 5s wait on timeout/5xx errors
#   - Resume capability: saves progress after each batch, restarts from last batch on crash
#   - Removed account_name from CREATE payload (was causing ~58 validation errors)
#   - Skip emails with apostrophes (HubSpot rejects them as invalid format)
#   - Batch size increased from 100 to 200 (cuts runtime roughly in half)
#   - Progress file: hs_push_progress.json (auto-deleted on clean completion)
# MODES:
#   python3 bq_to_hs_push.py --test       Push 10 records, validate, save revert backup
#   python3 bq_to_hs_push.py --revert     Restore values from last test backup
#   python3 bq_to_hs_push.py --full       Full push (requires typing YES)
#   python3 bq_to_hs_push.py --full --resume  Resume from last saved batch

import os
import sys
import json
import time
import argparse
import requests
import pandas as pd
from datetime import datetime

# --- CONFIGURATION ---
HS_KEY_FILE = 'HS_Service_key.txt'
OBJECT_TYPE = '2-58523979'
PORTAL_ID = '5315820'
BQ_PROJECT = 'support-467322'
BQ_TABLE = 'wordly_usage_data.current_usage_clean'
REVERT_FILE = 'hs_push_revert_backup.json'
PROGRESS_FILE = 'hs_push_progress.json'
SLACK_KEY_FILE = 'slack_webhook.txt'
BATCH_SIZE = 200
MAX_RETRIES = 3
RETRY_WAIT = 5

FIELD_MAP = {
    'Owner_Name':            'owner_name',
    'Owner_Email':           'owner_email',
    'Service':               'service',
    'Consumed_Mins':         'consumed_minutes',
    'Available_Mins':        'available_minutes',
    'Last_Used':             'last_used',
    'Consumed_Last_7_Days':  'consumed_last_7',
    'Consumed_Last_30_Days': 'consumed_last_30_days',
}


def get_token():
    with open(HS_KEY_FILE, 'r') as f:
        return f.read().strip()


def send_slack(message, is_error=True):
    if not os.path.exists(SLACK_KEY_FILE):
        return
    with open(SLACK_KEY_FILE, 'r') as f:
        url = f.read().strip()
    prefix = "🚨 *HS PUSH ERROR:* " if is_error else "✅ *HS PUSH:* "
    try:
        requests.post(url, json={"text": f"{prefix}{message}"}, timeout=10)
    except:
        pass


def has_apostrophe(email):
    """HubSpot rejects emails with apostrophes — skip them."""
    return "'" in str(email)


def format_date(date_str):
    if not date_str or str(date_str) in ('nan', 'None', ''):
        return None
    try:
        return datetime.strptime(str(date_str).strip(), '%m/%d/%Y').strftime('%Y-%m-%d')
    except:
        try:
            datetime.strptime(str(date_str).strip(), '%Y-%m-%d')
            return str(date_str).strip()
        except:
            return None


def load_bq_data(limit=None):
    import pandas_gbq
    query = f"SELECT * FROM `{BQ_PROJECT}.{BQ_TABLE}`"
    if limit:
        query += f" LIMIT {limit}"
    print(f"   📡 Loading from BQ: {BQ_TABLE}")
    df = pandas_gbq.read_gbq(query, project_id=BQ_PROJECT)
    print(f"   ✅ Loaded {len(df)} rows from BQ")
    return df


def api_call_with_retry(fn, *args, **kwargs):
    """Wrap an API call with retry logic for timeouts and 5xx errors."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            result = fn(*args, **kwargs)
            return result
        except requests.exceptions.Timeout:
            if attempt < MAX_RETRIES:
                print(f"      ⏱️ Timeout on attempt {attempt}, retrying in {RETRY_WAIT}s...")
                time.sleep(RETRY_WAIT)
            else:
                raise
        except requests.exceptions.RequestException as e:
            if attempt < MAX_RETRIES:
                print(f"      ⚠️ Request error on attempt {attempt}: {e}, retrying in {RETRY_WAIT}s...")
                time.sleep(RETRY_WAIT)
            else:
                raise


def search_hs_record(token, email):
    headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
    payload = {
        "filterGroups": [{"filters": [{"propertyName": "owner_email", "operator": "EQ", "value": email}]}],
        "properties": list(FIELD_MAP.values()) + ["account_name"],
        "limit": 1
    }

    def do_search():
        return requests.post(
            f"https://api.hubapi.com/crm/v3/objects/{OBJECT_TYPE}/search",
            headers=headers,
            json=payload,
            timeout=20
        )

    r = api_call_with_retry(do_search)
    if r.status_code == 200:
        results = r.json().get('results', [])
        if results:
            return results[0]
    return None


def upsert_hs_record(token, email, properties):
    headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
    existing = search_hs_record(token, email)

    if existing:
        record_id = existing['id']

        def do_patch():
            return requests.patch(
                f"https://api.hubapi.com/crm/v3/objects/{OBJECT_TYPE}/{record_id}",
                headers=headers,
                json={"properties": properties},
                timeout=20
            )

        r = api_call_with_retry(do_patch)
        action = "UPDATED"
    else:
        # CREATE — no account_name to avoid validation errors
        def do_post():
            return requests.post(
                f"https://api.hubapi.com/crm/v3/objects/{OBJECT_TYPE}",
                headers=headers,
                json={"properties": properties},
                timeout=20
            )

        r = api_call_with_retry(do_post)
        action = "CREATED"

    if r.status_code in (200, 201):
        return action, r.json()['id'], None
    else:
        return "ERROR", None, f"{r.status_code}: {r.text[:200]}"


def build_properties(row):
    props = {}
    for bq_col, hs_field in FIELD_MAP.items():
        val = row.get(bq_col)
        if val is None or str(val) in ('nan', 'None', ''):
            continue
        if hs_field == 'last_used':
            formatted = format_date(val)
            if formatted:
                props[hs_field] = formatted
        elif hs_field in ('consumed_minutes', 'available_minutes', 'consumed_last_7', 'consumed_last_30_days'):
            try:
                props[hs_field] = int(float(val))
            except:
                pass
        else:
            props[hs_field] = str(val)
    return props


def save_progress(batch_num, total_updated, total_created, total_errors, total_skipped):
    with open(PROGRESS_FILE, 'w') as f:
        json.dump({
            'batch_num': batch_num,
            'total_updated': total_updated,
            'total_created': total_created,
            'total_errors': total_errors,
            'total_skipped': total_skipped,
            'timestamp': datetime.now().isoformat()
        }, f)


def load_progress():
    if os.path.exists(PROGRESS_FILE):
        with open(PROGRESS_FILE, 'r') as f:
            return json.load(f)
    return None


def run_test(token, df):
    print(f"\n{'='*60}")
    print("TEST MODE — 10 records only")
    print(f"{'='*60}\n")

    sample = df.head(10)
    backup = []
    results = []

    for _, row in sample.iterrows():
        email = str(row.get('Owner_Email', '')).lower().strip()
        if not email or has_apostrophe(email):
            continue

        existing = search_hs_record(token, email)
        backup.append({
            'email': email,
            'record_id': existing['id'] if existing else None,
            'previous_properties': existing['properties'] if existing else None,
            'was_existing': existing is not None
        })

        props = build_properties(row.to_dict())
        action, record_id, error = upsert_hs_record(token, email, props)
        results.append({'email': email, 'action': action, 'record_id': record_id, 'error': error, 'props_sent': props})

        status = "✅" if action != "ERROR" else "❌"
        print(f"   {status} {action}: {email} (ID: {record_id or 'N/A'})")
        if error:
            print(f"      Error: {error}")
        time.sleep(0.15)

    with open(REVERT_FILE, 'w') as f:
        json.dump(backup, f, indent=2, default=str)
    print(f"\n💾 Revert backup saved to {REVERT_FILE}")

    print(f"\n{'='*60}")
    print("VALIDATION — reading back pushed records")
    print(f"{'='*60}\n")

    mismatches = 0
    for result in results:
        if result['action'] == 'ERROR':
            continue
        record = search_hs_record(token, result['email'])
        if not record:
            print(f"   ❌ {result['email']} — could not read back")
            mismatches += 1
            continue
        hs_props = record['properties']
        sent = result['props_sent']
        match = True
        for hs_field, sent_val in sent.items():
            hs_val = hs_props.get(hs_field)
            if str(sent_val) != str(hs_val):
                if hs_field not in ('owner_name', 'owner_email', 'service'):
                    print(f"   ⚠️  {result['email']} — {hs_field}: sent={sent_val}, got={hs_val}")
                    match = False
                    mismatches += 1
        if match:
            print(f"   ✅ {result['email']} — all fields match")

    print(f"\n{'='*60}")
    print(f"TEST COMPLETE — {len(results)} records, {mismatches} mismatches")
    print(f"Revert: python3 bq_to_hs_push.py --revert")
    print(f"{'='*60}\n")
    send_slack(f"Test push complete. {len(results)} records. {mismatches} mismatches.", is_error=False)


def run_revert(token):
    if not os.path.exists(REVERT_FILE):
        print("❌ No revert backup found.")
        return

    with open(REVERT_FILE, 'r') as f:
        backup = json.load(f)

    print(f"\n{'='*60}")
    print(f"REVERT MODE — restoring {len(backup)} records")
    print(f"{'='*60}\n")

    headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}

    for entry in backup:
        email = entry['email']
        record_id = entry['record_id']
        prev_props = entry['previous_properties']
        was_existing = entry['was_existing']

        if not was_existing:
            existing = search_hs_record(token, email)
            if existing:
                requests.delete(
                    f"https://api.hubapi.com/crm/v3/objects/{OBJECT_TYPE}/{existing['id']}",
                    headers=headers, timeout=20
                )
                print(f"   🗑️  DELETED: {email}")
        else:
            restore_props = {k: v for k, v in prev_props.items()
                             if not k.startswith('hs_') and v is not None and str(v) not in ('None', 'nan', '')}
            if restore_props and record_id:
                requests.patch(
                    f"https://api.hubapi.com/crm/v3/objects/{OBJECT_TYPE}/{record_id}",
                    headers=headers,
                    json={"properties": restore_props},
                    timeout=20
                )
                print(f"   ✅ RESTORED: {email}")
        time.sleep(0.15)

    print(f"\n✅ Revert complete.")
    os.rename(REVERT_FILE, REVERT_FILE.replace('.json', '_used.json'))


def run_full(token, df, resume=False):
    start_batch = 1
    total_updated = 0
    total_created = 0
    total_errors = 0
    total_skipped = 0

    if resume:
        progress = load_progress()
        if progress:
            start_batch = progress['batch_num'] + 1
            total_updated = progress['total_updated']
            total_created = progress['total_created']
            total_errors = progress['total_errors']
            total_skipped = progress['total_skipped']
            print(f"   ▶️  Resuming from batch {start_batch} (previous totals: Updated={total_updated}, Created={total_created}, Errors={total_errors})")
        else:
            print("   ⚠️  No progress file found — starting from beginning")

    total_records = len(df)
    print(f"\n{'='*60}")
    print(f"FULL PUSH — {total_records} records in batches of {BATCH_SIZE}")
    if start_batch > 1:
        start_record = (start_batch - 1) * BATCH_SIZE
        print(f"Starting from batch {start_batch} (record {start_record + 1})")
    print(f"{'='*60}\n")

    batch_num = 0
    for i in range(0, total_records, BATCH_SIZE):
        batch_num += 1
        if batch_num < start_batch:
            continue

        batch = df.iloc[i:i + BATCH_SIZE]
        batch_start = i + 1
        batch_end = min(i + BATCH_SIZE, total_records)
        print(f"   📦 Batch {batch_num} ({batch_start}-{batch_end} of {total_records})")

        for _, row in batch.iterrows():
            email = str(row.get('Owner_Email', '')).lower().strip()
            if not email:
                total_skipped += 1
                continue
            if has_apostrophe(email):
                total_skipped += 1
                continue

            props = build_properties(row.to_dict())
            try:
                action, record_id, error = upsert_hs_record(token, email, props)
                if action == 'UPDATED':
                    total_updated += 1
                elif action == 'CREATED':
                    total_created += 1
                else:
                    total_errors += 1
                    print(f"      ❌ {email}: {error}")
            except Exception as e:
                total_errors += 1
                print(f"      ❌ {email}: Exception — {e}")

            time.sleep(0.1)

        print(f"      ✅ Batch {batch_num} done. Totals — Updated: {total_updated}, Created: {total_created}, Skipped: {total_skipped}, Errors: {total_errors}")
        save_progress(batch_num, total_updated, total_created, total_errors, total_skipped)
        time.sleep(0.5)

    if os.path.exists(PROGRESS_FILE):
        os.remove(PROGRESS_FILE)

    summary = f"Full push complete. Updated: {total_updated}, Created: {total_created}, Skipped: {total_skipped}, Errors: {total_errors}"
    print(f"\n🎉 {summary}")
    send_slack(summary, is_error=total_errors > 0)


def main():
    parser = argparse.ArgumentParser(description='BQ to HubSpot push v0.2.0')
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--test', action='store_true')
    group.add_argument('--revert', action='store_true')
    group.add_argument('--full', action='store_true')
    parser.add_argument('--resume', action='store_true', help='Resume full push from last saved batch')
    args = parser.parse_args()

    token = get_token()

    if args.revert:
        run_revert(token)
        return

    print(f"📡 Loading BQ data...")
    df = load_bq_data(limit=10 if args.test else None)

    if args.test:
        run_test(token, df)
    elif args.full:
        if not args.resume:
            confirm = input(f"\n⚠️  This will push {len(df)} records to HubSpot. Type YES to confirm: ")
            if confirm.strip() != 'YES':
                print("Aborted.")
                return
        run_full(token, df, resume=args.resume)


if __name__ == '__main__':
    main()
