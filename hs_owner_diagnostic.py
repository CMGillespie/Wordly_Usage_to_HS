# hs_owner_diagnostic.py
# VERSION: 0.1.0
# PURPOSE: Diagnostic only. Tests the full owner lookup chain:
#   Company domain -> hubspot_owner_id -> owner name
# Run locally. Not for cloud deployment.

import requests

HS_KEY_FILE = '/Users/cmgillespie/Library/CloudStorage/GoogleDrive-chris.gillespie@wordly.ai/My Drive/Code/Wordly_Usage_to_HS/HS_Service_key.txt'

# Sample emails pulled from Wordly_Master_Import_2026-05-04.csv
TEST_EMAILS = [
    'kokusaika@shakaihokenroumushi.jp',
    'jz@yourgb.co.uk',
    'allan-charles.chipman@iofc.org',
    'thuy.pl@theolympiaschools.edu.vn',
    'christopher.orem@straumann.com',
]

def get_token():
    with open(HS_KEY_FILE, 'r') as f:
        return f.read().strip()

def get_company_owner_id(token, domain):
    """Look up a company by domain, return its hubspot_owner_id."""
    headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
    url = "https://api.hubapi.com/crm/v3/objects/companies"
    params = {
        "limit": 1,
        "properties": "domain,hubspot_owner_id",
        "archived": "false"
    }
    # Use search endpoint for domain lookup
    search_url = "https://api.hubapi.com/crm/v3/objects/companies/search"
    payload = {
        "filterGroups": [{
            "filters": [{
                "propertyName": "domain",
                "operator": "EQ",
                "value": domain
            }]
        }],
        "properties": ["domain", "hubspot_owner_id"],
        "limit": 1
    }
    r = requests.post(search_url, headers=headers, json=payload, timeout=15)
    if r.status_code == 200:
        results = r.json().get('results', [])
        if results:
            owner_id = results[0]['properties'].get('hubspot_owner_id')
            company_id = results[0]['id']
            return company_id, owner_id
    print(f"   ⚠️ Company lookup failed for {domain}: {r.status_code} {r.text[:200]}")
    return None, None

def get_owner_name(token, owner_id):
    """Resolve a numeric owner ID to a human name."""
    headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
    url = f"https://api.hubapi.com/crm/v3/owners/{owner_id}"
    r = requests.get(url, headers=headers, timeout=15)
    if r.status_code == 200:
        data = r.json()
        return f"{data.get('firstName', '')} {data.get('lastName', '')}".strip(), data.get('email', '')
    print(f"   ⚠️ Owner lookup failed for ID {owner_id}: {r.status_code}")
    return None, None

def get_all_owners(token):
    """Pull full owner list in one call. Returns {str(id): 'First Last'}"""
    headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
    r = requests.get(
        "https://api.hubapi.com/crm/v3/owners",
        headers=headers,
        params={"limit": 500},
        timeout=15
    )
    if r.status_code == 200:
        owners = r.json().get('results', [])
        print(f"\n✅ Full owner list pulled: {len(owners)} owners")
        print("--- ALL OWNERS ---")
        for o in owners:
            print(f"  ID: {o['id']}  Name: {o.get('firstName','')} {o.get('lastName','')}  Email: {o.get('email','')}")
        print("--- END OWNERS ---\n")
        return {str(o['id']): f"{o.get('firstName','')} {o.get('lastName','')}".strip() for o in owners}
    print(f"⚠️ Owner list failed: {r.status_code} {r.text[:200]}")
    return {}

def main():
    print("🔍 HS Owner Diagnostic v0.1.0\n")
    token = get_token()

    # Step 1 — pull the full owner list once
    owner_map = get_all_owners(token)

    # Step 2 — for each test email, look up company by domain, get owner_id, resolve name
    print("\n--- DOMAIN -> OWNER_ID -> NAME CHAIN ---")
    for email in TEST_EMAILS:
        domain = email.split('@')[-1].lower()
        print(f"\nEmail:  {email}")
        print(f"Domain: {domain}")

        company_id, owner_id = get_company_owner_id(token, domain)
        print(f"Company ID:    {company_id}")
        print(f"Raw owner_id:  {owner_id}")

        if owner_id:
            # Try resolving from the bulk map first
            name_from_map = owner_map.get(str(owner_id), 'NOT FOUND IN MAP')
            print(f"Name from map: {name_from_map}")

            # Also try direct lookup to confirm
            name_direct, owner_email = get_owner_name(token, owner_id)
            print(f"Name direct:   {name_direct}  ({owner_email})")
        else:
            print("No owner_id returned — company may have no assigned owner in HubSpot")

    print("\n✅ Diagnostic complete.")

if __name__ == "__main__":
    main()
