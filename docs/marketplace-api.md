# Marketplace API Spec

This is the cloud marketplace contract. The local CLI implements the workspace-side pieces needed for it, and the cloud API now persists an org-scoped catalog under the workspace root so workflows visible to that org survive process restarts:

- `list-workflows`
- `search-workflows`
- `publish-workflow`
- `share-workflow` as the local public-library alias
- `withdraw-workflow` to mark a local workflow private again
- `cloud-pull-workflow` to download a public workflow into a workspace
- `cloud-run --workflow-id` to resolve a marketplace workflow into `workflow_yaml` before execution

The cloud service also exposes workflow detail and download endpoints:

- `GET /api/workflows/{id}`
- `GET /api/workflows/{id}/download`
- `DELETE /api/workflows/{id}`

## GET /api/workflows

List workflows visible to the current org.

Query parameters:

- `visibility`: optional, defaults to `all` for the current org. Use `public`, `private`, `all`, or `*`.
- `category`: optional workflow category filter.
- `tag`: optional tag filter, repeatable.
- `limit`: optional integer, default `50`, max `100`.
- `cursor`: optional pagination cursor.

Response:

```json
{
  "workflows": [
    {
      "id": "wf_123",
      "name": "login_basic",
      "description": "Verify a basic login flow.",
      "tags": ["verification", "auth"],
      "visibility": "public",
      "author": "visual-agent-team",
      "license": "cc-by-4.0",
      "org": "team-a",
      "owner_user_id": "alice",
      "version": 1,
      "quality_score": 82,
      "downloads": 120,
      "created_at": "2026-06-08T00:00:00Z",
      "updated_at": "2026-06-08T00:00:00Z"
    }
  ],
  "next_cursor": null
}
```

`id` can be either the workflow id or the workflow name.

`workflow_yaml` is intentionally omitted from list/search responses and is only returned by the detail and download endpoints.

The catalog is scoped by `X-Visual-Agent-Org`. Workflows published by one org are not visible to another org unless the same underlying catalog directory is used.

## GET /api/workflows/search

Search workflows visible to the current org by name, description, and tags.

Query parameters:

- `q`: required search query.
- `visibility`: optional, defaults to `all` for the current org.
- `limit`: optional integer, default `20`, max `50`.

Response uses the same shape as `GET /api/workflows`, with an additional `score` field on each workflow.

## GET /api/workflows/{id}/download

Download the published workflow YAML for import or execution.

Response:

```json
{
  "schema_version": 1,
  "workflow_id": "wf_123",
  "name": "login_basic",
  "workflow_yaml": "schema_version: 1\n..."
}
```

## POST /api/workflows/publish

Publish or update a workflow.

Authentication:

- `Authorization: Bearer <api_key>`
- `X-Visual-Agent-Org: <org>` is recommended for multi-user deployments.
- `X-Visual-Agent-User: <user_id>` is recorded as the publishing actor when present.

Request:

```json
{
  "name": "login_basic",
  "description": "Verify a basic login flow.",
  "tags": ["verification", "auth"],
  "visibility": "public",
  "author": "visual-agent-team",
  "license": "cc-by-4.0",
  "workflow_yaml": "schema_version: 1\n..."
}
```

`workflow_yaml` is the preferred publish payload. If omitted, the cloud API falls back to the workspace copy by `name`.

`visibility` may be `public` or `private`. Private workflows stay visible within the publishing org's catalog but are not synced across orgs.

## DELETE /api/workflows/{id}

Remove a workflow from the org-scoped catalog and suppress workspace resync for the same workflow name or id.

Authentication:

- `Authorization: Bearer <api_key>`
- `X-Visual-Agent-Org` selects the org-scoped catalog to modify.
- `X-Visual-Agent-User` is recorded in audit logs when present.

Response:

```json
{
  "status": "deleted",
  "workflow_id": "wf_123",
  "workflow": {
    "id": "wf_123",
    "name": "login_basic"
  }
}
```

Marketplace clients can also use the environment variables `VISUAL_AGENT_CLOUD_MARKETPLACE_ENDPOINT`, `VISUAL_AGENT_CLOUD_MARKETPLACE_API_KEY`, `VISUAL_AGENT_CLOUD_MARKETPLACE_ORG`, and `VISUAL_AGENT_CLOUD_MARKETPLACE_USER` for lookup and download flows.

The local workspace counterpart is `withdraw-workflow`, which rewrites the YAML visibility to `private` and updates the local index accordingly.

Validation rules:

- `visibility` must be `public` or `private`.
- `license` is required; `cc-by-4.0` is recommended.
- Workflow YAML must pass strict schema validation.
- `quality_score` must be at least `60`.
- Secrets, cookies, tokens, and private URLs are rejected.

Response:

```json
{
  "status": "published",
  "id": "wf_123",
  "name": "login_basic",
  "version": 1,
  "visibility": "public",
  "org": "team-a",
  "user_id": "alice",
  "quality_score": 82,
  "url": "https://visualagent.dev/workflows/wf_123"
}
```

