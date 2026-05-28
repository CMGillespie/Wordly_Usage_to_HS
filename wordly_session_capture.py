# wordly_session_capture.py
# VERSION: 1.0-LOCAL
# PURPOSE: One-time manual login with MFA. Saves browser session state to GCS bucket
#          so cloud jobs can skip login entirely.
# RUN: python3 wordly_session_capture.py
# WHEN: Run this locally whenever the cloud job hits a login wall.

import os
import json
import subprocess
from datetime import datetime
from playwright.sync_api import sync_playwright

# --- CONFIGURATION ---
BASE_DIR = '/Users/chriswork/Documents/Wordly_Usage_to_HS'
CREDS_FILE = os.path.join(BASE_DIR, 'wordly_creds.txt')
SESSION_FILE = os.path.join(BASE_DIR, 'wordly_session_state.json')
GCS_BUCKET = 'wordly_usage_2_hs'
GCS_DEST = f'gs://{GCS_BUCKET}/wordly_session_state.json'

def get_credentials():
    with open(CREDS_FILE, 'r') as f:
        raw = f.read().strip().splitlines()
        if '=' in raw[0]:
            return raw[0].split('=')[1].strip(), raw[1].split('=')[1].strip()
        return raw[0], raw[1]

def upload_to_gcs(local_path, gcs_dest):
    print(f'   📤 Uploading session state to {gcs_dest}...')
    result = subprocess.run(
        ['gcloud', 'storage', 'cp', local_path, gcs_dest],
        capture_output=True, text=True
    )
    if result.returncode == 0:
        print('   ✅ Upload successful.')
    else:
        print(f'   ⚠️ Upload failed: {result.stderr}')
        print(f'   💡 You can manually upload: gsutil cp {local_path} {gcs_dest}')

def main():
    print('🔑 Wordly Session Capture v1.0')
    print('=' * 45)
    print('This script will open a visible browser.')
    print('Log in manually and complete MFA when prompted.')
    print('The script will detect success and save your session.')
    print('=' * 45)

    email, password = get_credentials()

    with sync_playwright() as p:
        # headless=False — you need to see the browser to complete MFA
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()

        print('\n🌐 Opening Wordly portal...')
        page.goto('https://portal.wordly.ai')

        # Handle the Wordly login button if present
        try:
            page.wait_for_selector('#portal-login-btn-signin-wordly', timeout=5000)
            page.click('#portal-login-btn-signin-wordly', force=True)
        except:
            pass

        # Fill in credentials
        page.wait_for_selector('#username', timeout=10000)
        page.fill('#username', email)
        page.fill('#password', password)
        page.click('#kc-login')

        print('\n⏳ Waiting for MFA...')
        print('   Complete the MFA prompt in the browser window.')
        print('   The script will continue automatically once you are logged in.')

        # Wait for successful login — detected by reaching the portal dashboard
        # Timeout is generous (5 minutes) to give you time to find your phone
        try:
            page.wait_for_url('**/portal.wordly.ai/**', timeout=300000)
            # Extra wait to ensure session cookies are fully set
            page.wait_for_timeout(3000)

            # Confirm we are past login by checking for a known portal element
            try:
                page.wait_for_selector('app-root', timeout=10000)
                print('\n✅ Login detected — session is active.')
            except:
                print('\n✅ Login detected via URL change.')

        except Exception as e:
            print(f'\n❌ Login not detected within 5 minutes: {e}')
            browser.close()
            return

        # Save session state (cookies + local storage)
        session_state = context.storage_state()
        with open(SESSION_FILE, 'w') as f:
            json.dump(session_state, f)

        ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        print(f'\n💾 Session state saved locally: {SESSION_FILE}')
        print(f'   Captured at: {ts}')
        print(f'   Cookies: {len(session_state.get("cookies", []))}')
        print(f'   Origins: {len(session_state.get("origins", []))}')

        browser.close()

    # Upload to GCS so cloud jobs can use it
    upload_to_gcs(SESSION_FILE, GCS_DEST)

    print('\n🎉 Done. The cloud job will use this session on its next run.')
    print('   If the cloud job hits a login wall again, re-run this script.')

if __name__ == '__main__':
    main()
