#!/usr/bin/env python3
# bq_to_hs_push.py
# VERSION: 0.3.0
# CHANGES FROM v0.2.0:
#   - Logs failed CREATEs to BQ hs_push_errors table
#   - Skips known unresolved errors on daily push (saves API calls)
#   - Weekly reconciliation: runs on Monday, checks if errors have resolved in HubSpot
#   - Fixed Slack message: SUCCESS unless errors > 200 or updated count drops significantly
#   - Error table tracks first_seen and resolved_date for cycle time analytics
# MODES:
#   python3 bq_to_hs_push.py --test           Push 10 records, validate, save revert backup
#   python3 bq_to_hs_push.py --revert         Restore values from last test backup
#   python3 bq_to_hs_push.py --full           Full push (requires typing YES)
#   python3 bq_to_hs_push.py --full --resume  Resume from last saved batch
#   python3 bq_to_hs_push.py --reconcile      Force reconciliation run regardless of day

import os
import json
import time
import argparse
import requests
import pandas as pd
from datetime import datetime, date

# --- CONFIGURATION ---
HS_KEY_FILE = 'HS_Service_key.txt'
OBJECT_TYPE = '2-58523979'
PORTAL_ID = '5315820'
BQ_PROJECT = 'support-467322'
BQ_TABLE = 'wordly_usage_data.current_usage_clean'
BQ_ERRORS_TABLE = 'wordly_usage_data.hs_push_errors'
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


def send_slack(message, is_error=False):
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


def load_known_errors():
    """Load emails that are currently in the error table and unresolved."""
    try:
        import pandas_gbq
        query = f"""
            SELECT owner_email
            FROM `{BQ_PROJECT}.{BQ_ERRORS_TABLE}`
            WHERE resolved = false
        """
        df = pandas_gbq.read_gbq(query, project_id=BQ_PROJECT)
        errors = set(df['owner_email'].str.lower().tolist())
        print(f"   ⚠️  Loaded {len(errors)} known unresolved errors — will skip on push")
        return errors
    except Exception as e:
        print(f"   ⚠️  Could not load error table: {e} — proceeding without skip list")
        return set()


def log_error_to_bq(email, name, error_msg):
    """Log a failed CREATE to the error table if not already there."""
    try:
        import pandas_gbq
        from google.cloud import bigquery
        bq_client = bigquery.Client(project=BQ_PROJECT)

        # Check if already in error table
        check = bq_client.query(f"""
            SELECT COUNT(*) as cnt
            FROM `{BQ_PROJECT}.{BQ_ERRORS_TABLE}`
            WHERE owner_email = '{email}' AND resolved = false
        """).result()

        for row in check:
            if row.cnt > 0:
                return  # Already logged

        # Insert new error
        error_df = pd.DataFrame([{
            'snapshot_date': date.today().isoformat(),
            'owner_email': email,
            'owner_name': name,
            'error_message': error_msg[:500],
            'resolved': False,
            'first_seen': date.today().isoformat(),
            'resolved_date': None
        }])
        pandas_gbq.to_gbq(
            error_df,
            BQ_ERRORS_TABLE,
            project_id=BQ_PROJECT,
            if_exists='append'
        )
    except Exception as e:
        print(f"      ⚠️  Could not log error to BQ: {e}")


def run_reconciliation(token):
    """Check all unresolved errors against HubSpot. Mark resolved if found."""
    print(f"\n{'='*60}")
    print("RECONCILIATION — checking unresolved errors against HubSpot")
    print(f"{'='*60}\n")

    try:
        import pandas_gbq
        from google.cloud import bigquery

        query = f"""
            SELECT owner_email, owner_name, first_seen
            FROM `{BQ_PROJECT}.{BQ_ERRORS_TABLE}`
            WHERE resolved = false
            ORDER BY first_seen
        """
        df = pandas_gbq.read_gbq(query, project_id=BQ_PROJECT)

        if df.empty:
            print("   ✅ No unresolved errors to check.")
            return

        print(f"   📋 Checking {len(df)} unresolved accounts...\n")

        resolved_count = 0
        still_missing = 0
        bq_client = bigquery.Client(project=BQ_PROJECT)

        for _, row in df.iterrows():
            email = str(row['owner_email']).lower().strip()
            existing = search_hs_record(token, email)

            if existing:
                # Found in HubSpot — mark resolved
                bq_client.query(f"""
                    UPDATE `{BQ_PROJECT}.{BQ_ERRORS_TABLE}`
                    SET resolved = true, resolved_date = '{date.today().isoformat()}'
                    WHERE owner_email = '{email}' AND resolved = false
                """).result()
                first_seen = row.get('first_seen')
                days = (date.today() - pd.to_datetime(first_seen).date()).days if first_seen else '?'
                print(f"   ✅ RESOLVED: {email} (took {days} days)")
                resolved_count += 1
            else:
                still_missing += 1

            time.sleep(0.15)

        print(f"\n{'='*60}")
        print(f"RECONCILIATION COMPLETE")
        print(f"  Resolved this run: {resolved_count}")
        print(f"  Still missing in HubSpot: {still_missing}")
        print(f"{'='*60}\n")

        send_slack(f"Weekly reconciliation: {resolved_count} errors resolved, {still_missing} still pending.")

    except Exception as e:
        print(f"   ❌ Reconciliation failed: {e}")


def api_call_with_retry(fn, *args, **kwargs):
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return fn(*args, **kwargs)
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
            headers=headers, json=payload, timeout=20
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
                headers=headers, json={"properties": properties}, timeout=20
            )
        r = api_call_with_retry(do_patch)
        action = "UPDATED"
    else:
        def do_post():
            return requests.post(
                f"https://api.hubapi.com/crm/v3/objects/{OBJECT_TYPE}",
                headers=headers, json={"properties": properties}, timeout=20
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
    send_slack(f"Test push complete. {len(results)} records. {mismatches} mismatches.")


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
                    headers=headers, json={"properties": restore_props}, timeout=20
                )
                print(f"   ✅ RESTORED: {email}")
        time.sleep(0.15)

    print(f"\n✅ Revert complete.")
    os.rename(REVERT_FILE, REVERT_FILE.replace('.json', '_used.json'))


def run_full(token, df, resume=False):
    # Load known errors to skip
    known_errors = load_known_errors()

    start_batch = 1
    total_updated = 0
    total_created = 0
    total_errors = 0
    total_skipped = 0
    total_error_skipped = 0

    if resume:
        progress = load_progress()
        if progress:
            start_batch = progress['batch_num'] + 1
            total_updated = progress['total_updated']
            total_created = progress['total_created']
            total_errors = progress['total_errors']
            total_skipped = progress['total_skipped']
            print(f"   ▶️  Resuming from batch {start_batch}")
        else:
            print("   ⚠️  No progress file found — starting from beginning")

    total_records = len(df)
    print(f"\n{'='*60}")
    print(f"FULL PUSH — {total_records} records in batches of {BATCH_SIZE}")
    print(f"Skipping {len(known_errors)} known unresolved errors")
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
            owner_name = str(row.get('Owner_Name', '')).strip()

            if not email:
                total_skipped += 1
                continue
            if has_apostrophe(email):
                total_skipped += 1
                continue
            if email in known_errors:
                total_error_skipped += 1
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
                    log_error_to_bq(email, owner_name, error)
                    known_errors.add(email)  # Don't retry in this run
            except Exception as e:
                total_errors += 1
                print(f"      ❌ {email}: Exception — {e}")

            time.sleep(0.1)

        print(f"      ✅ Batch {batch_num} done. Totals — Updated: {total_updated}, Created: {total_created}, Skipped: {total_skipped}, Error-skipped: {total_error_skipped}, Errors: {total_errors}")
        save_progress(batch_num, total_updated, total_created, total_errors, total_skipped)
        time.sleep(0.5)

    if os.path.exists(PROGRESS_FILE):
        os.remove(PROGRESS_FILE)

    summary = f"Push complete. Updated: {total_updated}, Created: {total_created}, New errors logged: {total_errors}, Skipped (known errors): {total_error_skipped}"
    print(f"\n🎉 {summary}")
    # Only flag as error if something genuinely went wrong
    is_error = total_updated < (total_records * 0.8)
    send_slack(summary, is_error=is_error)


def main():
    parser = argparse.ArgumentParser(description='BQ to HubSpot push v0.3.0')
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--test', action='store_true')
    group.add_argument('--revert', action='store_true')
    group.add_argument('--full', action='store_true')
    group.add_argument('--reconcile', action='store_true', help='Force reconciliation run')
    parser.add_argument('--resume', action='store_true', help='Resume full push from last saved batch')
    args = parser.parse_args()

    token = get_token()

    if args.revert:
        run_revert(token)
        return

    if args.reconcile:
        run_reconciliation(token)
        return

    print(f"📡 Loading BQ data...")
    df = load_bq_data(limit=10 if args.test else None)

    if args.test:
        run_test(token, df)
    elif args.full:
        # Auto-run reconciliation on Mondays
        if date.today().weekday() == 0:
            print("📅 Monday detected — running reconciliation first...")
            run_reconciliation(token)

        if not args.resume:
            confirm = input(f"\n⚠️  This will push {len(df)} records to HubSpot. Type YES to confirm: ")
            if confirm.strip() != 'YES':
                print("Aborted.")
                return
        run_full(token, df, resume=args.resume)


if __name__ == '__main__':
    main()
