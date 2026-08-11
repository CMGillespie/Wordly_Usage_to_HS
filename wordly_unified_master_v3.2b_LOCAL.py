print("🚀 [HEARTBEAT] SCRIPT IS STARTING NOW...")
# wordly_unified_master_v3.2b_LOCAL.py
# VERSION: v3.4-LOCAL
# MACHINE: MacBook Air M1 — wordly_apps@Kirks-MacBook-Air
# CHANGES FROM v3.3-LOCAL:
#   - Added customer_success_manager to company fetch → hs_csm column in BQ
#   - Added associatedcompanyid to contacts fetch → fallback company lookup for unmatched domains
#   - Added company_id → name/owner/csm maps for fallback resolution
#   - Slack message updated: v3.2b-LOCAL, Daily Account Usage Info captured
#   - Named service = company name logic retained from v3.3

import os
import glob
import json
import pandas as pd
import requests
import shutil
from datetime import datetime, timedelta
from playwright.sync_api import sync_playwright
import time

# --- ⚙️ LOCAL M1 PATH CONFIGURATION ---
BASE_DIR = "/Users/wordly_apps/Documents/Code/Wordly_Usage_to_HS"
GDRIVE_UPLOAD = "/Users/wordly_apps/Library/CloudStorage/GoogleDrive-chris.gillespie@wordly.ai/My Drive/Code/Wordly_Usage_to_HS/UPLOAD"

DATA_DIR = os.path.join(BASE_DIR, "DATA")
UPLOAD_DIR = GDRIVE_UPLOAD
PROCESSED_DIR = os.path.join(BASE_DIR, "PROCESSED")
ARCHIVE_DIR = os.path.join(PROCESSED_DIR, "ARCHIVE")
SESSION_FILE = os.path.join(BASE_DIR, "wordly_session_state.json")

WORDLY_CREDS = os.path.join(BASE_DIR, "wordly_creds.txt")
HS_KEY_FILE = os.path.join(BASE_DIR, "HS_Service_key.txt")
SLACK_KEY_FILE = os.path.join(BASE_DIR, "slack_webhook.txt")

EXCLUSION_KEYWORDS = ["Trial", "Intro", "Demo", "Test", "Free", "Wordly Internal"]

GENERIC_SERVICES = {
    'Active', 'Restricted', 'Trial', 'SMB Bronze', 'SMB Silver',
    'SMB Gold', 'SMB Platinum', 'SMB Diamond', 'Wordly Workspace'
}

# Ensure folders exist
for folder in [DATA_DIR, PROCESSED_DIR, ARCHIVE_DIR]:
    if not os.path.exists(folder):
        os.makedirs(folder)

if os.path.exists(UPLOAD_DIR):
    print(f"   ✅ GDrive UPLOAD folder confirmed: {UPLOAD_DIR}")
else:
    print(f"   ⚠️ GDrive UPLOAD not found — falling back to local UPLOAD")
    UPLOAD_DIR = os.path.join(BASE_DIR, "UPLOAD")
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    print(f"   📁 Local UPLOAD: {UPLOAD_DIR}")


def send_slack_message(message, is_error=True):
    if not os.path.exists(SLACK_KEY_FILE):
        print(f"⚠️ SLACK ERROR: Webhook file missing at {SLACK_KEY_FILE}")
        return
    with open(SLACK_KEY_FILE, "r") as f:
        url = f.read().strip()
    prefix = "🚨 *ERROR:* " if is_error else "✅ *SUCCESS:* "
    try:
        requests.post(url, json={"text": f"{prefix}{message}"}, timeout=10)
    except Exception as e:
        print(f"⚠️ SLACK POST FAILED: {e}")


def nuke_alert(page, location_name=""):
    try:
        print(f"☢️ Deploying Nuclear Option at {location_name}...")
        page.evaluate("""() => {
            const selectors = ['#DN70', '.p-dialog-mask', '.p-component-overlay', 'p-dialog'];
            selectors.forEach(sel => {
                const elements = document.querySelectorAll(sel);
                elements.forEach(el => {
                    let container = el.closest('.p-dialog-mask') || el.closest('.p-component-overlay') || el;
                    container.remove();
                });
            });
        }""")
        page.keyboard.press("Escape")
        time.sleep(1)
    except:
        pass


def load_session_state(context):
    if os.path.exists(SESSION_FILE):
        print(f"   🔑 Session state found — loading...")
        with open(SESSION_FILE, "r") as f:
            state = json.load(f)
        context.add_cookies(state.get("cookies", []))
        print(f"   ✅ Loaded {len(state.get('cookies', []))} cookies")
        return True
    print("   ⚠️ No session state file — will do full login")
    return False


def save_session_state(context):
    try:
        state = context.storage_state()
        with open(SESSION_FILE, "w") as f:
            json.dump(state, f)
        print(f"   💾 Session state saved: {len(state.get('cookies', []))} cookies")
    except Exception as e:
        print(f"   ⚠️ Could not save session state: {e}")


def fetch_hs_objects(token, obj_type, properties):
    print(f"   📡 Starting HubSpot {obj_type} fetch...")
    headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
    url = f"https://api.hubapi.com/crm/v3/objects/{obj_type}"
    all_records, params = [], {"limit": 100, "properties": ",".join(properties), "archived": "false"}

    while True:
        retries = 3
        success = False
        while retries > 0 and not success:
            try:
                r = requests.get(url, headers=headers, params=params, timeout=30)
                if r.status_code == 200:
                    success = True
                    data = r.json()
                else:
                    retries -= 1
                    time.sleep(5)
            except:
                retries -= 1
                time.sleep(5)

        if not success:
            break
        all_records.extend(data.get('results', []))
        if len(all_records) % 10000 == 0:
            print(f"      📥 Retrieved {len(all_records)} {obj_type}...")
            time.sleep(1)

        if 'paging' in data and 'next' in data['paging']:
            params['after'] = data['paging']['next']['after']
        else:
            break

    print(f"   ✅ Total {obj_type} fetched: {len(all_records)}")
    return all_records


def fetch_hs_owner_map(token):
    print("   📡 Fetching HubSpot owner list...")
    headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
    try:
        r = requests.get(
            "https://api.hubapi.com/crm/v3/owners",
            headers=headers,
            params={"limit": 500},
            timeout=30
        )
        if r.status_code == 200:
            owners = r.json().get('results', [])
            owner_map = {
                str(o['id']): f"{o.get('firstName', '')} {o.get('lastName', '')}".strip()
                for o in owners
            }
            print(f"   ✅ Owner map built: {len(owner_map)} entries")
            return owner_map
        else:
            print(f"   ⚠️ Owner fetch returned status {r.status_code}")
    except Exception as e:
        print(f"   ⚠️ Owner fetch failed: {e}")
    return {}


def get_sanitized_history(target_days):
    try:
        import pandas_gbq
        from datetime import date, timedelta
        target_date = (date.today() - timedelta(days=target_days)).isoformat()
        query = f"""
            SELECT snapshot_date, Owner_Email, Contact_ID, Consumed_Mins
            FROM `support-467322.wordly_usage_data.usage_history`
            WHERE snapshot_date = (
                SELECT snapshot_date
                FROM `support-467322.wordly_usage_data.usage_history`
                WHERE snapshot_date <= '{target_date}'
                ORDER BY snapshot_date DESC
                LIMIT 1
            )
        """
        hdf = pandas_gbq.read_gbq(query, project_id='support-467322')
        if hdf.empty:
            print(f"   ⚠️ No BQ history found for {target_days}d window.")
            return pd.Series(dtype=float)

        print(f"   ✅ BQ history loaded for {target_days}d window: {len(hdf)} rows from {hdf['snapshot_date'].iloc[0]}")

        for col in hdf.columns:
            if hdf[col].dtype == 'object':
                hdf[col] = hdf[col].astype(str).str.strip()

        if 'Contact_ID' in hdf.columns:
            hdf['CID'] = hdf['Contact_ID'].astype(str).str.replace(r'\.0$', '', regex=True).replace(['nan', 'None', ''], pd.NA)
            hdf['MK'] = hdf['CID'].fillna(hdf['Owner_Email'].str.lower())
        else:
            hdf['MK'] = hdf['Owner_Email'].str.lower()

        return hdf.groupby('MK')['Consumed_Mins'].sum()

    except Exception as e:
        print(f"   ⚠️ BQ history fetch failed: {e}")
        return pd.Series(dtype=float)


def run_v3_4_local():
    print(f"🚀 Launching v3.2b-LOCAL at {datetime.now().strftime('%H:%M:%S')}")

    with open(WORDLY_CREDS, "r") as f:
        raw_content = f.read().strip()
        lines = raw_content.splitlines()
        if '=' in lines[0]:
            email = lines[0].split('=')[1].strip()
            password = lines[1].split('=')[1].strip()
        else:
            email, password = lines[0], lines[1]

    downloaded_files = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()

        session_loaded = load_session_state(context)

        if session_loaded:
            page.goto("https://portal.wordly.ai")
            try:
                page.wait_for_selector("#portal-login-btn-signin-wordly", timeout=5000)
                page.click("#portal-login-btn-signin-wordly", force=True)
            except:
                pass
            try:
                page.wait_for_selector("app-root", timeout=15000)
                print("   ✅ Session state valid — skipping login")
            except:
                print("   ⚠️ Session expired — falling back to full login")
                if os.path.exists(SESSION_FILE):
                    os.remove(SESSION_FILE)
                session_loaded = False

        if not session_loaded:
            page.goto("https://portal.wordly.ai")
            nuke_alert(page, "Login Page")
            try:
                page.wait_for_selector("#portal-login-btn-signin-wordly", timeout=5000)
                page.click("#portal-login-btn-signin-wordly", force=True)
            except:
                pass
            page.wait_for_selector("#username", timeout=10000)
            page.fill("#username", email)
            page.fill("#password", password)
            page.click("#kc-login")

            print("   ⏳ Waiting for MFA completion...")
            try:
                page.wait_for_selector("app-root", timeout=300000)
                print("   ✅ Login complete")
                save_session_state(context)
            except Exception as e:
                print(f"   ❌ Login failed: {e}")
                browser.close()
                return

        page.goto("https://portal.wordly.ai/#/admin-accounts", wait_until="networkidle")
        page.wait_for_timeout(10000)
        nuke_alert(page, "Admin Hub")

        page.wait_for_selector("#serviceFilter", timeout=30000)
        page.locator("#serviceFilter").click()
        page.wait_for_timeout(2000)
        options = page.locator(".p-dropdown-item")

        targets = []
        for i in range(options.count()):
            name = options.nth(i).inner_text().strip()
            if name != "All" and not any(kw.lower() in name.lower() for kw in EXCLUSION_KEYWORDS):
                targets.append(name)

        page.keyboard.press("Escape")
        print(f"   📋 Services found: {targets}")

        for service in targets:
            try:
                page.locator("#serviceFilter").click()
                page.wait_for_timeout(1000)
                page.locator(".p-dropdown-items").get_by_text(service, exact=True).click()
                page.wait_for_timeout(5000)
                if page.locator(".account-loading").count() > 0:
                    print(f"   🚫 {service} — no accounts")
                    page.keyboard.press("Escape")
                    continue
                page.locator("span.option-text.locale-adjust").click(force=True)
                download_link = page.locator("span:has-text('Download Account Report')")

                if download_link.count() > 0:
                    with page.expect_download(timeout=30000) as d_info:
                        download_link.click(force=True)
                    path = os.path.join(DATA_DIR, f"temp_{service.replace(' ', '_')}.csv")
                    d_info.value.save_as(path)
                    downloaded_files.append(path)
                    print(f"   ✅ {service}")
                else:
                    page.keyboard.press("Escape")
            except Exception as e:
                print(f"   ⚠️ Skipped (error): {service} — {e}")
                page.keyboard.press("Escape")

        browser.close()

    if downloaded_files:
        master_df = pd.concat([pd.read_csv(f) for f in downloaded_files], ignore_index=True)
        print(f"   📊 Raw rows before aggregation: {len(master_df)}")
        for f in downloaded_files:
            os.remove(f)

        if os.path.exists(HS_KEY_FILE):
            with open(HS_KEY_FILE, "r") as f:
                token = f.read().strip()

            # --- CONTACTS: fetch email + associatedcompanyid ---
            raw_contacts = fetch_hs_objects(token, "contacts", ["email", "associatedcompanyid"])
            contacts = {
                c['properties']['email'].lower(): c['id']
                for c in raw_contacts
                if c['properties'].get('email')
            }
            # Map email → associated company ID for fallback lookup
            contact_company_map = {
                c['properties']['email'].lower(): str(c['properties'].get('associatedcompanyid', '') or '')
                for c in raw_contacts
                if c['properties'].get('email') and c['properties'].get('associatedcompanyid')
            }
            print(f"   ✅ Contact→company fallback map: {len(contact_company_map)} entries with company links")

            # --- COMPANIES: domain, owner, name, CSM ---
            raw_companies = fetch_hs_objects(token, "companies", [
                "domain", "hubspot_owner_id", "name", "customer_success_manager"
            ])

            # Domain-keyed maps (primary lookup)
            companies = {
                c['properties']['domain'].lower(): c['id']
                for c in raw_companies
                if c['properties'].get('domain')
            }
            company_owner_map = {
                c['properties']['domain'].lower(): str(c['properties'].get('hubspot_owner_id', '') or '')
                for c in raw_companies
                if c['properties'].get('domain')
            }
            company_name_map = {
                c['properties']['domain'].lower(): c['properties'].get('name', '') or ''
                for c in raw_companies
                if c['properties'].get('domain')
            }
            company_csm_map = {
                c['properties']['domain'].lower(): str(c['properties'].get('customer_success_manager', '') or '')
                for c in raw_companies
                if c['properties'].get('domain')
            }

            # Company ID-keyed maps (fallback lookup)
            company_id_to_hs_id = {
                c['id']: c['id']
                for c in raw_companies
            }
            company_id_owner_map = {
                c['id']: str(c['properties'].get('hubspot_owner_id', '') or '')
                for c in raw_companies
            }
            company_id_name_map = {
                c['id']: c['properties'].get('name', '') or ''
                for c in raw_companies
            }
            company_id_csm_map = {
                c['id']: str(c['properties'].get('customer_success_manager', '') or '')
                for c in raw_companies
            }

            # Owner name resolution
            hs_owner_name_map = fetch_hs_owner_map(token)

            # --- PRIMARY LOOKUPS (domain-based) ---
            master_df["Contact ID"] = master_df["Owner Email"].str.lower().map(contacts)
            master_df["Company ID"] = master_df["Owner Email"].str.lower().str.split('@').str[-1].map(companies)
            master_df["hs_owner_id"] = master_df["Owner Email"].str.lower().str.split('@').str[-1].map(company_owner_map)
            master_df["hs_account_owner"] = master_df["hs_owner_id"].map(hs_owner_name_map).fillna("")
            master_df["hs_company_name"] = master_df["Owner Email"].str.lower().str.split('@').str[-1].map(company_name_map).fillna("")
            master_df["hs_csm_id"] = master_df["Owner Email"].str.lower().str.split('@').str[-1].map(company_csm_map).fillna("")
            master_df["hs_csm"] = master_df["hs_csm_id"].map(hs_owner_name_map).fillna("")

            # --- NAMED SERVICE FALLBACK for company name ---
            master_df["hs_company_name"] = master_df.apply(
                lambda row: row["hs_company_name"] if row["hs_company_name"] != ""
                else (row["Service"] if row["Service"] not in GENERIC_SERVICES else ""),
                axis=1
            )

            # --- CONTACT→COMPANY FALLBACK for unmatched domains ---
            # For rows still missing Company ID, check if the contact has an associated company
            unmatched_mask = master_df["Company ID"].isna() | (master_df["Company ID"] == "")
            unmatched_count = unmatched_mask.sum()
            if unmatched_count > 0:
                print(f"   🔍 Running contact→company fallback for {unmatched_count} unmatched rows...")
                fallback_resolved = 0

                def apply_fallback(row):
                    if not (pd.isna(row["Company ID"]) or row["Company ID"] == ""):
                        return row
                    email = str(row["Owner Email"]).lower().strip()
                    assoc_company_id = contact_company_map.get(email, "")
                    if not assoc_company_id:
                        return row
                    row["Company ID"] = assoc_company_id
                    if not row["hs_account_owner"]:
                        owner_id = company_id_owner_map.get(assoc_company_id, "")
                        row["hs_owner_id"] = owner_id
                        row["hs_account_owner"] = hs_owner_name_map.get(owner_id, "")
                    if not row["hs_company_name"]:
                        row["hs_company_name"] = company_id_name_map.get(assoc_company_id, "")
                    if not row["hs_csm"]:
                        csm_id = company_id_csm_map.get(assoc_company_id, "")
                        row["hs_csm_id"] = csm_id
                        row["hs_csm"] = hs_owner_name_map.get(csm_id, "")
                    return row

                master_df[unmatched_mask] = master_df[unmatched_mask].apply(apply_fallback, axis=1)
                fallback_resolved = unmatched_mask.sum() - (master_df["Company ID"].isna() | (master_df["Company ID"] == "")).sum()
                print(f"   ✅ Fallback resolved {fallback_resolved} additional company links")

        master_df['MK'] = master_df['Contact ID'].fillna(master_df['Owner Email'].str.lower())
        agg_rules = {
            'Owner Name': 'first',
            'Owner Email': 'first',
            'Service': 'first',
            'Allow Overage': 'first',
            'Consumed Mins': 'sum',
            'Available Mins': 'sum',
            'Date Created': 'min',
            'Last Used': 'max',
            'Contact ID': 'first',
            'Company ID': 'first',
            'hs_owner_id': 'first',
            'hs_account_owner': 'first',
            'hs_company_name': 'first',
            'hs_csm_id': 'first',
            'hs_csm': 'first',
        }
        master_df = master_df.groupby('MK').agg(agg_rules).reset_index()

        now = datetime.now()
        h7, h30 = get_sanitized_history(7), get_sanitized_history(30)
        master_df['Consumed Last 7 Days'] = master_df.apply(
            lambda r: max(0, r['Consumed Mins'] - h7.get(r['MK'], 0)), axis=1
        )
        master_df['Consumed Last 30 Days'] = master_df.apply(
            lambda r: max(0, r['Consumed Mins'] - h30.get(r['MK'], 0)), axis=1
        )

        cutoff_90 = datetime.now() - timedelta(days=90)
        master_df['Consumed Last 7 Days'] = master_df.apply(
            lambda r: 0 if pd.to_datetime(r['Last Used'], errors='coerce') < cutoff_90 
            else r['Consumed Last 7 Days'], axis=1
        )
        master_df['Consumed Last 30 Days'] = master_df.apply(
            lambda r: 0 if pd.to_datetime(r['Last Used'], errors='coerce') < cutoff_90 
            else r['Consumed Last 30 Days'], axis=1
        )
        ts = now.strftime('%Y-%m-%d')
        for col in ["Contact ID", "Company ID"]:
            master_df[col] = master_df[col].astype(str).str.replace(r'\.0$', '', regex=True).replace(['nan', 'None', ''], '')

        # CSV — strip internal reconciliation columns
        csv_drop_cols = ['MK', 'hs_owner_id', 'hs_account_owner', 'hs_company_name', 'hs_csm_id', 'hs_csm']
        final_path = os.path.join(UPLOAD_DIR, f"Wordly_Master_Import_{ts}.csv")
        master_df.drop(columns=[c for c in csv_drop_cols if c in master_df.columns]).to_csv(
            final_path, index=False, encoding='utf-8-sig'
        )
        print(f"   ✅ CSV written: {final_path}")

        # --- BIGQUERY ---
        try:
            import pandas_gbq

            bq_drop_cols = ['MK', 'hs_owner_id', 'hs_csm_id']
            bq_df = master_df.drop(columns=[c for c in bq_drop_cols if c in master_df.columns]).copy()
            bq_df.columns = [c.replace(' ', '_') for c in bq_df.columns]
            bq_df['snapshot_date'] = pd.to_datetime('today').date()

            from google.cloud import bigquery
            bq_client = bigquery.Client(project='support-467322')
            today = bq_df['snapshot_date'].iloc[0]
            bq_client.query(f"""
                DELETE FROM `support-467322.wordly_usage_data.usage_history`
                WHERE snapshot_date = '{today}'
            """).result()
            print(f"   🗑️ Cleared existing rows for {today}")

            pandas_gbq.to_gbq(
                bq_df,
                'wordly_usage_data.usage_history',
                project_id='support-467322',
                if_exists='append'
            )
            print(f"🚀 Mirroring Complete: {len(bq_df)} rows added to BigQuery.")

        except Exception as e:
            print(f"⚠️ BigQuery Mirroring failed, but CSV is safe! Error: {e}")
        # --- END BIGQUERY ---

        send_slack_message(f"v3.2b-LOCAL Daily Account Usage Info captured. Rows: {len(master_df)}", is_error=False)
        print(f"🎉 SUCCESS! Aggregated: {len(master_df)} rows")
    else:
        send_slack_message("v3.2b-LOCAL: No files downloaded — check browser/session.", is_error=True)
        print("❌ No files downloaded.")


if __name__ == "__main__":
    run_v3_4_local()