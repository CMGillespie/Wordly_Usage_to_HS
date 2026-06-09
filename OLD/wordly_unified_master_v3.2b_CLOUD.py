print("🚀 [HEARTBEAT] SCRIPT IS STARTING NOW...")
# wordly_unified_master_v3.2b_CLOUD.py
# VERSION: 3.2b-CLOUD
# CHANGE FROM v3.2a-CLOUD:
#   - Loads persistent browser session state from GCS bucket at startup
#   - Bypasses MFA login if valid session exists (valid ~7 days)
#   - Falls back to standard login if no session file found
#   - Session state file: /mnt/wordly-data/wordly_session_state.json

import os
import glob
import json
import pandas as pd
import requests
import shutil
from datetime import datetime
from playwright.sync_api import sync_playwright
import time

# --- ⚙️ CLOUD CONFIGURATION ---
BASE_DIR = "/mnt/wordly-data"

DATA_DIR = os.path.join(BASE_DIR, "DATA")
UPLOAD_DIR = os.path.join(BASE_DIR, "UPLOAD")
PROCESSED_DIR = os.path.join(BASE_DIR, "PROCESSED")
ARCHIVE_DIR = os.path.join(PROCESSED_DIR, "ARCHIVE")
SESSION_FILE = os.path.join(BASE_DIR, "wordly_session_state.json")

SCRIPT_DIR = os.getcwd()
WORDLY_CREDS = os.path.join(SCRIPT_DIR, "wordly_creds.txt")
HS_KEY_FILE = os.path.join(SCRIPT_DIR, "HS_Service_key.txt")
SLACK_KEY_FILE = os.path.join(SCRIPT_DIR, "slack_webhook.txt")

EXCLUSION_KEYWORDS = ["Trial", "Intro", "Demo", "Test", "Free", "Wordly Internal"]

for folder in [DATA_DIR, UPLOAD_DIR, PROCESSED_DIR, ARCHIVE_DIR]:
    if not os.path.exists(folder):
        os.makedirs(folder)


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
    """Load saved session state from GCS bucket if available."""
    if os.path.exists(SESSION_FILE):
        print(f"   🔑 Session state found — loading...")
        with open(SESSION_FILE, "r") as f:
            state = json.load(f)
        context.add_cookies(state.get("cookies", []))
        print(f"   ✅ Loaded {len(state.get('cookies', []))} cookies")
        return True
    print("   ⚠️ No session state file found — will attempt fresh login")
    return False


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
    now = datetime.now()
    hist_files = glob.glob(os.path.join(PROCESSED_DIR, "Wordly_Master_Import_*.csv"))

    file_data = []
    for f in hist_files:
        try:
            f_date_str = os.path.basename(f).split('_')[-1].replace('.csv', '')
            f_date = datetime.strptime(f_date_str, '%Y-%m-%d')
            age = (now - f_date).days
            file_data.append({'age': age, 'diff': abs(age - target_days), 'path': f})
        except:
            continue

    if not file_data:
        print(f"   ⚠️ No history found for {target_days}d window.")
        return pd.Series(dtype=float)

    best_match = min(file_data, key=lambda x: x['diff'])
    hdf = pd.read_csv(best_match['path'])
    for col in hdf.columns:
        if hdf[col].dtype == 'object':
            hdf[col] = hdf[col].astype(str).str.strip()

    if 'Contact ID' in hdf.columns:
        hdf['CID'] = hdf['Contact ID'].astype(str).str.replace(r'\.0$', '', regex=True).replace(['nan', 'None', ''], pd.NA)
        hdf['MK'] = hdf['CID'].fillna(hdf['Owner Email'].str.lower())
    else:
        hdf['MK'] = hdf['Owner Email'].str.lower()

    return hdf.groupby('MK')['Consumed Mins'].sum()


def run_v3_2b():
    print(f"🚀 Launching v3.2b-CLOUD at {datetime.now().strftime('%H:%M:%S')}")

    current_uploads = glob.glob(os.path.join(UPLOAD_DIR, "*.csv"))
    for f in current_uploads:
        shutil.move(f, os.path.join(PROCESSED_DIR, os.path.basename(f)))

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

        # Attempt to load saved session state — bypasses MFA if valid
        session_loaded = load_session_state(context)

        page = context.new_page()
        page.goto("https://portal.wordly.ai")

        # Check if session loaded us past login
        if session_loaded:
            try:
                # If we land on the portal app, session is good
                page.wait_for_selector("app-root", timeout=8000)
                print("   ✅ Session state valid — skipping login")
            except:
                # Session expired — fall back to standard login
                print("   ⚠️ Session state expired — falling back to login")
                print("   💡 Re-run wordly_session_capture.py locally to refresh")
                session_loaded = False

        if not session_loaded:
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

        page.goto("https://portal.wordly.ai/#/admin-accounts", wait_until="networkidle")
        page.wait_for_timeout(5000)
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

        for service in targets:
            try:
                page.locator("#serviceFilter").click()
                page.wait_for_timeout(1000)
                page.get_by_text(service, exact=True).click()
                page.wait_for_timeout(5000)
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
            except:
                page.keyboard.press("Escape")

        browser.close()

    if downloaded_files:
        master_df = pd.concat([pd.read_csv(f) for f in downloaded_files], ignore_index=True)
        for f in downloaded_files:
            os.remove(f)

        if os.path.exists(HS_KEY_FILE):
            with open(HS_KEY_FILE, "r") as f:
                token = f.read().strip()

            contacts = {
                c['properties']['email'].lower(): c['id']
                for c in fetch_hs_objects(token, "contacts", ["email"])
                if c['properties'].get('email')
            }

            raw_companies = fetch_hs_objects(token, "companies", ["domain", "hubspot_owner_id"])
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

            hs_owner_name_map = fetch_hs_owner_map(token)

            master_df["Contact ID"] = master_df["Owner Email"].str.lower().map(contacts)
            master_df["Company ID"] = master_df["Owner Email"].str.lower().str.split('@').str[-1].map(companies)

            master_df["hs_owner_id"] = master_df["Owner Email"].str.lower().str.split('@').str[-1].map(company_owner_map)
            master_df["hs_account_owner"] = master_df["hs_owner_id"].map(hs_owner_name_map).fillna("")

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
            'hs_account_owner': 'first'
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

        ts = now.strftime('%Y-%m-%d')
        for col in ["Contact ID", "Company ID"]:
            master_df[col] = master_df[col].astype(str).str.replace(r'\.0$', '', regex=True).replace(['nan', 'None', ''], '')

        csv_drop_cols = ['MK', 'hs_owner_id', 'hs_account_owner']
        final_path = os.path.join(UPLOAD_DIR, f"Wordly_Master_Import_{ts}.csv")
        master_df.drop(columns=[c for c in csv_drop_cols if c in master_df.columns]).to_csv(
            final_path, index=False, encoding='utf-8-sig'
        )
        print(f"   ✅ CSV written: {final_path}")

        # --- BIGQUERY ---
        try:
            import pandas_gbq

            bq_drop_cols = ['MK', 'hs_owner_id']
            bq_df = master_df.drop(columns=[c for c in bq_drop_cols if c in master_df.columns]).copy()
            bq_df.columns = [c.replace(' ', '_') for c in bq_df.columns]
            bq_df['snapshot_date'] = pd.to_datetime('today').date()

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

        send_slack_message(f"v3.2b-CLOUD Success. Rows: {len(master_df)}", is_error=False)
        print(f"🎉 SUCCESS! Aggregated: {len(master_df)} rows")
    else:
        send_slack_message("v3.2b-CLOUD: No files downloaded — session may have expired. Re-run wordly_session_capture.py.", is_error=True)
        print("❌ No files downloaded.")


if __name__ == "__main__":
    run_v3_2b()
