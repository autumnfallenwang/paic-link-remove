#!/usr/bin/env python3
"""
Resolve FOUND_ALREADY_LINKED items from a PAIC recon.

Finds the latest recon for the given mapping, extracts all FOUND_ALREADY_LINKED
entries, locates their link objects, and deletes them so the next recon can
re-link correctly.
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
SAMPLE_SIZE = -1  # -1 = process all, 0 = dry run (find only), N > 0 = process first N entries
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
        # Load JWK once and convert to PEM
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


def api_delete(path, rev):
    """DELETE request to PAIC IDM API with If-Match rev."""
    return _do_request(requests.delete, path, rev=rev)


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

    # Sort by started timestamp descending, pick latest
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
    # Search by both IDs in both positions
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


def main():
    mode = "DRY RUN" if SAMPLE_SIZE == 0 else f"SAMPLE ({SAMPLE_SIZE})" if SAMPLE_SIZE > 0 else "ALL"
    print(f"Tenant:  {TENANT_HOST}")
    print(f"Mapping: {MAPPING_NAME}")
    print(f"Mode:    {mode}")
    print()

    # Step 1: Initialize token manager
    global token_mgr
    print("Initializing token manager...")
    token_mgr = TokenManager()
    print()

    # Step 2: Find latest recon
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
        return

    # Step 3: Get FOUND_ALREADY_LINKED entries
    print("Fetching FOUND_ALREADY_LINKED entries...")
    entries = get_found_already_linked_entries(recon_id)
    print(f"  Total entries: {len(entries)}")
    print()

    if not entries:
        print("No entries found (association data may not exist). Was recon run with persistAssociations=true?")
        return

    # Step 4: Find and delete links
    deleted = 0
    failed = 0
    not_found = 0
    dry_run = SAMPLE_SIZE == 0

    if SAMPLE_SIZE > 0:
        to_process = entries[:SAMPLE_SIZE]
        print(f"SAMPLE MODE: processing first {len(to_process)} of {len(entries)} entries")
    elif SAMPLE_SIZE == 0:
        to_process = entries
        print(f"DRY RUN MODE: listing all {len(entries)} entries without deleting")
    else:
        to_process = entries

    print()

    for i, entry in enumerate(to_process, 1):
        source_id = entry.get("sourceObjectId")
        target_id = entry.get("targetObjectId")
        print(f"[{i}/{len(to_process)}] source={source_id} target={target_id}")

        links = find_link(source_id, target_id)
        if not links:
            print(f"  WARNING: No matching link found for linkType={MAPPING_NAME}")
            not_found += 1
            continue

        for link in links:
            link_id = link["_id"]
            link_rev = link["_rev"]
            print(f"  Found link: {link_id} (firstId={link['firstId']}, secondId={link['secondId']})")

            if dry_run:
                print(f"  [DRY RUN] Would delete link {link_id}")
            else:
                try:
                    api_delete(f"/repo/link/{link_id}", link_rev)
                    print(f"  DELETED link {link_id}")
                    deleted += 1
                except Exception as e:
                    print(f"  ERROR deleting link {link_id}: {e}")
                    failed += 1

    # Summary
    print()
    print("=" * 60)
    print("Summary")
    print("=" * 60)
    print(f"  Total FOUND_ALREADY_LINKED entries: {len(entries)}")
    if SAMPLE_SIZE > 0:
        print(f"  Sampled:         {len(to_process)}")
    print(f"  Links deleted:   {deleted}")
    print(f"  Links not found: {not_found}")
    print(f"  Failures:        {failed}")
    if dry_run:
        print()
        print("  This was a DRY RUN. Set SAMPLE_SIZE = -1 to delete all.")


if __name__ == "__main__":
    main()
