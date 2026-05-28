# Version 0.3.0 - Bypass Lookup Probe
# Purpose: Use direct HTTP request to get Owner details (bypasses SDK SDK discovery errors)

import requests

def get_api_key():
    with open('HS_Service_key.txt', 'r') as file:
        return file.read().strip()

def test_owner_lookup_direct(owner_id):
    api_token = get_api_key()
    url = f"https://api.hubapi.com/crm/v3/owners/{owner_id}"
    headers = {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/json"
    }
    
    print(f"--- Probing Owner ID: {owner_id} via Direct Request ---")
    
    response = requests.get(url, headers=headers)
    
    if response.status_code == 200:
        data = response.json()
        print("\n--- TRANSLATION SUCCESSFUL ---")
        print(f"First Name: {data.get('firstName')}")
        print(f"Last Name:  {data.get('lastName')}")
        print(f"Email:      {data.get('email')}")
    else:
        print(f"Error: {response.status_code}")
        print(response.text)

if __name__ == "__main__":
    # The ID we found: 251327829
    test_owner_lookup_direct(251327829)