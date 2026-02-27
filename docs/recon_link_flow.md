# PAIC Recon & Link REST API Flow

## Overview

This document describes the relationships between REST calls used to investigate
and resolve `FOUND_ALREADY_LINKED` issues in PAIC IDM reconciliation.

## Flow Diagram

```
┌─────────────────────────────────────────────────────────────────────┐
│ 1. GET /openidm/recon                                               │
│    List all recon runs                                              │
│    → Pick your mapping name + timestamp                             │
│    → Returns: reconId, mapping, state, situationSummary             │
└──────────────────────┬──────────────────────────────────────────────┘
                       │ reconId
                       ▼
┌─────────────────────────────────────────────────────────────────────┐
│ 2. GET /openidm/recon/{reconId}                                     │
│    Get recon summary                                                │
│    → Confirm situationSummary.FOUND_ALREADY_LINKED > 0              │
│    → Also shows statusSummary, durationSummary                      │
└──────────────────────┬──────────────────────────────────────────────┘
                       │ reconId (same as assocId)
                       ▼
┌─────────────────────────────────────────────────────────────────────┐
│ 3. GET /openidm/recon/assoc/{assocId}/entry                         │
│    ?_queryFilter=situation eq "FOUND_ALREADY_LINKED"                 │
│    Get per-entry details for FOUND_ALREADY_LINKED items             │
│    → Returns: sourceObjectId, targetObjectId, linkQualifier         │
│    → Requires recon was run with persistAssociations=true            │
│    → Supports pagination via _pageSize + _pagedResultsCookie        │
└──────────────────────┬──────────────────────────────────────────────┘
                       │ sourceObjectId, targetObjectId
                       ▼
┌─────────────────────────────────────────────────────────────────────┐
│ 4. GET /openidm/repo/link                                           │
│    ?_queryFilter=firstId eq "{id}" OR secondId eq "{id}"            │
│    Find the existing link object                                    │
│    → Search using BOTH sourceObjectId and targetObjectId            │
│    → Search BOTH firstId and secondId (link may be flipped)         │
│    → Filter results by linkType == your mapping name                │
│    → Returns: _id (linkId), _rev, firstId, secondId, linkType       │
└──────────────────────┬──────────────────────────────────────────────┘
                       │ linkId, _rev
                       ▼
┌─────────────────────────────────────────────────────────────────────┐
│ 5. DELETE /openidm/repo/link/{linkId}                               │
│    Header: If-Match: {_rev}                                         │
│    Delete the stale link                                            │
│    → _rev goes in If-Match header, NOT as query param               │
└──────────────────────┬──────────────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────────────────┐
│ 6. POST /openidm/recon                                              │
│    ?_action=recon&mapping={name}&persistAssociations=true            │
│    Rerun the recon                                                  │
│    → Item should now be FOUND instead of FOUND_ALREADY_LINKED       │
│    → A new correct link will be created                             │
└─────────────────────────────────────────────────────────────────────┘
```

## Key Relationships

### reconId = assocId
The `_id` returned from `GET /openidm/recon` is the same ID used to query
`/openidm/recon/assoc/{id}/entry`. They are the same value.

### sourceObjectId/targetObjectId → firstId/secondId
The recon entry gives you `sourceObjectId` and `targetObjectId`.
The link object stores them as `firstId` and `secondId`.

**IMPORTANT**: These may be **flipped** depending on which mapping created the link.
- If the link was created by the current mapping: firstId=source, secondId=target
- If the link was created by a reverse mapping: firstId=target, secondId=source

Always search both IDs in both positions to find the link.

### linkType = mapping name
The link object's `linkType` field matches the mapping name that created it.
When searching for links, filter by `linkType == MAPPING_NAME` to find the
correct link to delete.

## Supporting Calls

### List association sets
```
GET /openidm/recon/assoc?_queryFilter=true
```
Use this to verify that association data exists for a recon run.
If the recon was NOT run with `persistAssociations=true`, the assoc entry
won't exist and per-entry data won't be available.

### Trigger recon with persist
```
POST /openidm/recon?_action=recon&mapping={name}&persistAssociations=true&waitForCompletion=true
```
`persistAssociations=true` is a **runtime parameter**, not a mapping config change.
It tells this specific run to store per-entry association data.
`waitForCompletion=true` makes the call synchronous (blocks until done).

### Get token (service account)
```
POST /am/oauth2/access_token
Content-Type: application/x-www-form-urlencoded

client_id=service-account
grant_type=urn:ietf:params:oauth:grant-type:jwt-bearer
assertion={signed_jwt}
scope=fr:idm:*
```
JWT payload: `iss` and `sub` = service account ID, `aud` = token endpoint URL.
Signed with RS256 using the service account's JWK private key.

### Audit recon (alternative to assoc)
```
GET /openidm/audit/recon?_queryFilter=/reconId eq "{id}" and situation eq "FOUND_ALREADY_LINKED"
```
Alternative way to get per-entry data. Requires the `recon` audit topic to be
enabled on the tenant. Does NOT require `persistAssociations=true`.
