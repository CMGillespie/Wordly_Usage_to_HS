# wordly_unified_master_v3.1.5.py
# VERSION: 3.1.5
# AUTH: Gemini (Vibe Coder Architect) for Chris
# SURGICAL FIX: Uses JS Injection to delete the IAM alert from the DOM.
# RESTORES: ALL 260ish lines of logic from v3.1.3 (HS retries, Archiving, Unlinked).

import os
import glob
import pandas as pd
import requests
import shutil
from datetime import datetime, timedelta
from playwright.sync_api import sync_playwright
import time

# --- ⚙️ CONFIGURATION ---
BASE_DIR = "/Users/wordly_apps/Library/CloudStorage/GoogleDrive-chris.gillespie@wordly.ai/My Drive/Code/Wordly_Usage_to_HS"
DATA_DIR = os.path.join(BASE_DIR, "DATA")
UPLOAD_DIR = os.path.join(BASE_DIR, "UPLOAD")
PROCESSED_DIR = os.path.join(BASE_DIR, "PROCESSED")
ARCHIVE_DIR = os.path.join(PROCESSED_DIR, "ARCHIVE")
WORDLY_CREDS = os.path.join(BASE_DIR, "wordly_creds.txt")
HS_KEY_FILE = os.path.join(BASE_DIR, "HS_Service_key.txt")
SLACK_KEY_FILE = os.path.join(BASE_DIR, "slack_webhook.txt")
EXCLUSION_KEYWORDS = ["Trial", "Intro", "Demo", "Test", "Free", "Wordly Internal"]

for folder in [DATA_DIR, UPLOAD_DIR, PROCESSED_DIR, ARCHIVE_DIR]:
    if not os.path.exists(folder): os.makedirs(folder)

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
    """The Nuclear Option: Deletes the alert and its mask from the browser memory."""
    try:
        print(f"☢️ Deploying Nuclear Option at {location_name}...")
        # This JavaScript finds the X button (#DN70) or the modal mask and deletes them
        page.evaluate("""() => {
            const selectors = ['#DN70', '.p-dialog-mask', '.p-component-overlay', 'p-dialog'];
            selectors.forEach(sel => {
                const elements = document.querySelectorAll(sel);
                elements.forEach(el => {
                    // Find the container and kill it
                    let container = el.closest('.p-dialog-mask') || el.closest('.p-component-overlay') || el;
                    container.remove();
                });
            });
        }""")
        # Fallback: Still hit Escape just to be sure the app state resets
        page.keyboard.press("Escape")
        time.sleep(1)
    except: pass

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

        if not success: break
        all_records.extend(data.get('results', []))
        if len(all_records) % 10000 == 0:
            print(f"      📥 Retrieved {len(all_records)} {obj_type}...")
            time.sleep(1) 

        if 'paging' in data and 'next' in data['paging']:
            params['after'] = data['paging']['next']['after']
        else: break
            
    print(f"   ✅ Total {obj_type} fetched: {len(all_records)}")
    return all_records

def get_sanitized_history(target_days):
    """Finds the anchor file for subtraction. Rule: File closest to Target Days age."""
    now = datetime.now()
    hist_files = glob.glob(os.path.join(PROCESSED_DIR, "Wordly_Master_Import_*.csv"))
    
    file_data = []
    for f in hist_files:
        try:
            f_date_str = os.path.basename(f).split('_')[-1].replace('.csv', '')
            f_date = datetime.strptime(f_date_str, '%Y-%m-%d')
            age = (now - f_date).days
            file_data.append({'age': age, 'diff': abs(age - target_days), 'path': f})
        except: continue
    
    if not file_data:
        print(f"   ⚠️ No history found for {target_days}d window.")
        return pd.Series(dtype=float)
    
    best_match = min(file_data, key=lambda x: x['diff'])
    print(f"   📊 {target_days}-day Delta Anchor: {os.path.basename(best_match['path'])} (Age: {best_match['age']} days)")
    
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

def run_v3_1_5_sync():
    print(f"🚀 Launching v3.1.5 at {datetime.now().strftime('%H:%M:%S')}")
    
    current_uploads = glob.glob(os.path.join(UPLOAD_DIR, "*.csv"))
    for f in current_uploads:
        shutil.move(f, os.path.join(PROCESSED_DIR, os.path.basename(f)))

    with open(WORDLY_CREDS, "r") as f: 
        raw_content = f.read().strip()
        if ',' in raw_content: 
            email, password = raw_content.split(",", 1)
        else:
            lines = raw_content.splitlines()
            if len(lines) > 0 and '=' in lines[0]:
                email = lines[0].split('=')[1].strip()
                password = lines[1].split('=')[1].strip()
            else:
                email, password = lines[0], lines[1]

    downloaded_files = []
    excluded_log = []

    with sync_playwright() as p:
        # Headless=True for production, False for your observation
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()

        page.goto("https://portal.wordly.ai")
        
        # --- NUCLEAR STRIKE 1 (Login) ---
        nuke_alert(page, "Login Page")

        try:
            page.wait_for_selector("#portal-login-btn-signin-wordly", timeout=5000)
            page.click("#portal-login-btn-signin-wordly", force=True)
        except: pass
        
        page.wait_for_selector("#username", timeout=10000)
        page.fill("#username", email)
        page.fill("#password", password)
        page.click("#kc-login")
        
        page.goto("https://portal.wordly.ai/#/admin-accounts", wait_until="networkidle")
        
        # --- NUCLEAR STRIKE 2 (Hub Page) ---
        # Give it a 5s window for the lazy-load to occur, then delete it
        page.wait_for_timeout(5000) 
        nuke_alert(page, "Admin Hub")

        page.wait_for_selector("#serviceFilter", timeout=15000)
        page.locator("#serviceFilter").click()
        page.wait_for_timeout(2000)
        options = page.locator(".p-dropdown-item")
        
        targets = []
        for i in range(options.count()):
            name = options.nth(i).inner_text().strip()
            if name == "All": continue
            if any(kw.lower() in name.lower() for kw in EXCLUSION_KEYWORDS):
                excluded_log.append(name)
            else:
                targets.append(name)
        
        page.keyboard.press("Escape")
        if excluded_log:
            print(f"   🚫 Skipping {len(excluded_log)} internal/trial accounts.")

        for service in targets:
            try:
                page.locator("#serviceFilter").click()
                page.wait_for_timeout(1000)
                page.get_by_text(service, exact=True).click()
                page.wait_for_timeout(5000) 
                page.locator("span.option-text.locale-adjust").click(force=True)
                page.wait_for_selector("a.p-menuitem-link, span:has-text('Download')", timeout=5000)
                
                download_link = page.locator("span:has-text('Download Account Report')")
                
                if download_link.count() == 0:
                    print(f"   ➡️ {service} (Empty)")
                    page.keyboard.press("Escape")
                    continue
                
                with page.expect_download(timeout=30000) as d_info:
                    download_link.click(force=True)
                path = os.path.join(DATA_DIR, f"temp_{service.replace(' ', '_')}.csv")
                d_info.value.save_as(path)
                downloaded_files.append(path)
                print(f"   ✅ {service}")
            except:
                print(f"   ➡️ {service} (Skipped/Error)")
                page.keyboard.press("Escape")
        browser.close()

    # --- ALL SUBSEQUENT LOGIC PRESERVED FROM v3.1.3 ---
    if downloaded_files:
        master_df = pd.concat([pd.read_csv(f) for f in downloaded_files], ignore_index=True)
        for f in downloaded_files: os.remove(f)

        for col in master_df.columns:
            if master_df[col].dtype == 'object':
                master_df[col] = master_df[col].astype(str).str.strip()

        for col in ['Consumed Mins', 'Available Mins']:
            master_df[col] = pd.to_numeric(master_df[col], errors='coerce').fillna(0).astype(int)

        if os.path.exists(HS_KEY_FILE):
            with open(HS_KEY_FILE, "r") as f: token = f.read().strip()
            contacts = {c['properties']['email'].lower(): c['id'] for c in fetch_hs_objects(token, "contacts", ["email"]) if c['properties'].get('email')}
            companies = {c['properties']['domain'].lower(): c['id'] for c in fetch_hs_objects(token, "companies", ["domain"]) if c['properties'].get('domain')}
            
            master_df["Contact ID"] = master_df["Owner Email"].str.lower().map(contacts)
            master_df["Company ID"] = master_df["Owner Email"].str.lower().str.split('@').str[-1].map(companies)

        master_df['MK'] = master_df['Contact ID'].fillna(master_df['Owner Email'].str.lower())
        agg_rules = {
            'Owner Name': 'first', 'Owner Email': 'first', 'Service': 'first', 
            'Allow Overage': 'first', 'Consumed Mins': 'sum', 'Available Mins': 'sum',
            'Date Created': 'min', 'Last Used': 'max', 'Contact ID': 'first', 'Company ID': 'first'
        }
        master_df = master_df.groupby('MK').agg(agg_rules).reset_index()

        now = datetime.now()
        dt_col = pd.to_datetime(master_df['Last Used'], errors='coerce')
        h7 = get_sanitized_history(7)
        h30 = get_sanitized_history(30)

        master_df['Consumed Last 7 Days'] = master_df.apply(lambda r: max(0, r['Consumed Mins'] - h7.get(r['MK'], 0)), axis=1)
        master_df['Consumed Last 30 Days'] = master_df.apply(lambda r: max(0, r['Consumed Mins'] - h30.get(r['MK'], 0)), axis=1)

        master_df.loc[dt_col < (now - timedelta(days=7)), 'Consumed Last 7 Days'] = 0
        master_df.loc[dt_col < (now - timedelta(days=30)), 'Consumed Last 30 Days'] = 0
        master_df.loc[dt_col.isna(), ['Consumed Last 7 Days', 'Consumed Last 30 Days']] = 0

        ts = now.strftime('%Y-%m-%d')
        unlinked = master_df[master_df['Contact ID'].isna() | master_df['Company ID'].isna()].copy()
        for col in ["Contact ID", "Company ID"]:
            unlinked[col] = unlinked[col].astype(str).str.replace(r'\.0$', '', regex=True).replace(['nan', 'None', ''], '')
        unlinked.drop(columns=['MK']).to_csv(os.path.join(DATA_DIR, f"Wordly_Unlinked_Accounts_{ts}.csv"), index=False)

        for col in ["Contact ID", "Company ID"]:
            master_df[col] = master_df[col].astype(str).str.replace(r'\.0$', '', regex=True).replace(['nan', 'None', ''], '')

        final_filename = f"Wordly_Master_Import_{ts}.csv"
        final_path = os.path.join(UPLOAD_DIR, final_filename)
        master_df.drop(columns=['MK']).to_csv(final_path, index=False, encoding='utf-8-sig')
        
        all_processed = glob.glob(os.path.join(PROCESSED_DIR, "*.csv"))
        archive_cutoff = now - timedelta(days=31)
        for f in all_processed:
            try:
                f_name = os.path.basename(f).split('_')[-1].replace('.csv', '')
                if datetime.strptime(f_name, '%Y-%m-%d') < archive_cutoff:
                    shutil.move(f, os.path.join(ARCHIVE_DIR, os.path.basename(f)))
            except: continue

        send_slack_message(f"v3.1.5 Success. Accounts: {len(master_df)}")
        print(f"🎉 SUCCESS! Aggregated: {len(master_df)}")

if __name__ == "__main__":
    run_v3_1_5_sync()