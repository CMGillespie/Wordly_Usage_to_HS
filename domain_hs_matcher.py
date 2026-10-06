#!/usr/bin/env python3
# domain_hs_matcher.py
# VERSION: 1.2.0
# CHANGES FROM v1.1.0:
#   - Smarter keyword extraction: splits compound words (countyofmonterey → monterey)
#   - Tries multiple keyword strategies before giving up
#   - Substring search for long single-token domains
# RUN FROM: /Users/wordly_apps/Documents/Code/Wordly_Usage_to_HS/
# OUTPUT: domain_match_results.csv

import re
import time
import requests
import pandas as pd
import pandas_gbq

BQ_PROJECT = 'support-467322'
HS_KEY_FILE = 'HS_Service_key.txt'
OUTPUT_FILE = 'domain_match_results.csv'

GENERIC_DOMAINS = {
    'gmail.com','yahoo.com','hotmail.com','outlook.com','icloud.com',
    'aol.com','live.com','me.com','mac.com','msn.com','ymail.com',
    'protonmail.com','mail.com','inbox.com','gmx.com','zoho.com',
    'yahoo.fr','yahoo.co.uk','yahoo.co.jp','comcast.net','mail.ru',
    'web.de','gmx.net','qq.com','googlemail.com'
}

STRIP_TLDS = {
    'com','org','net','gov','edu','co','uk','us','ca','au','de','fr',
    'it','es','jp','cn','br','in','nl','be','ch','dk','se','no','fi',
    'pl','cz','hu','ro','gr','pt','mx','ar','cl','pe','za','nz','sg',
    'hk','tw','kr','ae','sa','il','tr','ru','ua','id','ph','my','vn',
    'th','pk','bd','eg','ma','gh','ke','ng','tz','ug','zm','zw','one',
    'online','info','biz','mobi','tel','ac','ne','or'
}

NOISE_WORDS = {
    'the','and','of','for','with','www','mail','info','web','my',
    'city','county','school','schools','church','group','global',
    'management','services','solutions','consulting','technology',
    'media','studio','studio','productions','agency','events','brand'
}

# Common English words that appear at the start of domain compound words
# Used to strip prefix and get the more distinctive part
COMMON_PREFIXES = ['city', 'county', 'school', 'church', 'brand', 'web', 'mail', 'my', 'the']


def strip_tlds(domain):
    """Remove TLD parts from domain, return remaining parts."""
    parts = domain.split('.')
    # Remove known TLD parts from the end (handle multi-part TLDs like co.uk, ac.jp)
    while len(parts) > 1 and parts[-1].lower() in STRIP_TLDS:
        parts.pop()
    while len(parts) > 1 and parts[-1].lower() in STRIP_TLDS:
        parts.pop()
    return parts


def extract_keywords(domain):
    """
    Extract search keywords from domain with multiple strategies:
    1. Split on dots/hyphens/underscores
    2. For long compound words, try CamelCase split
    3. For long compound words, try suffix (last N chars)
    4. Return multiple candidate keywords to try in order
    """
    parts = strip_tlds(domain)
    candidates = []

    for part in parts:
        if len(part) <= 2:
            continue

        # Split on hyphens/underscores first
        tokens = re.split(r'[-_]+', part)

        for token in tokens:
            if len(token) <= 2 or token.lower() in NOISE_WORDS:
                continue

            if len(token) <= 12:
                # Short enough to use directly
                candidates.append(token)
            else:
                # Long compound word — try multiple strategies

                # Strategy 1: CamelCase split
                camel_parts = re.sub(r'([a-z])([A-Z])', r'\1 \2', token).split()
                meaningful_camel = [p for p in camel_parts if len(p) > 2 and p.lower() not in NOISE_WORDS]
                if len(meaningful_camel) > 1:
                    candidates.extend(meaningful_camel)
                    continue

                # Strategy 2: strip known prefix words and use remainder
                lower = token.lower()
                stripped = token
                for prefix in COMMON_PREFIXES:
                    if lower.startswith(prefix) and len(token) > len(prefix) + 3:
                        stripped = token[len(prefix):]
                        break

                if stripped != token and len(stripped) > 3:
                    candidates.append(stripped)

                # Strategy 3: use last meaningful chunk (last 6-10 chars)
                # Good for: countyofmonterey → monterey, cityofgilroy → gilroy
                suffix = token[-8:] if len(token) > 8 else token[-6:]
                if suffix not in candidates and len(suffix) > 3:
                    candidates.append(suffix)

                # Strategy 4: use first meaningful chunk
                prefix_chunk = token[:8] if len(token) > 8 else token[:6]
                if prefix_chunk not in candidates and len(prefix_chunk) > 3:
                    candidates.append(prefix_chunk)

    # Deduplicate preserving order
    seen = set()
    result = []
    for c in candidates:
        if c.lower() not in seen:
            seen.add(c.lower())
            result.append(c)

    return result


def get_token():
    with open(HS_KEY_FILE, 'r') as f:
        return f.read().strip()


def get_unmatched_domains():
    print("📡 Loading unmatched corporate domains from BQ...")
    generic_list = ','.join([f"'{d}'" for d in GENERIC_DOMAINS])
    query = f"""
        SELECT 
          SPLIT(Owner_Email, '@')[OFFSET(1)] as email_domain,
          COUNT(*) as account_count,
          SUM(Consumed_Mins) as total_consumed,
          STRING_AGG(DISTINCT Service, ', ') as services,
          STRING_AGG(DISTINCT hs_account_owner, ', ') as account_owners
        FROM `support-467322.wordly_usage_data.current_usage_clean`
        WHERE hs_company_name = ''
          AND Service IN ('Active', 'Restricted')
          AND SPLIT(Owner_Email, '@')[OFFSET(1)] NOT IN ({generic_list})
        GROUP BY email_domain
        ORDER BY total_consumed DESC
    """
    df = pandas_gbq.read_gbq(query, project_id=BQ_PROJECT)
    print(f"   ✅ Found {len(df)} unique unmatched corporate domains")
    return df


def search_by_domain(token, domain):
    headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
    payload = {
        "filterGroups": [{"filters": [{"propertyName": "domain", "operator": "EQ", "value": domain}]}],
        "properties": ["name", "domain"],
        "limit": 5
    }
    try:
        r = requests.post(
            "https://api.hubapi.com/crm/v3/objects/companies/search",
            headers=headers, json=payload, timeout=15
        )
        if r.status_code == 200:
            return r.json().get('results', [])
    except Exception as e:
        print(f"      ⚠️  Domain search error for {domain}: {e}")
    return []


def search_by_name(token, keyword):
    headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
    payload = {
        "query": keyword,
        "properties": ["name", "domain"],
        "limit": 5
    }
    try:
        r = requests.post(
            "https://api.hubapi.com/crm/v3/objects/companies/search",
            headers=headers, json=payload, timeout=15
        )
        if r.status_code == 200:
            return r.json().get('results', [])
    except Exception as e:
        print(f"      ⚠️  Name search error for {keyword}: {e}")
    return []


def safe_join(values):
    return ' | '.join([v or '' for v in values])


def main():
    token = get_token()
    domains_df = get_unmatched_domains()

    matched = []
    fuzzy = []
    ambiguous = []
    not_found = []

    total = len(domains_df)
    print(f"\n🔍 Searching HubSpot for {total} domains...\n")

    for _, row in domains_df.iterrows():
        domain = row['email_domain']
        account_count = row['account_count']
        total_consumed = row['total_consumed']
        services = row['services']
        account_owners = row['account_owners']

        base = {
            'email_domain': domain,
            'account_count': account_count,
            'total_consumed': total_consumed,
            'services': services,
            'account_owners': account_owners,
        }

        # Step 1 — exact domain search
        results = search_by_domain(token, domain)
        time.sleep(0.12)

        if len(results) == 1:
            company = results[0]
            matched.append({**base,
                'status': 'MATCHED',
                'hs_company_name': company['properties'].get('name', ''),
                'hs_company_domain': company['properties'].get('domain') or '',
                'hs_company_id': company['id'],
                'match_method': 'exact domain'
            })
            print(f"   ✅ EXACT: {domain} → {company['properties'].get('name', '')}")
            continue

        if len(results) > 1:
            names = [r['properties'].get('name', '') for r in results]
            ambiguous.append({**base,
                'status': 'AMBIGUOUS',
                'hs_company_name': safe_join(names),
                'hs_company_domain': safe_join([r['properties'].get('domain') for r in results]),
                'hs_company_id': safe_join([r['id'] for r in results]),
                'match_method': 'exact domain - multiple'
            })
            print(f"   ⚠️  AMBIGUOUS: {domain} → {', '.join(names)}")
            continue

        # Step 2 — fuzzy name search using extracted keywords
        keywords = extract_keywords(domain)
        fuzzy_results = []
        fuzzy_keyword = ''

        for kw in keywords:
            if len(kw) < 3:
                continue
            results = search_by_name(token, kw)
            time.sleep(0.12)
            if len(results) == 1:
                fuzzy_results = results
                fuzzy_keyword = kw
                break
            elif len(results) > 1:
                # Multiple — keep looking for a more specific keyword
                # But save this as a fallback
                if not fuzzy_results:
                    fuzzy_results = results
                    fuzzy_keyword = kw

        if len(fuzzy_results) == 1:
            company = fuzzy_results[0]
            fuzzy.append({**base,
                'status': 'FUZZY MATCH - REVIEW',
                'hs_company_name': company['properties'].get('name', ''),
                'hs_company_domain': company['properties'].get('domain') or '',
                'hs_company_id': company['id'],
                'match_method': f'keyword: {fuzzy_keyword}'
            })
            print(f"   🔶 FUZZY: {domain} → {company['properties'].get('name', '')} (keyword: {fuzzy_keyword})")
        elif len(fuzzy_results) > 1:
            names = [r['properties'].get('name') or '' for r in fuzzy_results]
            ambiguous.append({**base,
                'status': 'AMBIGUOUS',
                'hs_company_name': safe_join(names),
                'hs_company_domain': safe_join([r['properties'].get('domain') for r in fuzzy_results]),
                'hs_company_id': safe_join([r['id'] for r in fuzzy_results]),
                'match_method': f'keyword: {fuzzy_keyword} - multiple'
            })
            print(f"   ⚠️  AMBIGUOUS FUZZY: {domain} → {', '.join(names)}")
        else:
            not_found.append({**base,
                'status': 'NOT IN HUBSPOT',
                'hs_company_name': '',
                'hs_company_domain': '',
                'hs_company_id': '',
                'match_method': f'tried keywords: {keywords}'
            })
            print(f"   ❌ NOT FOUND: {domain} (tried: {keywords})")

    all_results = matched + fuzzy + ambiguous + not_found
    results_df = pd.DataFrame(all_results)
    results_df = results_df.sort_values(
        ['status', 'total_consumed'],
        ascending=[True, False]
    )
    results_df.to_csv(OUTPUT_FILE, index=False)

    print(f"\n{'='*60}")
    print(f"RESULTS SUMMARY")
    print(f"  ✅ Exact match:              {len(matched)}")
    print(f"  🔶 Fuzzy match (review):     {len(fuzzy)}")
    print(f"  ⚠️  Ambiguous:               {len(ambiguous)}")
    print(f"  ❌ Not in HubSpot:           {len(not_found)}")
    print(f"{'='*60}")
    print(f"\n📄 Full results saved to: {OUTPUT_FILE}")


if __name__ == '__main__':
    main()