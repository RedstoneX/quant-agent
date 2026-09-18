# Credential Delivery — Accepted Architecture (OneCLI)

Status: **accepted, commissioned, and verified — 2026-08-12.** This is the durable architecture reference for how QAMC obtains real credentials. See `docs/STATE.md` for current authorization and why the custom proxy from commit `2207b0b74287101ea65ce79782081e51a27420ba` is rejected architecture and must not be revived.

## OneCLI Credential Gateway

- OneCLI is the credential delivery layer for QAMC. It runs under Docker on the VPS, administered by `ubuntu` (see `ops/onecli/README.md`); `qamc` and `dev` are never added to the `docker` group and cannot reach the Docker socket.
- Secrets are stored only in OneCLI, never duplicated in QAMC's own configuration. QAMC's `.env` and `config/settings.yaml` hold placeholder values only.
- Agent access uses explicit secret grants (`secretMode: "selective"` on the Default Agent). Creating a secret does not automatically make it available to an agent — granting it is a separate step.
- The gateway (port `10255`) matches outbound requests to a secret by destination host/path and injects the real credential (header or query parameter) before forwarding; the dashboard (port `10254`) manages secrets/agents/grants. Both bind `127.0.0.1` only.
- QAMC's consuming code needs no awareness of any of this: `src/agents/base.py`'s OpenRouter branch, `src/execution/broker.py`'s `AlpacaBroker`, and `src/data/macro.py`'s `MacroDataProvider` all construct their SDK clients (`openai`/`httpx`, `alpaca-py`/`requests`, `fredapi`/`urllib`) with no custom session/opener, so each already inherits its library's default environment-driven proxy/CA trust. Zero `src/` or `config/` changes were needed to integrate any of the four credentials.
- Client-side wiring is three environment variables in `/home/qamc/quant-agent/.env` (operator-only — `dev` cannot write into `/home/qamc`): `HTTPS_PROXY` (`http://x:<agent-token>@127.0.0.1:10255` — note `127.0.0.1`, not the `host.docker.internal` OneCLI's own `GET /api/container-config` returns by default, which only resolves inside a Docker container and not for QAMC's bare `qamc`-account processes), `SSL_CERT_FILE`, and `REQUESTS_CA_BUNDLE` (both pointed at OneCLI's gateway CA cert — `requests`, Alpaca's transport, does not honor `SSL_CERT_FILE` alone).

## Two Alpaca accounts share the same OneCLI setup — verified 2026-08-28

In plain terms: there is not one Alpaca paper account behind OneCLI, there are
two, and the only thing that tells them apart is which agent is asking — not
which key file is on disk. Looking at the filesystem alone (one `.env`, one set
of placeholder values, one pair of header names) makes it look like a single
account is configured; that appearance is wrong.

The production desk and a separate rehearsal harness both authenticate with the
identical header pair (`APCA-API-KEY-ID` / `APCA-API-SECRET-KEY`), and
production's secret matches the wildcard host pattern `*.alpaca.markets`, which
also covers the paper-trading host — so the request headers and the host alone
cannot distinguish which account is being reached. What actually decides it is
the **agent access token** carried in the outbound proxy URL
(`HTTPS_PROXY=http://x:<agent-token>@127.0.0.1:10255`): OneCLI maps that token
to an agent identity, and each agent identity has its own credential grants in
the `agent_secrets` table.

- `Default Agent` (`identifier=default`) → production, Alpaca paper account
  `PA3DFXH9FF5V` (the same account already named elsewhere in this repo's
  incident history).
- `Rehearsal Harness` (`identifier=rehearsal`) → a separate paper account,
  `PA30V8QHEW1C`, funded at $10,000, with its secrets pinned specifically to
  `paper-api.alpaca.markets`.

Verified directly: the same URL called through each of the two tokens returns
data for a different account. When OneCLI cannot resolve a token to a grant
unambiguously, it fails closed (`access_restricted`) rather than guessing —
also confirmed empirically, not assumed from the gateway's documentation.

Two gaps this leaves open, low urgency only because the rehearsal harness is
currently offline: the rehearsal agent can still reach Telegram using the
production bot token, so a live rehearsal run would alert the owner's real
phone with fake trades unless a separate bot or a forced prefix is added; and
an LLM call made from a rehearsal run spends real OpenRouter money on the same
account as production, but is tracked in the rehearsal's own separate
cost-circuit database, so production's own cost accounting would under-count
the true bill if both ran at once. Neither is a reason to avoid rehearsal
today; both are conditions to close before rehearsal and production ever run
concurrently.

## Configured Providers

**OpenRouter** — LLM provider credential for all 9 agents. Header-based: `Authorization: Bearer {value}`, host `openrouter.ai`.

**FRED** — economic data. URL query-parameter injection, not a header: parameter `api_key`, host `api.stlouisfed.org`.

**Alpaca** — paper trading credentials. Two headers, both required together: `APCA-API-KEY-ID` and `APCA-API-SECRET-KEY`. Values must be injected raw (`{value}`) — **not** `Bearer {value}`; Alpaca's headers are not OAuth-style. Host coverage must be `*.alpaca.markets` (a leading-subdomain wildcard), since QAMC calls both `paper-api.alpaca.markets` (trading) and `data.alpaca.markets` (market/historical data) as distinct hosts. Path pattern must stay blank/unset — Alpaca's API spans multiple paths (`/v2/account`, `/v2/orders`, `/v2/stocks/...`, etc.), and a path pattern narrower than that will block injection on paths it doesn't happen to match.

## Validation

All four credentials were verified working end-to-end through the OneCLI gateway, using obviously-fake placeholder credentials sent by the client and comparing gateway-routed vs. direct requests against endpoints that actually validate the credential (not endpoints that respond regardless of auth). Every response body was discarded; no real credential value was ever read, logged, or held by `dev`.

- OpenRouter connectivity verified through the OneCLI gateway.
- FRED connectivity verified through the OneCLI gateway.
- Alpaca connectivity (both the trading host and the market-data host) verified through the OneCLI gateway, after the operator resolved credential-routing issues by correcting grants, host scope, and header value formatting in OneCLI directly.

Remaining step: apply the `.env` wiring above to `/home/qamc/quant-agent/.env` and confirm `quant-agent-api.service`'s `/health` reports `broker_reachable: true`. Trading timers remain disabled independent of this.

## OneCLI install requirements (for reference)

`github.com/onecli/onecli`'s documented Quick Start install script unconditionally requires Docker + a running daemon + Docker Compose, and deploys PostgreSQL via Docker Compose (not an embedded database). This is why commissioning required a privileged `ubuntu` action rather than something `dev` could complete alone — see `ops/onecli/README.md`.

## HTTP-stack proxy/CA behavior (why the three env vars above are correct and sufficient)

- **`httpx`** (OpenRouter, via the `openai` SDK): honors `HTTPS_PROXY`; CA trust via `SSL_CERT_FILE`.
- **`requests`** (Alpaca, via `alpaca-py`): honors `HTTPS_PROXY`; CA trust via `REQUESTS_CA_BUNDLE` only — does **not** honor `SSL_CERT_FILE`.
- **`urllib`** (FRED, via `fredapi`): honors `HTTPS_PROXY`/`https_proxy`; CA trust via `SSL_CERT_FILE` (OpenSSL-level).

---

# The websocket exception — broker credentials delivered to the process (2026-09-17)

**This section narrows the rule above, and says so openly.** The statement
"secrets are stored only in OneCLI, never duplicated in QAMC's own
configuration" remains true for OpenRouter and FRED, and remains the default
for everything else. It is no longer true of the Alpaca pair. The owner
accepted that change on 2026-09-17 for the reason below. **The rejected custom
credential proxy is still rejected and is not revived by any of this** — nothing
here adds a proxy, a listener, or any code between QAMC and a provider.

## Why the gateway cannot cover this one case

The gateway substitutes a real credential into an outbound **REST** request by
rewriting a header. Two independent facts put Alpaca's `trade_updates`
websocket out of its reach:

- **Alpaca authenticates the socket with an in-band message.** After the socket
  is open, the client sends an authentication *payload*. It is not an HTTP
  handshake header, so a header-injecting gateway has nothing to rewrite.
- **The installed `alpaca-py` stream is built on `websockets.legacy`, which has
  no proxy support at all.** Proxy support arrived in `websockets` 15.0, for the
  asyncio and sync clients only — not the legacy implementation the SDK uses. So
  the socket would not traverse the gateway even if a gateway could help.

The consequence is the thing that cost this project most: **order placement
works on a placeholder credential, and the websocket cannot ever authenticate
on one.** The two failure modes look nothing alike from the outside, which is
why a placeholder survived eight days while five attempts tuned the timing of a
handshake that was never going to succeed.

Because authentication is in-band, the process itself must hold the key. There
is no arrangement in which the socket authenticates and the desk does not hold
the credential. The decision was therefore to deliver it in the least exposed
way available rather than to keep withholding it.

## How it is delivered

systemd hands each credential to the unit as a **file**, not an environment
variable: a read-only file on a tmpfs, in a per-unit directory, with the process
told where via `CREDENTIALS_DIRECTORY`. `src/credentials.py` prefers those files
over the environment; `.env` keeps its placeholders and is never edited.
`ApiKeysConfig` ends up populated identically either way, so nothing downstream
of `load_config` knows which path was used.

What this buys over the status quo, stated without inflation:

- The value is **not in the process environment**, so it is absent from
  `/proc/<pid>/environ`, is not inherited by every child process, and does not
  appear in a dump that echoes the environment.
- It lives in **one root-free file with its own permissions**, separate from the
  `.env` that holds every other secret.
- It is **not in the repository checkout**, so no deploy, `git checkout` or
  branch switch can move, overwrite or expose it.

## What it does NOT buy — encryption at rest is NOT achieved

**`LoadCredentialEncrypted=` cannot be used on this box, and the units use plain
`LoadCredential=` instead.** This was verified directly, not assumed:

- QAMC's units are `systemd --user` units under the lingering `qamc` account.
- Decrypting a host-key credential requires reading
  `/var/lib/systemd/credential.secret`, which is mode `0400`, owner `root`.
- An unprivileged user manager therefore fails with `Failed to determine local
  credential key: Permission denied`, and the unit dies at `status=243/CREDENTIALS`
  before the application runs. Reproduced on this host with a dummy value.
- There is **no TPM device on this VPS** (`/dev/tpm*` does not exist), so
  `--with-key=tpm2` is not an option either.
- `systemd-creds` at the installed version (systemd 255) has **no `--user`
  flag**; user-scoped credential encryption is a later addition and is tracked
  upstream as an open limitation of user services.

So the credential is protected by **file permissions, not cryptography**. That
is a smaller claim than "encrypted at rest, bound to the host", and it is the
accurate one. Encryption at rest becomes available only if the desk's units are
moved from the user manager to the system manager — a separate decision with its
own blast radius, not taken here. **The application code is identical either
way:** both directives populate the same directory, so switching is a one-word
change in the units and no change at all in `src/`.

## Why a missing credential file is loud rather than fatal

`LoadCredential=` against a missing file is fatal to the unit — it never reaches
the application. Installing these units before the credential files existed
would therefore have stopped the entire desk. Each unit declares a
`SetCredential=` stand-in *before* the `LoadCredential=` line: the real file
overrides it whenever present, and when it is absent the process starts holding
an obviously-fake value that the placeholder check catches and alerts on. The
desk keeps trading through the REST path (which the gateway still serves) and
the owner is told the socket cannot authenticate. An outage is the wrong
response to a missing credential for a notification feature.

A credential that is delivered but **empty or unreadable** is a different case
and *is* fatal: `load_config` raises rather than fall back to the placeholder
and fail later at the broker, where the error looks like a broker problem.

## The placeholder check, and its honest limit

Every start logs how each broker credential arrived, its length, and nothing
else. If either is obviously a stand-in, that is an `ERROR` line and a separate
Telegram message to the owner.

The check fires only on **positive evidence that a human typed a stand-in**: a
fill-this-in word, whitespace, one repeated character, or a byte that cannot
appear in a header value. It deliberately applies **no length rule and no prefix
rule**, because Alpaca does not document its key format — inventing one would be
an arbitrary number, and it would start rejecting real keys the day Alpaca
changed its issuing format.

**Its limit, stated plainly: it cannot detect a wrong-but-plausible key.** A
revoked, mistyped or other-account key passes every rule. Only the broker can
judge that, which is why the acceptance test below is a once-only observation
that the socket actually authenticated, and not this check.

---

# For the owner — turning the real credential on

Nothing here is reversible in a way that loses anything: step 9 puts the desk
back exactly as it is today. Do these in order. Steps 1 to 5 are done once, on
the box, as yourself.

1. Sign in to the trading account's dashboard and copy the two paper-trading
   values it shows you: the key and the secret.
2. Open a terminal on the box and become the desk's own account by running
   `sudo -u qamc -i`.
3. Make the folder the desk will read the credentials from, private to that
   account, with `mkdir -p ~/credentials && chmod 700 ~/credentials`.
4. Write the key, replacing the bracketed part with what you copied, and nothing
   else on the line: `printf '%s' '<paste-your-key-here>' > ~/credentials/alpaca_api_key`
5. Write the secret the same way: `printf '%s' '<paste-your-secret-here>' > ~/credentials/alpaca_secret_key`
6. Lock both files so only the desk can read them: `chmod 400 ~/credentials/alpaca_api_key ~/credentials/alpaca_secret_key`
7. Check they are the length you expect without showing them, using
   `wc -c ~/credentials/alpaca_api_key ~/credentials/alpaca_secret_key` — it
   prints a number of characters per file and never the contents.
8. Ask an engineer to install the updated service files and restart the desk;
   until that is done nothing has changed.
9. To undo all of this at any time, delete the two files with
   `rm -f ~/credentials/alpaca_api_key ~/credentials/alpaca_secret_key` and
   restart the desk — it returns to exactly today's behaviour, trading over the
   gateway with the placeholder, and tells you it is doing so.

**How you confirm it worked, without ever printing the key.** Start a session and
look at the log for the line beginning `credential delivery`. It names each
credential, says whether it arrived as a *systemd credential* or from the
*environment*, and gives its length. Two things together mean it is right: the
source says `systemd credential`, and the length matches what step 7 printed. If
either credential is still a stand-in you will get a Telegram message saying so
in plain words — you do not have to go looking.

**The one proof that actually settles it.** The live fill websocket stays switched
**off** in this change. Turn it on only after the two checks above pass, and then
confirm **once** that the socket authenticated — described under "Acceptance" below.
Until that single observation exists, the credential is not proven, whatever the
log says about lengths.

## Acceptance — how to know the socket authenticated, once

The lengths-and-source log line proves the process *received* something real
enough. It does not prove the broker *accepted* it. Those are different claims
and this project has already paid for confusing them.

With the websocket flag switched on, the existence proof is the socket's own
authentication response in the journal for a session unit — the stream logs that
it connected and was authenticated before it begins reporting fills. One
observation is enough and it is the whole acceptance test: it is a statement the
broker made, not one the desk made about itself. No credential value appears in
that line, so nothing needs to be redacted to read it.

If the socket instead reports an authentication failure, the credential is wrong
rather than absent — the placeholder check cannot tell you that, and only this
step can.
