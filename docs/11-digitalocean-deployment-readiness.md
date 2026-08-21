# UrbanMend — DigitalOcean Deployment Readiness

> Step-by-step execution tracker for deploying the current backend to one DigitalOcean Droplet
> with Docker Compose, PostgreSQL/PostGIS, Redis, MinIO, Celery, and HTTPS.

| | |
|---|---|
| **Document** | `docs/11-digitalocean-deployment-readiness.md` |
| **Status** | Not started — implementation checklist |
| **Target** | DigitalOcean Droplet running Ubuntu LTS and Docker Compose |
| **Scope** | Backend infrastructure and production validation; frontend deployment is excluded |
| **Related** | `06-devops-guide.md` · `09-operations.md` · `10-llm-deployment-evaluation.md` |

## 0. Target Architecture

The first production deployment uses one Droplet. This minimizes operational complexity for the
capstone while preserving the application's existing container and S3-compatible storage design.

```text
Internet
   |
DigitalOcean firewall: 22, 80, 443 only
   |
Caddy or Nginx: TLS termination and reverse proxy
   |-- api.<domain>       -> Django/Uvicorn:8080
   `-- storage.<domain>   -> MinIO S3 API:9000

Private Docker network
   |-- api
   |-- worker
   |-- beat (exactly one instance)
   |-- PostgreSQL 17 + PostGIS 3.5
   |-- Redis
   `-- MinIO + persistent media volume
```

The MinIO console, PostgreSQL, Redis, Prometheus metrics, and application container ports must not
be publicly reachable. The browser-facing MinIO endpoint is required because presigned media URLs
cannot contain the Docker-only hostname `storage`.

## 1. Readiness Definition

The deployment is ready only when all of these statements are true:

- [ ] A clean Droplet can be provisioned and deployed using documented commands.
- [ ] Production uses `urbenmend.settings.prod`; no development settings or credentials remain.
- [ ] Database migrations apply before application rollout.
- [ ] API, worker, and exactly one Celery Beat process run independently.
- [ ] A citizen can upload an image and retrieve its HTTPS presigned MinIO URL.
- [ ] Report classification, clustering, media processing, and notifications run asynchronously.
- [ ] PostgreSQL and MinIO data survive container replacement and Droplet restart.
- [ ] Database and media backups can be restored into an isolated environment.
- [ ] Rollback to a previous image has been rehearsed without reversing schema by default.
- [ ] Logs, health checks, uptime, resource usage, disk usage, and queue health are observable.
- [ ] The complete production smoke-test and security checklist passes.

## 2. Decisions to Record Before Implementation

Record the chosen values here before creating production infrastructure.

| Decision | Selected value | Status |
|---|---|---|
| DigitalOcean region | TBD | [ ] |
| Droplet size | Recommended starting point: 2 vCPU / 4 GB RAM; 8 GB preferred | [ ] |
| Domain registrar/DNS | TBD | [ ] |
| API hostname | `api.<domain>` | [ ] |
| MinIO hostname | `storage.<domain>` | [ ] |
| Reverse proxy | Caddy with automatic TLS | [x] |
| PostgreSQL hosting | Self-hosted on Droplet initially | [ ] |
| MinIO data location | Droplet disk or mounted DigitalOcean Volume | [ ] |
| Container registry | GitHub Container Registry (`ghcr.io`) | [ ] |
| SMTP provider/from address | Resend SMTP; sender domain TBD | [ ] |
| LLM provider/model | See `10-llm-deployment-evaluation.md` | [ ] |
| Backup destination | Must be outside the Droplet | [ ] |
| Monitoring/alert destination | Email, Discord, Slack, or equivalent | [ ] |

## 3. Phase 1 — Production Deployment Files

### 3.1 Create a production Compose file

- [x] Add `docker-compose.prod.yml` or `compose.prod.yml`.
- [x] Do not bind-mount source code.
- [x] Build or pull only the Dockerfile `runtime` image.
- [x] Add independent `api`, `worker`, and `beat` services.
- [x] Remove `-B` from the worker command.
- [x] Run exactly one Beat container.
- [x] Add health checks and restart policies.
- [x] Keep PostgreSQL, Redis, and MinIO on private Docker networks.
- [x] Publish no application or backing-service ports before the reverse proxy is added.
- [x] Use named volumes or explicit mounted paths for database, Redis, and MinIO data.
- [x] Add an idempotent MinIO bucket-initialization service for the private
  `urbenmend-media` bucket.
- [x] Set container log rotation limits so JSON logs cannot fill the disk.

Acceptance:

```powershell
docker compose -f docker-compose.prod.yml config
```

must render successfully without exposing secrets or development-only mounts/commands.

### 3.2 Add a reverse-proxy configuration

- [x] Add a version-controlled Caddyfile or Nginx configuration template.
- [x] Route the API hostname to `api:8080`.
- [x] Route the storage hostname to MinIO `storage:9000`.
- [x] Support SSE without response buffering or short proxy timeouts.
- [x] Forward the original host and `X-Forwarded-Proto=https`.
- [x] Configure a request-body limit at least equal to `MEDIA_MAX_UPLOAD_BYTES` plus overhead.
- [x] Serve Django static files or define a deliberate alternative.
- [x] Do not expose `/metrics` publicly.
- [x] Do not expose the MinIO administration console publicly.

### 3.3 Add production environment templates

- [x] Add a committed placeholder-only `.env.production.example`.
- [x] Keep the actual `.env.production` outside Git with mode `0600`.
- [x] Remove obsolete or duplicate variables such as unused `LLM_API_KEY` after verification.
- [x] Document generation and rotation of every secret.

Required settings include:

```dotenv
DJANGO_SETTINGS_MODULE=urbenmend.settings.prod
DJANGO_SECRET_KEY=<generated-secret>
DJANGO_ALLOWED_HOSTS=api.<domain>
DJANGO_CSRF_TRUSTED_ORIGINS=https://<frontend-domain>
DJANGO_HSTS_SECONDS=<start-low-then-increase>

DATABASE_URL=postgis://<user>:<password>@db:5432/<database>
DATABASE_SSLMODE=disable
REDIS_URL=redis://:<password>@redis:6379/0
CELERY_BROKER_URL=redis://:<password>@redis:6379/1

STORAGE_ENDPOINT=https://storage.<domain>
STORAGE_BUCKET=urbenmend-media
STORAGE_ACCESS_KEY=<generated-access-key>
STORAGE_SECRET_KEY=<generated-secret-key>
AWS_REGION=us-east-1
```

`DATABASE_SSLMODE=disable` is appropriate only while PostgreSQL is self-hosted on the private
Docker network. A future managed database should use TLS and its provider-supplied CA policy.

Create the server file from the committed template:

```bash
cp .env.production.example .env.production
chmod 600 .env.production
```

Generate secrets on the Droplet or another trusted machine. Do not paste their output into chat,
issue trackers, shell history, or repository files:

```bash
# Django secret (URL-safe, roughly 64 bytes of entropy)
openssl rand -base64 64 | tr -d '\n'

# PostgreSQL and Redis passwords
openssl rand -base64 36 | tr -d '\n'

# MinIO access key and secret key
openssl rand -hex 16
openssl rand -base64 48 | tr -d '\n'
```

Prefer generated values without `$`, `:`, `/`, `@`, or whitespace. Otherwise URL-encode database
and Redis credentials inside connection URLs, and escape `$` as `$$` for Docker Compose.

Rotation rules:

| Secret | Rotation procedure |
|---|---|
| Django secret key | Maintenance window; changing it invalidates signed values and active sessions. Replace the environment value and restart API/worker/Beat. |
| PostgreSQL password | Change the database role password, update both `POSTGRES_PASSWORD` and `DATABASE_URL`, then restart application processes and verify health. The image's initialization variable does not alter an existing database automatically. |
| Redis password | Update Redis configuration and both Redis URLs as one coordinated maintenance operation, restart the stack, then verify sessions, throttles, Celery, and outbox relay. |
| MinIO credentials | Create/activate replacement credentials first, update MinIO and application variables, restart, verify upload/download, then revoke the old credentials. Avoid a state where the application and MinIO expect different root credentials. |
| LLM API key | Create a replacement provider key, update `CLASSIFICATION_LLM_API_KEY`, restart workers, run the smoke evaluation, then revoke the old key. |
| GHCR deploy token | Create a read-only replacement, authenticate Docker, verify an image pull, then revoke the old token. |

Production email is configured for Resend's SMTP service through the settings in
`.env.production.example`. Repository configuration alone does not prove external delivery: the
Resend account, sender domain, DNS records, and a real delivery test are still required.

## 4. Phase 2 — Droplet and Network Provisioning

- [ ] Create an Ubuntu LTS Droplet in the recorded region.
- [ ] Add SSH keys; disable password authentication and root SSH login after bootstrap.
- [ ] Create a non-root deployment user with narrowly scoped sudo access.
- [ ] Install Docker Engine and the Compose plugin from Docker's supported repository.
- [ ] Enable unattended security updates.
- [ ] Enable a host firewall and a DigitalOcean Cloud Firewall.
- [ ] Allow SSH only from trusted source addresses where practical.
- [ ] Allow inbound ports `80` and `443` publicly.
- [ ] Do not allow public `5432`, `6379`, `9000`, `9001`, or `8080`.
- [ ] Configure DNS records for the API and storage hostnames.
- [ ] If using a DigitalOcean Volume, format, mount, and persist it in `/etc/fstab` using UUID.
- [ ] Confirm enough free disk remains for images, database growth, media, and temporary backups.
- [ ] Configure swap only if needed; it is not a substitute for adequate RAM.

Acceptance:

- [ ] SSH key login works for the deployment user.
- [ ] Rebooting the Droplet remounts persistent storage and starts Docker successfully.
- [ ] A remote port scan shows only the intended public services.

## 5. Phase 3 — Secrets and External Services

### 5.1 Application secrets

- [ ] Generate a high-entropy Django secret key.
- [ ] Generate unique PostgreSQL, Redis, and MinIO credentials.
- [ ] Do not reuse local-development credentials.
- [ ] Store registry credentials using Docker's credential mechanism or a read-only deploy token.
- [ ] Record a secret-rotation procedure without recording secret values.

### 5.2 Email

- [x] Select an SMTP or transactional email provider: Resend.
- [x] Add production email settings to Django configuration if not already supported.
- [ ] Configure a verified sender/domain and DNS records in Resend.
- [ ] Test registration verification, password reset, and status-change email delivery.
- [ ] Confirm failed email delivery does not lose the underlying outbox event.

#### Resend setup

1. Create a Resend account and add the domain that will appear after `@` in
   `DEFAULT_FROM_EMAIL` (for example, `mail.example.com`).
2. Add every DNS record Resend shows for that domain, including SPF and DKIM. Keep the records
   exactly as Resend provides them; do not add a second SPF TXT record for the same hostname.
3. Wait for Resend to mark the domain verified. Use a verified domain-based sender; Resend may
   restrict or rewrite unverified senders.
4. Create a restricted Resend API key for this deployment. Store it as `EMAIL_HOST_PASSWORD` in the
   server-only `.env.production` file. The SMTP username is literally `resend`.
5. Keep `EMAIL_HOST=smtp.resend.com`, `EMAIL_PORT=587`, `EMAIL_USE_TLS=true`, and
   `EMAIL_USE_SSL=false`. Port `465` is an alternative only when using SSL instead of STARTTLS.
6. Restart API and worker containers after changing the environment file:

   ```bash
   docker compose --env-file .env.production -f docker-compose.prod.yml up -d --force-recreate api worker beat
   ```

7. Exercise registration verification, password reset, and an issue status notification. Confirm the
   messages arrive and inspect Resend's delivery logs. For a failure test, temporarily use an invalid
   SMTP key and confirm the notification remains pending and Celery retries the task.

### 5.3 LLM

- [ ] Complete the configuration and evaluation procedure in
  `10-llm-deployment-evaluation.md`.
- [ ] Keep the deterministic fallback enabled.
- [ ] Run the labeled evaluation dataset and archive its JSON result.
- [ ] Test an invalid/unavailable LLM endpoint and verify reports still reach triage.

## 6. Phase 4 — Image Build, Migration, and Deployment

- [ ] Confirm CI builds and pushes an immutable SHA-tagged runtime image to GHCR.
- [ ] Add a Droplet deployment script; existing `scripts/deploy.ps1` targets Kubernetes and is not
  the Droplet deployment path.
- [ ] Pull the exact SHA-tagged image on the Droplet; never deploy `latest`.
- [ ] Run `python manage.py check --deploy --fail-level WARNING` with production settings.
- [ ] Back up the database before any non-trivial migration.
- [ ] Run migrations as a one-off pre-deploy container.
- [ ] Start/update API, worker, Beat, reverse proxy, database, Redis, and MinIO.
- [ ] Wait for dependency and application health checks.
- [ ] Record the deployed image SHA and migration state.

Required ordering:

```text
pull immutable image
  -> back up
  -> run migrations once
  -> update API
  -> update worker
  -> update the single Beat instance
  -> run smoke tests
```

## 7. Phase 5 — Storage and Persistence Validation

- [x] Add idempotent MinIO bucket creation and an authenticated read-back check that its anonymous
  policy is `none`; API/worker/Beat startup depends on this check succeeding.
- [ ] Confirm the MinIO bucket is private on the deployed host.
- [ ] Upload a JPEG, PNG, and WebP through the API.
- [ ] Confirm EXIF is removed and derivatives are generated by the worker.
- [ ] Confirm returned presigned URLs use `https://storage.<domain>`, not `storage:9000`.
- [ ] Confirm an expired signed URL is rejected.
- [ ] Restart and replace the API and worker containers; media must remain accessible.
- [ ] Restart and replace MinIO without deleting its volume; media must remain accessible.
- [ ] Restart and replace PostgreSQL without deleting its volume; data must remain accessible.
- [ ] Reboot the Droplet and repeat a read/write smoke test.

## 8. Phase 6 — Backups and Restore

- [ ] Store backups outside the Droplet; a backup on the same disk is not disaster recovery.
- [ ] Schedule daily PostgreSQL backups with retention.
- [ ] Schedule MinIO media backups or replication with retention.
- [ ] Encrypt backups in transit and at rest.
- [ ] Monitor backup age and job failure.
- [ ] Adapt the existing backup/restore rehearsal scripts for the production paths and credentials.
- [ ] Restore PostgreSQL into an isolated database.
- [ ] Restore media into an isolated bucket.
- [ ] Compare expected object/record counts and open representative restored images.
- [ ] Record recovery-point and recovery-time observations.

## 9. Phase 7 — Observability and Resource Protection

- [ ] Configure DigitalOcean host monitoring or an equivalent agent.
- [ ] Add external HTTPS uptime checks for health and a representative public API endpoint.
- [ ] Alert on CPU, memory, disk usage, inode usage, and Droplet unreachability.
- [ ] Alert before the PostgreSQL or MinIO volume is full.
- [ ] Monitor Redis health and Celery queue backlog.
- [ ] Monitor the outbox's oldest pending event age.
- [ ] Monitor HTTP error rate and latency.
- [ ] Monitor LLM fallback rate, circuit state, and budget exhaustion.
- [ ] Restrict Prometheus `/metrics` to private/internal access.
- [ ] Verify Docker log rotation and define retention.
- [ ] Write the first-response procedure and notification destination for each alert.

## 10. Phase 8 — Security and Privacy Review

- [ ] Run the complete automated test suite against production-like services.
- [ ] Run Django's production deploy check with no warnings.
- [ ] Confirm `DEBUG=False` and secure cookie flags.
- [ ] Confirm HTTP redirects to HTTPS without a proxy redirect loop.
- [ ] Start HSTS with a short value; increase only after HTTPS is stable.
- [ ] Verify `ALLOWED_HOSTS` and `CSRF_TRUSTED_ORIGINS` contain only intended origins.
- [ ] Confirm database, Redis, MinIO console, API container port, and metrics are not public.
- [ ] Verify RBAC/IDOR protections for citizen, authority, admin, and anonymous users.
- [ ] Verify rate limits through the public proxy address configuration.
- [ ] Verify uploaded content-type and size enforcement.
- [ ] Confirm report text sent to the LLM contains no extra identity/location fields.
- [ ] Confirm logs do not contain credentials, signed URLs, report text, or reset tokens.
- [ ] Rotate any credential exposed during rehearsal.

## 11. Phase 9 — End-to-End Production Rehearsal

Run this scenario on the real deployment before calling it ready:

1. [ ] Register and verify a citizen account.
2. [ ] Log in and upload an image.
3. [ ] Submit a geolocated report using the uploaded media.
4. [ ] Verify asynchronous media processing.
5. [ ] Verify LLM classification or the explicitly tested keyword fallback.
6. [ ] Verify report-to-issue clustering.
7. [ ] Log in as an authority and verify category-scoped queue access.
8. [ ] Assign the issue and perform valid status transitions.
9. [ ] Verify in-app and email notifications.
10. [ ] Verify status events and audit events.
11. [ ] Export CSV and GeoJSON data and retrieve the signed download.
12. [ ] Restart the API, worker, and Beat; repeat core reads and one write.
13. [ ] Simulate LLM unavailability and verify intake continues.
14. [ ] Perform a backup and isolated restore check.
15. [ ] Deploy a second SHA, then roll back to the previous SHA.

## 12. Final Launch Gate

- [ ] Every Phase 1–9 acceptance item is complete or has an explicitly accepted exception.
- [ ] Domain and TLS certificates are valid and auto-renewal is verified.
- [ ] Production secrets are unique and stored only on the server/provider secret stores.
- [ ] Latest database and media backups completed successfully.
- [ ] Restore and rollback rehearsals have documented evidence.
- [ ] Monitoring and alerts reach an actual operator.
- [ ] Known limitations are documented for the capstone demonstration.
- [ ] The deployed image SHA, deployment date, operator, and test result are recorded below.

| Release record | Value |
|---|---|
| Image SHA | TBD |
| Deployment date | TBD |
| Operator | TBD |
| Full test-suite result | TBD |
| LLM evaluation result | TBD |
| Backup/restore rehearsal | TBD |
| Rollback rehearsal | TBD |

## 13. Repository Work Queue

These are the concrete code/document artifacts still expected from this checklist. Complete them in
order unless a dependency requires otherwise.

1. [x] Production Docker Compose file with separate API, worker, and Beat services.
2. [x] Caddy/Nginx configuration for API, MinIO, TLS, SSE, and static files.
3. [x] `.env.production.example` with complete placeholder-only production variables.
4. [x] MinIO bucket initialization and private-bucket policy.
5. [x] Production email backend configuration and tests.
6. [ ] DigitalOcean Droplet bootstrap guide or script.
7. [ ] SHA-based Docker Compose deploy and rollback scripts.
8. [ ] Off-Droplet backup destination and scheduled backup procedure.
9. [ ] Monitoring and alert configuration.
10. [ ] Completed DigitalOcean sections in `09-operations.md` after the first rehearsal.

Do not mark an item complete merely because its configuration file exists. Completion requires its
acceptance check to pass in a clean or production-like environment.
