# Hospitable AI Toolkit

**Platform testing:** This community toolkit has been tested on Linux only. Windows / Docker Desktop installation, bind mounts, persistence and service setup have not been tested. Windows commands below are guidance, not a verified installation procedure.

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

**Guest auto-send defaults off.** Community containers can send supported drafts only after both account and property response modes are explicitly set to Automatic replies, with tested owner email enabled. Ordinary draft mode does not send. Legacy deployments retain `live_unavailable` behavior.
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
Automatic replies require explicit enablement at account and property level and a tested owner email connection.
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
old delivery ledgers can duplicate alerts or guest replies. Preserve both event history and the guest-send ledger; pausing sending before restoring an older backup is required.
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
monitoring. Live guest sending requires staging validation against actual webhook/thread schemas and channel permissions. Heating and self-service OAuth onboarding require separate implementation. Home Assistant integration is unavailable in community containers. Legacy HA configuration/database fields are retained for compatibility but cannot enable HA actions in a container. There is no deployment to AI implied
by publishing this repository.

### AI provider and model settings

In `/settings`, choose Anthropic (Claude), OpenAI, xAI (Grok), or Google (Gemini), enter the provider API key and load its available models. Choose a text model (or enter its exact API model ID), then **Test connection** and **Save AI settings**. The test makes a small billable request with no guest/property data. Model lists can include models unsuitable for text drafts; a successful connection test is required to establish compatibility for your choice. Chat service subscriptions are separate from API access/billing.

The saved provider/model applies account-wide to the next draft, with no restart. Existing Claude configuration remains the fallback until a selection is saved. An empty setup can start without a model key; configure AI before enabling drafts. Existing environment keys (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `XAI_API_KEY`, `GEMINI_API_KEY`) can be reused; leave the key input blank to retain an available key. Switching providers never reuses a different provider's key.

Browser-entered keys persist in `data/state/ai-settings.json` with mode 600, separate from the secret-free registry and the read-only installation credentials file. Treat data backups as credential backups. Keys are never returned by the settings API; the browser password field is cleared after saving/disconnecting. Enter keys over HTTPS or a trusted SSH tunnel. Changing AI providers sends future draft context to the chosen provider. API/model failures remain human-review events; automatic guest sending still requires explicit account and property enablement.

### Browser email setup

The Owner email alerts section of `/settings` accepts SMTP host, port, STARTTLS/TLS, username, app password/password, sender and comma-separated owner recipients. **Send test email** uses the form values and sends a plain setup message to that recipient; it does not save settings or enable alerts. **Save email settings** persists a private mode-600 `data/state/smtp-settings.json`. The password is never returned to the browser; leave it blank to retain it, or enter a replacement. Changing host or username requires re-entering the password. TLS certificate verification is always enabled. Some providers require an app password or an allowed sender; OAuth-only SMTP is not supported.

Saved browser settings override installer msmtp delivery configuration for this account and its properties without a restart. A saved email connection must pass the test before processing can be enabled in a community container. Enabling draft processing also enables required email alerts. Review-email alerts cannot be turned off while using processing; pause processing instead. Workers keep events pending until tested email and alert permission are ready. Changing SMTP settings invalidates the test until the new settings are successfully tested. Test responses mean the SMTP server accepted delivery, so check the inbox/spam folder. Generated msmtp files are mode 600 and deleted after delivery. Backups of data must be protected as credential backups.

### Account and channel display

Use **Refresh name and connected channels from Hospitable** to fetch the authenticated `/user` profile and properties with listings included. The account display name uses the returned company/name when available and can be edited; the internal account ID/data folder stays fixed. Property cards use Hospitable's internal property name/nickname before the public listing title. Listing-read permission is required for channels. API markup values are shown only when explicitly present; missing values are labelled unavailable, and numeric values without units are labelled as such. No markups or prices are changed. Real-account response schemas and scope access still require staging validation.

### Automatic guest replies

Community containers provide **Automatic replies / Draft only / Paused** at account and property level. Both account and property must be enabled and explicitly set to automatic; switching the account to draft prevents all property sends without changing their saved choices. Pause overrides sending and drafting. Existing config with `shadow: false` does not itself opt in: enablement timestamps are required. Saved/tested owner SMTP and enabled email alerts are required. Settings are audited and persist across restarts.

Only new guest messages with timezone-aware `created_at`, received after the most recent global/property enablement and less than ten minutes old, qualify. Before sending, the worker independently resolves account/property ownership, fetches the message thread and confirms the event is still the latest guest message. Unconfirmed or paginated conversation data, a newer host/guest response, old pending events, incidents/host decisions, missing facts and invalid model outputs require human review. Full API message shape and channel sending permissions must be verified in staging.

The worker stores `send_pending` before attempting a guest reply and an account-wide attempt ledger before the HTTP call. Sends have no automatic POST retry. Duplicate deliveries, crashes and timeouts never blindly resend; uncertain outcomes alert the owner to inspect the conversation. Rate guards allow at most two attempts per thread per minute and fifty per account per five minutes. API acceptance is recorded as sent; it is not proof of downstream channel delivery. A request already in flight cannot be recalled by changing a switch. AI-generated source grounding remains an imperfect control; validate drafts in shadow mode before opting in.

### Scheduled reservation messages

Open `/scheduled`, sign in with the toolkit admin token, choose a property nickname, load reservations, choose the reservation, enter the property's local date/time and literal message, then confirm **Schedule guest message**. The page lists this toolkit's pending, cancelled, sent and review messages; **Cancel scheduled message** works only while pending. The queue is persistent under `data/state/scheduled-messages.sqlite3` and runs as a supervised container process. It does not modify Hospitable's native messaging rules or their schedules.

Pending toolkit messages can also be edited in place: change the message and/or local send time, review the recipient and confirm saving. Edits check the saved revision and pending state; stale edits or messages already sending require a refresh. Delivery rechecks the due time and body before claiming an attempt, so an edit cannot silently send an earlier snapshot.

To list and edit Hospitable's own scheduled messages, use the optional **Connect Hospitable messages** section on `/scheduled`. In Hospitable, open Settings → Integrations → MCP → Fallback bearer tokens → Add fallback token, then verify/save that token on this page. It is separate from the toolkit admin token and Hospitable PAT. The connection verifies that both credentials belong to the same Hospitable account and stores the MCP token privately, mode 600, at `data/state/hospitable-mcp.json`. Protect data backups as credential backups. Removing the connection does not cancel messages in Hospitable. Availability depends on your Hospitable plan.

Select a booking and click **Load Hospitable messages**. These messages are labelled Hospitable; toolkit messages have their own list. The adapter uses the documented `get-reservation-scheduled-messages` and `update-scheduled-message` tools at the fixed `https://mcp.hospitable.com/mcp` endpoint. It verifies property ownership using both the PAT and MCP connection, refreshes the message before editing and rejects a changed, cancelled or sent message. Native edits update only that booking's message/content/time, never the messaging-rule template, never create a local delivery copy and never request immediate sending. Hospitable has no atomic revision parameter, so a change between the final read and upstream update remains possible. Uncertain edits are not automatically retried: reload messages to inspect the result first.

Hospitable takes native send times without a UTC offset; choose a time outside the repeated clock-change hour when editing its messages. Manually edited native messages may stop following automated adjustments to booking dates; check them after alterations. See [Hospitable MCP connection/tools](https://help.hospitable.com/en/articles/14424057-connect-an-ai-agent-to-hospitable-using-mcp) and [editing scheduled messages](https://help.hospitable.com/en/articles/9749323-editing-scheduled-message). Linux is the only tested platform; Windows remains untested. Native reads were verified against the connected service; native writes are covered by mocked tests and require deployment validation. No live native message was edited during development.

Scheduling is explicitly authorized manual sending, independent of the AI automatic-reply switches. Account/property Paused holds sends, and tested owner email plus enabled alerts are required. The reservation's property ownership is checked before saving and again at delivery, and delivery requires a confirmed/accepted reservation. A daylight-saving time that does not exist is rejected; a repeated local time requires first/second occurrence selection. Messages more than 15 minutes late require review. Sending is claimed durably before the HTTP call; cancellation cannot recall an in-flight request. Crashes, timeouts and unconfirmed outcomes require human review, never blind retry. No template short codes are expanded.

Optional MCP tools now include `list_reservations`, `scheduled_messages`, `schedule_message` and `cancel_scheduled_message`. Reading requires property `read` permission; creating/cancelling additionally requires a separate explicit property `schedule` permission in `mcp_clients.json`. Existing client grants are not upgraded automatically. `schedule_message` requires `confirm_send: true`; callers must obtain authorization for the concrete recipient, message and time before creating a schedule. MCP remains disabled unless configured, with provisioned bearer tokens rather than self-service OAuth login. No schedules are created by merely enabling MCP.

### Gmail owner alerts and multiple recipients

For Gmail, use SMTP host `smtp.gmail.com`, port `587`, STARTTLS, and the full sending Gmail address as the username. Sender email normally equals that address. Owner recipients are where human-review alerts should go; enter comma-separated addresses, for example `owner@example.com, cohost@example.com`. Up to 20 addresses are supported; test emails go to every entered recipient. Enabling review alerts may deliver pending alerts to the new recipient list.

Enable [Google 2-Step Verification](https://myaccount.google.com/signinoptions/two-step-verification), then create an [app password](https://myaccount.google.com/apppasswords) on the sending account. Paste the app password without Google's display spaces into the toolkit password box, not the normal Google login password. App-password availability depends on account/security/Workspace policies, and changing the Google account password revokes existing app passwords. See [Google's official instructions](https://support.google.com/accounts/answer/185833). These links and settings are also in the email setup section of the page.

### Finding the admin token on Linux, Windows or macOS

Once the toolkit container is running, this command is host-platform neutral:

```text
docker exec account-one-toolkit-1 python -c "from pathlib import Path; from hosting.container_runtime import read_credentials; print(read_credentials(Path('/run/toolkit-secrets/credentials.env'))['TOOLKIT_ADMIN_SECRET'])"
```

Use your actual container name (`docker ps --format "{{.Names}}"`). Linux installations may require `sudo`. Alternatively, open your instance's `secrets/credentials.env` on the host and copy just the admin-secret value. `/srv/...` is a Linux example, not a required Windows folder. The toolkit container is a Linux container even on Docker Desktop. Full Windows toolkit installation remains unvalidated; if a Windows bind mount reports permissive Unix modes and fails credential checks, use a WSL2 Linux-filesystem instance folder with the prescribed permissions rather than weakening the checks.

### Caddy HTTPS setup on Linux or Windows

Caddy runs on the host in these instructions, proxying the toolkit's loopback Docker port. Keep the toolkit port mapping `127.0.0.1:8790:8790`; do not expose port 8790 to the internet. Caddy in another container needs a shared Docker network and service-name upstream instead: its `127.0.0.1` is not the host. Use one Caddy installation for an existing site; do not start a second server competing for ports 80/443.

Choose a hostname you control and point its DNS to the Caddy host. For ordinary public automatic certificates, TCP ports 80 and 443 must reach Caddy, with firewall/router forwarding as needed. A private-only hostname needs a suitable DNS challenge setup or Caddy's internal CA trusted on each client instead. The example `examples/Caddyfile.owner.example` allows public authenticated webhook/MCP routes but limits owner pages/APIs to private-source IP addresses. Use a trusted LAN/VPN for owner access. No access log is enabled, to avoid storing webhook URL tokens. If an upstream proxy hides real client IPs, do not rely on the example's source-IP check without configuring trusted proxy handling.

**Linux (Debian/Ubuntu).** Install Caddy using the [official package instructions](https://caddyserver.com/docs/install#debian-ubuntu-raspbian); that package provisions its systemd service. Edit `/etc/caddy/Caddyfile`, adding the example site after replacing `toolkit.example.com` and the internal account ID. Preserve existing sites such as Windsor. Validate before reloading:

```bash
sudo caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
sudo systemctl enable --now caddy
sudo systemctl reload caddy
sudo systemctl status caddy --no-pager
```

**Windows (native Caddy).** Download the appropriate binary from [Caddy's official download page](https://caddyserver.com/download) into `C:\caddy\caddy.exe`, create `C:\caddy\Caddyfile` from the example and replace its hostname/account ID. In PowerShell:

```powershell
C:\caddy\caddy.exe validate --config C:\caddy\Caddyfile --adapter caddyfile
C:\caddy\caddy.exe run --config C:\caddy\Caddyfile --adapter caddyfile
```

This foreground test runs until stopped. For automatic startup, after stopping that foreground process, open elevated PowerShell and register a service:

```powershell
sc.exe create caddy start= auto binPath= "C:\caddy\caddy.exe run --config C:\caddy\Caddyfile --adapter caddyfile"
sc.exe start caddy
```

Allow the required ports through Windows Firewall for your deployment. The service account needs access to its configuration and certificate storage; protect these files. Reload changed configuration with `C:\caddy\caddy.exe reload --config C:\caddy\Caddyfile --adapter caddyfile`. See [Caddy's service documentation](https://caddyserver.com/docs/running#windows-service) for sc.exe/WinSW alternatives and account/storage considerations. The Windows service instructions follow Caddy documentation but have not been tested in this project's Linux CI.

After setup, open `https://YOUR_HOSTNAME/settings` or `/scheduled` from the allowed LAN/VPN. Verify that access from an untrusted public IP is denied for `/admin/*`; webhook/MCP authentication remains enforced by the application. Caddy does not enable MCP or configure Hospitable outbound webhooks by itself.

### Provisioning MCP access

MCP access is optional. It uses a separate bearer token for each client, never the admin token or Hospitable PAT. The server supports clients that can supply an Authorization header; it does not provide a browser OAuth sign-in flow. A client that requires OAuth cannot connect directly yet. This applies independently of your choice of AI provider for guest drafts.

1. In `data/config/account.json`, add this top-level object alongside `account`, replacing the hostname and internal account ID:

```json
"mcp": {
  "enabled": true,
  "resource_url": "https://toolkit.example.com/toolkit/account-one/mcp",
  "issuer_url": "https://toolkit.example.com"
}
```

The issuer URL is metadata only; it does not create an authorization server.

2. Generate a random token locally, for example `python -c "import secrets; print(secrets.token_urlsafe(32))"`. Add it as `OWNER_MCP_TOKEN=...` to the instance's `secrets/credentials.env`, keeping the file private (mode 600 on Linux/WSL). Give different clients different tokens.
3. Copy `examples/mcp_clients.json` into `data/config/mcp_clients.json`; replace account/property IDs with this instance's configured IDs. Start with property `read` and `preview` permissions. Add property `schedule` only when that client should create/cancel guest messages. `settings` additionally permits control changes. Grants are explicit per property; an account-level grant does not grant all properties.
4. Configure Caddy HTTPS for the resource URL using the instructions above, then recreate the toolkit container to load the MCP credentials/configuration.
5. In a client that supports bearer-authenticated Streamable HTTP, use the exact `resource_url` and header `Authorization: Bearer YOUR_CLIENT_TOKEN`. First call `list_access`, then `get_settings` or `search` for a granted property. Never paste tokens into guest messages or source documents. Start with a read-only grant when checking a new client.

The automated transport tests verify unauthenticated rejection, tool discovery and property permissions; a successful connection from your actual client and hostname must still be checked after deployment. Token rotation requires a container restart; changing grants in `mcp_clients.json` is read on each tool call. Remove a client's access by setting its `enabled` to `false`.
