#!/usr/bin/env python3
"""
Generate a list of link _ids to delete from PAIC IDM.

Two modes:
  - "recon":   Find the latest recon, extract FOUND_ALREADY_LINKED entries,
               resolve each to its link object, and write link _ids to a file.
  - "mapping": Query all links for the mapping directly via /repo/link and
               write all link _ids to a file.
"""

import json
import secrets
import sys
import time

import jwt
import requests
from jwcrypto import jwk

# ──────────────────────────────────────────────────────────────────────────────
# CONSTANTS — update these before running
# ──────────────────────────────────────────────────────────────────────────────
TENANT_HOST = ""  # PAIC tenant FQDN
SERVICE_ACCOUNT_ID = ""  # Service account UUID
SERVICE_ACCOUNT_JWK_FILE = ""  # Path to service account JWK JSON file
MAPPING_NAME = ""  # IDM mapping name to resolve
SCOPE = "fr:idm:*"
MODE = "recon"  # "recon" = from FOUND_ALREADY_LINKED entries, "mapping" = all links for the mapping
OUTPUT_FILE = "link_ids.txt"  # Output file for link _ids
# ──────────────────────────────────────────────────────────────────────────────

BASE_URL = f"https://{TENANT_HOST}/openidm"
TOKEN_ENDPOINT = f"https://{TENANT_HOST}/am/oauth2/access_token"
HEADERS = {
    "Accept-API-Version": "resource=1.0",
    "Content-Type": "application/json",
}


class TokenManager:
    """Token manager that refreshes on 401 errors."""

    def __init__(self):
        self._token = None
        with open(SERVICE_ACCOUNT_JWK_FILE) as f:
            jwk_data = json.load(f)
        key = jwk.JWK(**jwk_data)
        self._private_key_pem = key.export_to_pem(private_key=True, password=None)

    def refresh(self):
        """Fetch a new access token via JWT bearer assertion."""
        now = int(time.time())
        payload = {
            "iss": SERVICE_ACCOUNT_ID,
            "sub": SERVICE_ACCOUNT_ID,
            "aud": TOKEN_ENDPOINT,
            "exp": now + 899,
            "jti": secrets.token_urlsafe(16),
        }
        signed_jwt = jwt.encode(payload, self._private_key_pem, algorithm="RS256")

        resp = requests.post(
            TOKEN_ENDPOINT,
            data={
                "client_id": "service-account",
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": signed_jwt,
                "scope": SCOPE,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        resp.raise_for_status()
        data = resp.json()
        self._token = data["access_token"]
        print(f"  Token acquired (expires in {data.get('expires_in', '?')}s)")

    @property
    def token(self):
        if not self._token:
            self.refresh()
        return self._token


# Global token manager
token_mgr = None


def _do_request(method, path, params=None, rev=None):
    """Execute an API request, retry once on 401 with a refreshed token."""
    url = f"{BASE_URL}{path}"
    for attempt in range(2):
        headers = {**HEADERS, "Authorization": f"Bearer {token_mgr.token}"}
        if rev:
            headers["If-Match"] = rev
        resp = method(url, headers=headers, params=params)
        if resp.status_code == 401 and attempt == 0:
            print("  Token expired, refreshing...")
            token_mgr.refresh()
            continue
        resp.raise_for_status()
        return resp.json()


def api_get(path, params=None):
    """GET request to PAIC IDM API."""
    return _do_request(requests.get, path, params=params)


def find_latest_recon():
    """Find the latest recon run for the given mapping."""
    data = api_get("/recon")
    recons = [
        r for r in data.get("reconciliations", [])
        if r.get("mapping") == MAPPING_NAME
    ]
    if not recons:
        print(f"ERROR: No recon found for mapping '{MAPPING_NAME}'")
        sys.exit(1)

    recons.sort(key=lambda r: r.get("started", ""), reverse=True)
    latest = recons[0]
    return latest


def get_found_already_linked_entries(recon_id):
    """Get all FOUND_ALREADY_LINKED entries from the recon, handling pagination."""
    entries = []
    cookie = None
    page = 0

    while True:
        params = {
            "_queryFilter": 'situation eq "FOUND_ALREADY_LINKED"',
            "_pageSize": "500",
        }
        if cookie:
            params["_pagedResultsCookie"] = cookie

        data = api_get(f"/recon/assoc/{recon_id}/entry", params=params)
        batch = data.get("result", [])
        entries.extend(batch)
        page += 1
        print(f"  Fetched page {page}: {len(batch)} entries")

        cookie = data.get("pagedResultsCookie")
        if not cookie or len(batch) == 0:
            break

    return entries


def find_link(source_id, target_id):
    """Find the link object for the given source/target pair matching our mapping."""
    search_ids = set(filter(None, [source_id, target_id]))
    candidates = []

    for search_id in search_ids:
        for field in ["firstId", "secondId"]:
            data = api_get(
                "/repo/link",
                params={"_queryFilter": f'{field} eq "{search_id}"'}
            )
            candidates.extend(data.get("result", []))

    # Deduplicate by _id
    seen = set()
    unique = []
    for c in candidates:
        if c["_id"] not in seen:
            seen.add(c["_id"])
            unique.append(c)

    # Filter to links matching our mapping's linkType
    matching = [c for c in unique if c.get("linkType") == MAPPING_NAME]
    return matching


def get_all_links_for_mapping():
    """Get all link _ids for the mapping directly via /repo/link, handling pagination."""
    link_ids = []
    cookie = None
    page = 0

    while True:
        params = {
            "_queryFilter": f'linkType eq "{MAPPING_NAME}"',
            "_pageSize": "500",
        }
        if cookie:
            params["_pagedResultsCookie"] = cookie

        data = api_get("/repo/link", params=params)
        batch = data.get("result", [])
        for link in batch:
            link_ids.append(link["_id"])
        page += 1
        print(f"  Fetched page {page}: {len(batch)} links")

        cookie = data.get("pagedResultsCookie")
        if not cookie or len(batch) == 0:
            break

    return link_ids


def generate_from_recon():
    """Generate link _ids from the latest recon's FOUND_ALREADY_LINKED entries."""
    # Find latest recon
    print("Finding latest recon...")
    recon = find_latest_recon()
    recon_id = recon["_id"]
    fal_count = recon.get("situationSummary", {}).get("FOUND_ALREADY_LINKED", 0)
    print(f"  Recon ID: {recon_id}")
    print(f"  State:    {recon['state']}")
    print(f"  Started:  {recon.get('started')}")
    print(f"  FOUND_ALREADY_LINKED: {fal_count}")
    print()

    if fal_count == 0:
        print("No FOUND_ALREADY_LINKED items found. Nothing to do.")
        return []

    # Get FOUND_ALREADY_LINKED entries
    print("Fetching FOUND_ALREADY_LINKED entries...")
    entries = get_found_already_linked_entries(recon_id)
    print(f"  Total entries: {len(entries)}")
    print()

    if not entries:
        print("No entries found (association data may not exist). Was recon run with persistAssociations=true?")
        return []

    # Find links and collect _ids
    link_ids = []
    not_found = 0

    for i, entry in enumerate(entries, 1):
        source_id = entry.get("sourceObjectId")
        target_id = entry.get("targetObjectId")
        print(f"[{i}/{len(entries)}] source={source_id} target={target_id}")

        links = find_link(source_id, target_id)
        if not links:
            print(f"  WARNING: No matching link found for linkType={MAPPING_NAME}")
            not_found += 1
            continue

        for link in links:
            link_id = link["_id"]
            print(f"  Found link: {link_id} (firstId={link['firstId']}, secondId={link['secondId']})")
            link_ids.append(link_id)

    print()
    print(f"  Links not found: {not_found}")
    return link_ids


def generate_from_mapping():
    """Generate link _ids by querying all links for the mapping directly."""
    print(f"Querying all links for linkType={MAPPING_NAME}...")
    link_ids = get_all_links_for_mapping()
    print(f"  Total links: {len(link_ids)}")
    return link_ids


def main():
    if MODE not in ("recon", "mapping"):
        print(f"ERROR: Invalid MODE '{MODE}'. Must be 'recon' or 'mapping'.")
        sys.exit(1)

    print(f"Tenant:  {TENANT_HOST}")
    print(f"Mapping: {MAPPING_NAME}")
    print(f"Mode:    {MODE}")
    print(f"Output:  {OUTPUT_FILE}")
    print()

    # Initialize token manager
    global token_mgr
    print("Initializing token manager...")
    token_mgr = TokenManager()
    print()

    # Generate link _ids based on mode
    if MODE == "recon":
        link_ids = generate_from_recon()
    else:
        link_ids = generate_from_mapping()

    if not link_ids:
        print("No link _ids found. Nothing to write.")
        return

    # Deduplicate (recon mode may have duplicates)
    unique_ids = list(dict.fromkeys(link_ids))

    # Write to file
    with open(OUTPUT_FILE, "w") as f:
        for lid in unique_ids:
            f.write(lid + "\n")

    # Summary
    print()
    print("=" * 60)
    print("Summary")
    print("=" * 60)
    print(f"  Mode:             {MODE}")
    print(f"  Links found:      {len(link_ids)}")
    print(f"  Unique link _ids: {len(unique_ids)}")
    print(f"  Output file:      {OUTPUT_FILE}")
    print()
    print(f"Review {OUTPUT_FILE}, then run delete_links.py to delete them.")


if __name__ == "__main__":
    main()
