# Backup — initial HTTP handler

This is a **frozen backup** of the first version of the Lambda, kept for
reference only. It is **not deployed** and not wired into the Terraform.

This version was a direct request/response handler: it received the
transcription JSON in the invocation body (API Gateway style), fetched a
Keycloak token, and POSTed straight to a single hard-coded endpoint. Credentials
came from `CLIENT_ID` / `CLIENT_SECRET` environment variables.

The active Lambda in [`../empatia-tipificaciones`](../empatia-tipificaciones)
supersedes it: it is triggered by S3 → EventBridge → SQS, resolves per-client
routing and credentials from SSM Parameter Store, and supports multiple
endpoints without code changes.

| | This backup | Active handler |
| --- | --- | --- |
| Trigger | Direct invoke / API Gateway body | SQS (S3 Object Created events) |
| Endpoint | Single, hard-coded `b_occ` | Multi-client via SSM |
| Credentials | `CLIENT_ID` / `CLIENT_SECRET` env vars | SSM SecureString |
