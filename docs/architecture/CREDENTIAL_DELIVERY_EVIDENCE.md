# Credential Delivery — Accepted Architecture

> **THE source-of-truth document for credentials.** Start with the next section.

Status: **accepted and commissioned; last verified 2026-10-05.** This is the durable architecture reference for how QAMC obtains real credentials. See `docs/STATE.md` for current authorization and why the custom proxy from commit `2207b0b74287101ea65ce79782081e51a27420ba` is rejected architecture and must not be revived.

## Where credentials live — read this first

- **OneCLI is the source of truth for provider credentials routed through its gateway:** OpenRouter, Google direct and FRED. Operator runbook: `ops/onecli/README.md`.
- **The two Alpaca Paper key pairs are the narrow exception.** Production and the separate disposable account each have qamc-owned, mode-0400 source files under `/home/qamc/credentials/`. Their processes need the real value for Alpaca's in-band websocket authentication, which a header-injecting gateway cannot perform.
- **The repo's `.env` is NOT the source of truth.** It is stale leftovers. Its two broker keys were tested against the broker's account endpoint and do not authenticate at all. Finding a key in `.env` proves nothing about what is real; do not copy from it and do not "fix" it by pasting real values in.
- **How an operator changes a gateway credential:** through OneCLI's own dashboard, bound to `127.0.0.1` port `10254` on the VPS and administered by `ubuntu` (reach it with an SSH tunnel). Alpaca file rotation follows the restricted file procedure under "For the owner" below.
- **Agents never read the vault.** Real values never go in the repo, a prompt, a log or a doc: names and provenance only.
- Do not confuse OneCLI with any other credential tool on this shared VPS (another tenant runs its own). Nothing in that tool is part of QAMC.

Two separate questions, deliberately not conflated:

- **Storage (where the truth is kept):** OneCLI for gateway providers; restricted qamc-owned files for the two Alpaca Paper key pairs.
- **Delivery (how a process receives a value):** the OneCLI gateway injects provider credentials into outbound HTTP. For Alpaca, systemd materialises the selected account's key pair as read-only files on a private tmpfs and points the process at them with `CREDENTIALS_DIRECTORY`, so the value never enters the process environment. `src/credentials.py` implements the reader (see "How it is delivered" below).

## OneCLI Credential Gateway

- OneCLI is the gateway credential-delivery layer for QAMC's LLM and FRED providers, and still fronts the production desk's REST environment. It runs under Docker on the VPS, administered by `ubuntu` (see `ops/onecli/README.md`); `qamc` and `dev` are never added to the `docker` group and cannot reach the Docker socket.
- Secrets are stored only in OneCLI, never duplicated in QAMC's own configuration. QAMC's `.env` and `config/settings.yaml` are not authoritative: the REST placeholders are inert, and any real-looking broker key left in `.env` is stale and does not authenticate. The desk's websocket key arrives by systemd credential files (below), not from `.env`.
- Agent access uses explicit secret grants (`secretMode: "selective"` on the Default Agent). Creating a secret does not automatically make it available to an agent — granting it is a separate step.
- The gateway (port `10255`) matches outbound requests to a secret by destination host/path and injects the real credential (header or query parameter) before forwarding; it binds `127.0.0.1` only. The management dashboard (port `10254`) listens on loopback and this VPS's private Tailscale addresses, never a public interface, so the operator can reach it only locally or through the private tailnet.
- QAMC's consuming code needs no awareness of OneCLI itself: `src/agents/base.py`'s OpenRouter and Google-direct branches, `src/execution/broker.py`'s `AlpacaBroker`, and `src/data/macro.py`'s `MacroDataProvider` use their normal SDK/HTTP transports (`openai`/`httpx`, `alpaca-py`/`requests`, `fredapi`/`urllib`), so each inherits environment-driven proxy/CA trust. The current Google-direct route deliberately uses Google's OpenAI-compatible endpoint so OneCLI can inject its bearer credential through the same gateway mechanism; see `.env.example` and `config/settings.yaml`.
- Client-side wiring is three environment variables in `/home/qamc/quant-agent/.env` (operator-only — `dev` cannot write into `/home/qamc`): `HTTPS_PROXY` (`http://x:<agent-token>@127.0.0.1:10255` — note `127.0.0.1`, not the `host.docker.internal` OneCLI's own `GET /api/container-config` returns by default, which only resolves inside a Docker container and not for QAMC's bare `qamc`-account processes), `SSL_CERT_FILE`, and `REQUESTS_CA_BUNDLE` (both pointed at OneCLI's gateway CA cert — `requests`, Alpaca's transport, does not honor `SSL_CERT_FILE` alone).

## Historical secondary-account routing through OneCLI — retired 2026-10-05

The first rehearsal broker-conformance run selected the disposable $10,000
Paper account through a separate OneCLI agent token. That proved REST behavior,
but it did not reproduce production's credential-file boundary, could not
authenticate the Alpaca fill websocket, and its grant did not cover the market
data host. The evidence from that run remains in `ops/rehearsal/CONFORMANCE.md`;
the delivery mechanism is now retired.

The disposable account now has its own qamc-owned, mode-0400 source files under
`/home/qamc/credentials/rehearsal/`. A bounded transient user unit loads those
files under the same logical systemd credential names production uses. The
Python process reads them only from `CREDENTIALS_DIRECTORY`; neither key is put
in argv, the environment, the repository, or output. No secondary account
number file is required: each account's number is read from Alpaca's
`get_account()` response. Before a secondary capture, a separate read-only
transient unit obtains the primary Paper number and passes only that number to
the capture unit; the latter reads the secondary number from its own Paper
account endpoint and refuses identical identities or a non-empty secondary
position/order book before the first write-capable session call.

The conformance runner removes proxy and custom-CA variables before building
its Alpaca clients. The capture runner preserves the existing OneCLI route for
OpenRouter and FRED but bypasses that proxy for Alpaca's trading and data hosts.
No permanent service or timer was added; transient units receive private
credential directories that systemd removes when they exit.

Commissioning on 2026-10-05 made only read-only broker calls: direct Paper
authentication succeeded, the secondary account was active, and its identity
was different from the production Paper account. No order was placed.

The conformance runner sets `QAMC_REHEARSAL=1` before Python starts, so every
notification transport is suppressed. It makes no LLM calls. A future bounded
live capture that does call an LLM must still account for shared provider spend
and must not run concurrently with the production desk; the hermetic replay
itself remains credentialless and network-sealed.

## Configured Providers

**OpenRouter** — LLM provider credential for the configured OpenRouter seats and fallback routes. Header-based: `Authorization: Bearer {value}`, host `openrouter.ai`.

**Google AI Studio direct** — primary LLM provider credential for the configured specialist seats. Header-based: `Authorization: Bearer {value}`, host `generativelanguage.googleapis.com`; QAMC uses the OpenAI-compatible `/v1beta/openai/` endpoint because the native Gemini `x-goog-api-key` scheme is not this gateway grant's injection contract.

**FRED** — economic data. URL query-parameter injection, not a header: parameter `api_key`, host `api.stlouisfed.org`.

**Alpaca** — paper trading credentials. Two headers, both required together: `APCA-API-KEY-ID` and `APCA-API-SECRET-KEY`. Values must be injected raw (`{value}`) — **not** `Bearer {value}`; Alpaca's headers are not OAuth-style. Host coverage must be `*.alpaca.markets` (a leading-subdomain wildcard), since QAMC calls both `paper-api.alpaca.markets` (trading) and `data.alpaca.markets` (market/historical data) as distinct hosts. Path pattern must stay blank/unset — Alpaca's API spans multiple paths (`/v2/account`, `/v2/orders`, `/v2/stocks/...`, etc.), and a path pattern narrower than that will block injection on paths it doesn't happen to match.

## Validation

The commissioned QAMC REST credentials — OpenRouter, Google direct, FRED, and the Alpaca key/secret header pair — were verified working end-to-end through the OneCLI gateway, using obviously-fake placeholder credentials sent by the client and comparing gateway-routed vs. direct requests against endpoints that actually validate the credential (not endpoints that respond regardless of auth). Every response body was discarded; no real credential value was ever read, logged, or held by an engineering account.

- OpenRouter connectivity verified through the OneCLI gateway.
- Google-direct connectivity verified through the OneCLI gateway against the OpenAI-compatible endpoint used by QAMC; the real value remains in OneCLI and is granted to the Default Agent.
- FRED connectivity verified through the OneCLI gateway.
- Alpaca connectivity (both the trading host and the market-data host) verified through the OneCLI gateway, after the operator resolved credential-routing issues by correcting grants, host scope, and header value formatting in OneCLI directly.

Commissioning is complete: the runtime `.env` carries the gateway/CA wiring above, and `quant-agent-api.service` health reporting `broker_reachable: true` is the end-to-end acceptance observable. Trading timers remain controlled independently of credential delivery.

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

Both bullets re-verified 2026-09-18 against the installed packages:
`alpaca/trading/stream.py` imports `websockets.legacy.client`, and that
client's `connect` takes no proxy argument (websockets 17.0.1).

**What that in-band payload now is.** Alpaca's authorization reply to
alpaca-py's frame says the `{"action":"authenticate","data":{...}}` form is
being deprecated in favour of `{"action":"auth","key":K,"secret":K}`. The
vendor still builds the deprecated form in its newest release (0.44.0,
checked 2026-09-18), so the desk sends the current frame itself, on a
per-instance wrapper, and falls back to the vendor's frame on a fresh socket
if the current one is refused. Both were measured `authorized` against the
live paper broker on 2026-09-18. See the auth-format block at the top of
`src/execution/broker.py`.

**One consequence worth stating once.** Because the socket authenticates
in-band with the key pair and cannot traverse the gateway, the socket's
identity is whatever account that key pair belongs to — it CANNOT be pointed
at a second account that is selected by a gateway agent token. Measured
2026-09-18: the key pair in `/home/qamc/credentials` reads back account
`<redacted-main-account-id>`. A fill-notification test on any gateway-selected account is
therefore not possible on this box.

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
