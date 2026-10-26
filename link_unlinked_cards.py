#!/usr/bin/env python3
# link_unlinked_cards.py
# VERSION: 0.1.0a
# WRITTEN BY: Claude
# ORIGIN: Project chat "Wordly Portal Data Analytics" (linking cards to contacts)
# DATE WRITTEN: 2026-10-26
# MODIFIED: (none yet)
# CHANGES: New file. One-time sweep that links existing Accounts Usage cards to their contact and company.
#
# WHAT IT DOES
#   Finds Accounts Usage cards that have no contact link, or no company link.
#   Matches each card to the HubSpot contact with the SAME email, then to that contact's primary company.
#   It NEVER creates a contact, a company, or a card. It only adds links.
#   It skips a card when no contact has that email, or when more than one contact has it.
#   Scope: only cards stamped with data_as_of (cards that are still in the nightly data).
#          Use --all-cards to include the old unstamped cards too.
#
# HOW TO RUN (in Cloud Shell, in the folder that holds HS_Service_key.txt)
#   python3 link_unlinked_cards.py                    Dry run. Changes nothing. Writes a plan CSV.
#   python3 link_unlinked_cards.py --apply --limit 5  Links the first 5 cards in the plan.
#   python3 link_unlinked_cards.py --apply            Links everything in the plan.

import argparse
import csv
import os
import sys
import time
from datetime import date

import requests

OBJECT_TYPE = '2-58523979'
BASE = 'https://api.hubapi.com'
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
KEY_FILE = os.path.join(SCRIPT_DIR, 'HS_Service_key.txt')


def load_headers():
    with open(KEY_FILE) as f:
        token = f.read().strip()
    return {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}


HEADERS = None


def call(method, url, **kwargs):
    """One HubSpot call with a simple retry on rate limits and server errors."""
    r = None
    for attempt in range(5):
        r = requests.request(method, url, headers=HEADERS, timeout=30, **kwargs)
        if r.status_code in (429, 500, 502, 503, 504):
            time.sleep(2 * (attempt + 1))
            continue
        return r
    return r


def list_cards():
    """Return every card as a dict with id, email, and data_as_of."""
    cards = []
    after = None
    while True:
        params = {'limit': 100, 'properties': 'owner_email,data_as_of'}
        if after:
            params['after'] = after
        r = call('GET', f'{BASE}/crm/v3/objects/{OBJECT_TYPE}', params=params)
        if r.status_code != 200:
            print(f"❌ Could not list cards: {r.status_code} {r.text[:200]}")
            sys.exit(1)
        data = r.json()
        for item in data.get('results', []):
            props = item.get('properties', {})
            cards.append({
                'id': str(item['id']),
                'email': (props.get('owner_email') or '').strip().lower(),
                'stamp': (props.get('data_as_of') or '').strip(),
            })
        after = data.get('paging', {}).get('next', {}).get('after')
        if not after:
            break
        time.sleep(0.1)
    return cards


def linked_map(card_ids, to_type):
    """Return {card id: [linked ids]} for cards that already have at least one link of this type."""
    linked = {}
    for i in range(0, len(card_ids), 100):
        chunk = card_ids[i:i + 100]
        r = call('POST', f'{BASE}/crm/v4/associations/{OBJECT_TYPE}/{to_type}/batch/read',
                 json={'inputs': [{'id': x} for x in chunk]})
        if r.status_code not in (200, 207):
            print(f"❌ Could not read {to_type} links: {r.status_code} {r.text[:200]}")
            sys.exit(1)  # stop. A wrong answer here would create wrong links.
        for item in r.json().get('results', []):
            from_id = str(item.get('from', {}).get('id'))
            to_ids = [str(t.get('toObjectId', t.get('id'))) for t in item.get('to', [])]
            if to_ids:
                linked[from_id] = to_ids
        time.sleep(0.15)
    return linked


_contact_cache = {}
_company_cache = {}


def find_contact(email):
    """Return ('one', id), ('none', None), or ('many', None)."""
    if email in _contact_cache:
        return _contact_cache[email]
    payload = {"filterGroups": [{"filters": [{"propertyName": "email", "operator": "EQ", "value": email}]}],
               "properties": ["email"], "limit": 2}
    r = call('POST', f'{BASE}/crm/v3/objects/contacts/search', json=payload)
    if r.status_code != 200:
        result = ('error', None)
    else:
        results = r.json().get('results', [])
        result = ('one', results[0]['id']) if len(results) == 1 else (('none', None) if not results else ('many', None))
    _contact_cache[email] = result
    time.sleep(0.12)
    return result


def primary_company(contact_id):
    """Return the contact's primary company id. If there is no primary, use the only company. Otherwise None."""
    if contact_id in _company_cache:
        return _company_cache[contact_id]
    r = call('GET', f'{BASE}/crm/v4/objects/contacts/{contact_id}/associations/companies')
    company_id = None
    if r.status_code == 200:
        results = r.json().get('results', [])
        for item in results:
            if any(t.get('typeId') == 1 for t in item.get('associationTypes', [])):
                company_id = str(item.get('toObjectId'))
                break
        if company_id is None and len(results) == 1:
            company_id = str(results[0].get('toObjectId'))
    _company_cache[contact_id] = company_id
    time.sleep(0.12)
    return company_id


def build_plan(all_cards):
    cards = list_cards()
    print(f"📋 Cards found: {len(cards)}")

    in_scope = [c for c in cards
                if c['email'] and not c['email'].endswith('@wordly.ai') and (all_cards or c['stamp'])]
    print(f"📋 In scope: {len(in_scope)} ({'all cards' if all_cards else 'cards stamped with data_as_of'}, internal and blank emails removed)")

    ids = [c['id'] for c in in_scope]
    contact_links = linked_map(ids, 'contacts')
    company_links = linked_map(ids, 'companies')

    plan = []
    counts = {'fully_linked': 0, 'no_contact_found': 0, 'many_contacts': 0, 'lookup_error': 0, 'no_company': 0}
    for c in in_scope:
        has_contact = c['id'] in contact_links
        has_company = c['id'] in company_links
        if has_contact and has_company:
            counts['fully_linked'] += 1
            continue

        if has_contact:
            contact_id, status = contact_links[c['id']][0], 'one'
        else:
            status, contact_id = find_contact(c['email'])
        if status != 'one':
            key = {'none': 'no_contact_found', 'many': 'many_contacts'}.get(status, 'lookup_error')
            counts[key] += 1
            continue

        company_id = None
        if not has_company:
            company_id = primary_company(contact_id)
            if not company_id:
                counts['no_company'] += 1
        add_contact = None if has_contact else contact_id
        if add_contact or company_id:
            plan.append({'card_id': c['id'], 'email': c['email'],
                         'add_contact': add_contact or '', 'add_company': company_id or ''})
    return plan, counts


def write_csv(path, rows, fields):
    with open(path, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def apply_plan(plan, limit):
    todo = plan[:limit] if limit else plan
    results = []
    ok_contacts = ok_companies = failed = 0
    for row in todo:
        base = f"{BASE}/crm/v4/objects/{OBJECT_TYPE}/{row['card_id']}/associations/default"
        contact_status = company_status = ''
        if row['add_contact']:
            r = call('PUT', f"{base}/contacts/{row['add_contact']}")
            contact_status = r.status_code
            if r.status_code in (200, 201):
                ok_contacts += 1
            else:
                failed += 1
        if row['add_company']:
            r = call('PUT', f"{base}/companies/{row['add_company']}")
            company_status = r.status_code
            if r.status_code in (200, 201):
                ok_companies += 1
            else:
                failed += 1
        results.append({**row, 'contact_status': contact_status, 'company_status': company_status})
        time.sleep(0.15)
    out = os.path.join(SCRIPT_DIR, f'link_results_{date.today().isoformat()}.csv')
    write_csv(out, results, ['card_id', 'email', 'add_contact', 'add_company', 'contact_status', 'company_status'])
    print(f"✅ Applied to {len(todo)} cards. Contact links added: {ok_contacts}. Company links added: {ok_companies}. Failed calls: {failed}.")
    print(f"📄 Results saved: {out}")


def main():
    global HEADERS
    parser = argparse.ArgumentParser(description='Link existing Accounts Usage cards to their contact and company')
    parser.add_argument('--apply', action='store_true', help='Actually add the links. Without this, nothing changes.')
    parser.add_argument('--limit', type=int, default=0, help='With --apply: only link the first N cards in the plan.')
    parser.add_argument('--all-cards', action='store_true', help='Include old cards without a data_as_of stamp.')
    args = parser.parse_args()

    HEADERS = load_headers()
    plan, counts = build_plan(args.all_cards)

    plan_path = os.path.join(SCRIPT_DIR, f'link_plan_{date.today().isoformat()}.csv')
    write_csv(plan_path, plan, ['card_id', 'email', 'add_contact', 'add_company'])

    print("\n===== PLAN =====")
    print(f"Cards that need a contact link: {sum(1 for p in plan if p['add_contact'])}")
    print(f"Cards that need a company link: {sum(1 for p in plan if p['add_company'])}")
    print(f"Already fully linked:           {counts['fully_linked']}")
    print(f"Skipped, no contact has that email:   {counts['no_contact_found']}")
    print(f"Skipped, several contacts share it:   {counts['many_contacts']}")
    print(f"Contact found but no company to link: {counts['no_company']}")
    print(f"Skipped, lookup error:                {counts['lookup_error']}")
    print(f"📄 Plan saved: {plan_path}")

    if not args.apply:
        print("\nDry run only. Nothing was changed. Add --apply to link the cards in the plan.")
        return
    apply_plan(plan, args.limit)


if __name__ == '__main__':
    main()
