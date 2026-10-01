# Hospitable AI Toolkit

One **Hospitable account per container**, with multiple selected properties.
Another account uses the same image in another container, on another host port,
with its own configuration, credentials and mounted data folder.

This is a standalone draft/review foundation. It contains no Windsor knowledge,
Windsor scheduler or personal deployment configuration. Martin's original service
continues independently in [windsor-rag](https://github.com/flinty1970/windsor-rag).

## What works

- Durable authenticated message webhook inbox and API-verified property routing.
- A separate draft worker, knowledge index, event ledger and alert outbox per property.
- Persistent account/property enabled and shadow controls.
- Optional scoped MCP on the same published port; explicit read/preview/settings grants.
- Independent optional HA review alerts and owner-specific msmtp email, with property overrides.
- Manual or optional daily ingestion inside the container, with atomic index generation changes.
- Read-only application/configuration/secret mounts, non-root processes and loopback host ports.

**Guest auto-send is unavailable.** Turning shadow off pauses processing with
`live_unavailable`; it does not start sending. Ordinary drafts are for review.
Heating has an opt-in flag but no controller. No direct PriceLabs integration exists.

## Storage

Each account's host folder contains:

```text
account-one/
  instance.env                 # Compose settings, not API credentials
  secrets/
    credentials.env            # owner API/model/webhook/admin/MCP/HA credentials
    msmtprc                    # owner's SMTP credentials/configuration
  data/
    config/
      account.json             # exactly one account and its selected properties
      mcp_clients.json         # optional explicit property permissions
    state/
      instance.json            # account identity and schema guard
      runtime-registry.json
      inbox.sqlite3
      controls.sqlite3
      index-schedule.json
    model-cache/               # shared embedding downloads for this account
    properties/
      property-uuid/
        source-documents/      # private archive, never automatically indexed
        document-review/       # converted PDF Markdown awaiting approval
        docs/                  # curated guest-safe Markdown
        index/
          current.json         # completed active generation
          <generation>/        # Chroma database
        state/
          events.sqlite3       # deduplication, drafts and durable alerts
          worker-token
          worker-registry.json
          index.lock
        logs/
          ingestion.log
```

All directories shown under `data/` are bind-mounted. Configuration and secrets
are mounted read-only. Replacing a container preserves data. The container exits
if the mounted state belongs to another account. Different properties cannot
use overlapping folders. Processes in the same account share a trust boundary;
the property workers are not hostile-tenant security sandboxes.

## Initial setup on Linux

Use Docker Engine with the Compose plugin. This runs on a Linux server, not an
OpenWrt router. CPU PyTorch/Chroma make the image large; start with the provided
4 GB memory limit and measure real workloads before adding properties. Each
property has a process; accounts are isolated by their containers and mounts.

```bash
git clone https://github.com/flinty1970/hospitable-ai-toolkit.git
cd hospitable-ai-toolkit
mkdir -p /srv/hospitable-ai/account-one/data/config /srv/hospitable-ai/account-one/secrets
chmod 700 /srv/hospitable-ai/account-one/secrets
cp examples/account.setup.json /srv/hospitable-ai/account-one/data/config/account.json
cp examples/mcp_clients.json /srv/hospitable-ai/account-one/data/config/mcp_clients.json
cp examples/credentials.env.example /srv/hospitable-ai/account-one/secrets/credentials.env
cp examples/msmtprc.example /srv/hospitable-ai/account-one/secrets/msmtprc
cp examples/instance.env.example /srv/hospitable-ai/account-one/instance.env
chmod 600 /srv/hospitable-ai/account-one/secrets/*
```

Create `/srv` folders with sudo if necessary, then assign them to the intended
service user. Set APP_UID/APP_GID to that user's numeric IDs and match the mounted
files' ownership. Do not make credentials world-readable to solve permissions.
Edit the copied files **before startup**:

1. The setup example starts with no imported properties. After startup, open
   `/settings`, sign in, and discover/select properties using this account's PAT.
2. Alternatively, use `examples/account.json` to configure property UUIDs manually.
3. Put this account's PAT/model keys and distinct randomly generated tokens in
   `secrets/credentials.env`; this file is parsed as literal assignments, not shell code.
4. Set this owner's SMTP server, user, password and sender in `msmtprc`, and real
   sender/recipients in `account.json`. Email defaults on in the example, HA off.
5. Add only credentials referenced by this instance or the supported AI provider keys. Do not copy Windsor's complete
   `/etc/*.env` files; they contain unrelated credentials and settings.

No setup command alters Windsor, Caddy or Hospitable's configured webhook.

```bash
docker compose --env-file /srv/hospitable-ai/account-one/instance.env -p account-one up -d --build
docker compose --env-file /srv/hospitable-ai/account-one/instance.env -p account-one ps
curl --fail http://127.0.0.1:8790/ready
```

`/health` checks the receiver; `/ready` additionally checks its property workers.
Neither guarantees provider API/model/SMTP availability. Failed children cause
the container to exit; Docker restarts it. An unhealthy healthcheck alone does
not make Docker automatically restart a still-running container.

## Browser account setup and controls

Open `/settings` on the same toolkit port and sign in with the admin token.
**Refresh Hospitable properties** lists names, IDs and timezones from this
account's PAT. Select properties and import them; membership is verified again
server-side against the account API. No guest documents or reservations are
imported by discovery. Properties absent from the account API cannot be imported.

Hospitable may return a fixed UTC offset instead of an IANA timezone. Existing properties keep their configured timezone; new imports with an offset require a timezone choice such as `Europe/London` so daylight-saving changes work correctly.

Selections persist in `data/state/property-selection.json`, separate from the
read-only configuration. Imports add to the configured properties and never
delete existing folders or knowledge. Click the restart button to apply them;
the supervisor exits gracefully and Compose's `unless-stopped` policy restarts
this account container. New properties start **paused**. Then select **Draft only**
to enable processing after loading approved property knowledge. Bare `docker run`
without a restart policy requires an operator restart instead.

Account/property **Draft only / Paused** controls persist immediately in the
existing controls database and are audited. Account pause overrides property
settings. Resuming wakes pending inbox events; old review events are not replayed.
Automatic replies remain unavailable and cannot be enabled by the menu/API.
Email alert switches use existing owner configuration; enabling
an unconfigured alert channel is rejected. Changing
timezones/indexing schedules, guest-draft review, and account OAuth login
still require separate configuration or future UI work.

Use `/documents` for each property's PDF review and index update. Both interfaces
use memory-only bearer tokens and the same trusted LAN/VPN or SSH-tunnel access.
Do not publicly expose HTTP admin routes; use HTTPS and access restrictions for
a public owner portal. The Caddy example keeps admin routes private.

## Property documents and indexing

Put PDFs/manuals under that property's `source-documents/`. Manual and daily
ingestion extract new text-based PDFs into `document-review/` as Markdown, with
page headings and source fingerprints. Layout-aware extraction keeps positioned
words together while retaining line and column structure. Converter upgrades
create a new review version; previous edits and approved text are preserved.
Run `convert-pdfs` again after upgrading, review the new `-v2.md` version,
reapply any host corrections, and explicitly replace the approved version. They never enter the guest index until
you review/edit them and explicitly approve them into `docs/`. Installer
instructions, access codes and private information must be removed during review.
The browser document interface is at `/documents` on the toolkit port. Sign in
with `TOOLKIT_ADMIN_SECRET`, choose the property, upload a PDF, review/edit its
Markdown, save, confirm it is guest-safe, approve, then click **Update property
knowledge**. Tokens stay in page memory, never URLs or browser storage. A reload
or disconnect requires signing in again. Review text is shown as plain text;
PDF/Markdown content is never executed as HTML.

For a remote trial, forward the loopback port from your own computer:

```bash
ssh -N -L 8790:127.0.0.1:8790 mflint@YOUR_AI_SERVER
```

Then open `http://127.0.0.1:8790/documents` on that computer. Obtain the admin token
locally from the instance credentials file; never share it in chat. The existing
Caddy example deliberately does not expose admin routes. Keep it that way for
this trial. A public owner portal needs separate HTTPS/access configuration.

Uploads are limited to 50 MB and selected properties. Existing source filenames
are not overwritten; rename an updated PDF before uploading. Approval only copies
saved text into curated knowledge. The separate index update publishes atomically;
a failed update keeps the previous index. Requests show completion/error status;
there is no durable background-job UI, so keep the page open during conversion
and indexing. CLI commands below remain available.

```bash
docker compose --env-file /srv/hospitable-ai/account-one/instance.env -p account-one exec toolkit python -m hosting.cli convert-pdfs <property-uuid>
# Review/edit the generated .md in the host property's document-review/ folder.
docker compose --env-file /srv/hospitable-ai/account-one/instance.env -p account-one exec toolkit python -m hosting.cli approve-pdf <property-uuid> <converted-file.md>
```

Then run reindex below. Updated PDFs create new review versions; approval rejects
a stale source fingerprint. Existing approved text requires `--replace`, so
conversion cannot overwrite host edits. A conversion-report.json records errors
and pages with no extracted text. Image-only scans require OCR (not yet included);
mixed PDFs with missing pages require inspection and explicit `--allow-incomplete`
to approve. Tables, drawings and complex layouts require manual checking; this
is text extraction, not a promise of visually faithful conversion.

```bash
docker compose --env-file /srv/hospitable-ai/account-one/instance.env -p account-one exec toolkit python -m hosting.cli reindex <property-uuid>
docker compose --env-file /srv/hospitable-ai/account-one/instance.env -p account-one exec toolkit python -m hosting.cli review <property-uuid>
docker compose --env-file /srv/hospitable-ai/account-one/instance.env -p account-one exec toolkit python -m hosting.cli inbox
```

The first index build downloads `all-MiniLM-L6-v2` into the mounted model cache;
internet access is needed. Later builds reuse it. There is no shared account
knowledge fallback. Ingestion builds a new generation, then atomically replaces
`index/current.json`. Failure or empty documents preserve the previous index.
Old/incomplete generations remain for inspection; stop the container and back up
before pruning them. Removing all knowledge requires an explicit operator action:
an empty rebuild intentionally does not silently erase the last working index.

Optional account `indexing.enabled: true` starts a container-local scheduler.
`daily_at` and an explicit IANA `timezone` determine its run; each property can
override settings or opt out. It catches up a missed run after restart, persists
successful dates, runs properties sequentially and retries failures after an hour.
It never installs host cron jobs or uses Windsor's `reindex.sh`/PDF builder.

## Webhook and MCP

Public webhook URL: `https://your-host/webhook/hospitable/account-one?token=<secret>`.
Caddy proxies only that account's route to `127.0.0.1:8790`; do not publish worker
ports or the admin endpoint. See `examples/Caddyfile.fragment`. Preserve the
existing Windsor route. Do not log bearer tokens in URL query strings.

Only `message.created` is handled. The receiver returns 200 after durable inbox
storage. The account PAT independently verifies the reservation/inquiry property;
unresolved/unmanaged/conflicting events go to owner review, never another index.
Retries deduplicate by account/message ID. Received events survive restarts;
missed-provider-event reconciliation and full conversation-order handling remain
outstanding. Real payload/API schemas require staging validation.

Authentication currently uses independently configured bearer secrets, **not
payload signatures**. Hospitable signing details still require confirmation.
Add validated source restrictions at Caddy if desired; they do not replace signatures.

MCP defaults off. Enable `mcp.enabled`, set the public HTTPS `resource_url` (for
example `/toolkit/account-one/mcp`) and configured issuer metadata, add distinct
client tokens to credentials, and configure explicit grants in `mcp_clients.json`.
Proxy the exact MCP and protected-resource metadata paths in Caddy. Paths are
preserved; no URI stripping is required. Two accounts need distinct public MCP paths.
Clients use operator-issued Bearer tokens. OAuth login/token issuance is not
implemented; issuer metadata is not an OAuth server. Clients requiring browser
OAuth onboarding need that addition before they can connect. Windsor's existing
owner OAuth/MCP is unrelated and stays running.

## Optional integrations and switches

Account defaults and property overrides select each owner's endpoints and mail
settings. Parent and property enabled permissions intersect; parent or property
shadow mode keeps processing in shadow. Scoped MCP setting changes are audited.

- HA's master `home_assistant.enabled` gates all hosted HA actions. Review alerts
  additionally require `notifications.ha.enabled` and a `webhook_url_env` credential.
  No VPN/HA is needed when disabled. Configured HA may use a private reachable URL.
- Heating is separately gated, but `heating_available` is false in this version.
  These controls cannot disable existing external HA or router automations.
- Email is independent of HA. Account/property `notifications.email` specify
  `enabled`, absolute `msmtp_config_file`, `msmtp_account`, `sender` and `recipients`.
  Use mounted owner-specific files; there is no personal mailbox fallback.
- Human-review results queue persistent alerts; normal shadow drafts do not alert.
  Channels retry independently. Disabled alerts remain pending and may deliver
  when re-enabled. Ambiguous HTTP/SMTP acceptance can duplicate an alert.

## Second account

Create `/srv/hospitable-ai/account-two` with its own files and data. Set a different
account ID, PAT, tokens and selected properties, plus `HOST_PORT=8791` in its
instance settings. Start with `-p account-two`. Reuse the same image; internal
ports remain the same because each container has its own network namespace.
Add only its distinct webhook/MCP routes to Caddy. Do not reuse the first
account's data directory or credentials. Bind ports to loopback as supplied.

## Upgrade, backup and rollback

Build/tag an immutable image per release, record its image ID and retain the
previous image. In production set `TOOLKIT_IMAGE` to a retained version and run
`up -d --no-build`; do not rely on rebuilding an old tag with changing dependencies.
Stop only the relevant account container before a complete consistent backup of
its `data/`, `secrets/` and `instance.env`. Protect the backup as guest/credential
data and verify restoration into a separate directory. Then restart the account.

Rollback uses the previous image against compatible current state. Schema version
1 is recorded; incompatible versions refuse the directory. Database migrations
must remain backward compatible or require a planned data restore. Restoring an
old delivery ledger can duplicate alerts; there are no automatic guest sends here.
Deleting a container does not delete bind mounts; do not delete the host directory.

## Validation and remaining work

`python -m pip install -r requirements-runtime.txt` then
`python -m unittest discover -s tests -v` runs isolation/routing/ACL/controls/alerts,
container configuration, shared-port MCP and atomic-index tests with external
provider/model/SMTP calls mocked. The GitHub workflow builds the real CPU image
and smoke-tests two simultaneous containers, authentication, restart persistence
and a real embedding/index/query against dummy curated property facts.
It does not validate real guest generation or a user's SMTP/HA deployment.

Before real user onboarding: stage real payloads and knowledge, confirm signing,
add missed-event reconciliation, guest-draft review UI, retention/deletion policy and operational
monitoring. Live guest sending, schedules, heating and self-service OAuth onboarding
require separate implementation and validation. Home Assistant integration is unavailable in community containers. Legacy HA configuration/database fields are retained for compatibility but cannot enable HA actions in a container. There is no deployment to AI implied
by publishing this repository.

### AI provider and model settings

In `/settings`, choose Anthropic (Claude), OpenAI, xAI (Grok), or Google (Gemini), enter the provider API key and load its available models. Choose a text model (or enter its exact API model ID), then **Test connection** and **Save AI settings**. The test makes a small billable request with no guest/property data. Model lists can include models unsuitable for text drafts; a successful connection test is required to establish compatibility for your choice. Chat service subscriptions are separate from API access/billing.

The saved provider/model applies account-wide to the next draft, with no restart. Existing Claude configuration remains the fallback until a selection is saved. An empty setup can start without a model key; configure AI before enabling drafts. Existing environment keys (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `XAI_API_KEY`, `GEMINI_API_KEY`) can be reused; leave the key input blank to retain an available key. Switching providers never reuses a different provider's key.

Browser-entered keys persist in `data/state/ai-settings.json` with mode 600, separate from the secret-free registry and the read-only installation credentials file. Treat data backups as credential backups. Keys are never returned by the settings API; the browser password field is cleared after saving/disconnecting. Enter keys over HTTPS or a trusted SSH tunnel. Changing AI providers sends future draft context to the chosen provider. API/model failures remain human-review events; automatic guest sending is still unavailable.

### Browser email setup

The Owner email alerts section of `/settings` accepts SMTP host, port, STARTTLS/TLS, username, app password/password, sender and one owner recipient. **Send test email** uses the form values and sends a plain setup message to that recipient; it does not save settings or enable alerts. **Save email settings** persists a private mode-600 `data/state/smtp-settings.json`. The password is never returned to the browser; leave it blank to retain it, or enter a replacement. Changing host or username requires re-entering the password. TLS certificate verification is always enabled. Some providers require an app password or an allowed sender; OAuth-only SMTP is not supported.

Saved browser settings override installer msmtp delivery configuration for this account and its properties without a restart. A saved email connection must pass the test before processing can be enabled in a community container. Enabling draft processing also enables required email alerts. Review-email alerts cannot be turned off while using processing; pause processing instead. Workers keep events pending until tested email and alert permission are ready. Changing SMTP settings invalidates the test until the new settings are successfully tested. Test responses mean the SMTP server accepted delivery, so check the inbox/spam folder. Generated msmtp files are mode 600 and deleted after delivery. Backups of data must be protected as credential backups.

### Account and channel display

Use **Refresh name and connected channels from Hospitable** to fetch properties with `user,listings` included. The account display name uses the returned user name when available and can be edited; the internal account ID/data folder stays fixed. Property cards use Hospitable's internal property name/nickname before the public listing title. Listing-read permission is required for channels. API markup values are shown only when explicitly present; missing values are labelled unavailable, and numeric values without units are labelled as such. No markups or prices are changed. Real-account response schemas and scope access still require staging validation.
