#!/usr/bin/env python3
"""
Delete link objects from PAIC IDM by reading link _ids from a text file.

Reads link _ids (one per line) from the input file, fetches each link to get
the current _rev, and deletes it.
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
SCOPE = "fr:idm:*"
INPUT_FILE = "link_ids.txt"  # Input file with link _ids (one per line)
DRY_RUN = True  # Set to False to actually delete
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


def api_delete(path, rev):
    """DELETE request to PAIC IDM API with If-Match rev."""
    return _do_request(requests.delete, path, rev=rev)


def main():
    print(f"Tenant:  {TENANT_HOST}")
    print(f"Input:   {INPUT_FILE}")
    print(f"Mode:    {'DRY RUN' if DRY_RUN else 'DELETE'}")
    print()

    # Load link _ids from file
    try:
        with open(INPUT_FILE) as f:
            link_ids = [line.strip() for line in f if line.strip()]
    except FileNotFoundError:
        print(f"ERROR: Input file '{INPUT_FILE}' not found.")
        print("Run generate_link_ids.py first to create it.")
        sys.exit(1)

    if not link_ids:
        print("No link _ids found in input file. Nothing to do.")
        return

    print(f"Loaded {len(link_ids)} link _ids from {INPUT_FILE}")
    print()

    # Initialize token manager
    global token_mgr
    print("Initializing token manager...")
    token_mgr = TokenManager()
    print()

    # Process each link
    deleted = 0
    failed = 0
    not_found = 0

    for i, link_id in enumerate(link_ids, 1):
        print(f"[{i}/{len(link_ids)}] link _id={link_id}")

        # Fetch the link to get current _rev
        try:
            link = api_get(f"/repo/link/{link_id}")
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                print(f"  WARNING: Link {link_id} not found (already deleted?)")
                not_found += 1
                continue
            raise

        link_rev = link["_rev"]
        print(f"  Found link (firstId={link.get('firstId')}, secondId={link.get('secondId')}, rev={link_rev})")

        if DRY_RUN:
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
    print(f"  Total link _ids: {len(link_ids)}")
    print(f"  Links deleted:   {deleted}")
    print(f"  Links not found: {not_found}")
    print(f"  Failures:        {failed}")
    if DRY_RUN:
        print()
        print("  This was a DRY RUN. Set DRY_RUN = False to actually delete.")


if __name__ == "__main__":
    main()
