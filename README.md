# lpagent-tracker

Realtime Telegram alert bot for Uniswap V3/V4 LP positions on Robinhood Chain (chainId 4663), tracked through the LPAgent API.

Monitors target wallets and pushes **OPEN / CLOSE / LIQUIDITY CHANGE** alerts to a Telegram chat within seconds, with inline buttons to the target's LPAgent portfolio and the GMGN token chart.

## Features

- **HTTP-only watcher** — polls `api.lpagent.xyz` (the CF-free mirror of api.lpagent.io) every 5s per target; no on-chain RPC needed for tracking
- **Unauthenticated API** — all portfolio endpoints are public; only requires the `chain: ROBINHOOD` header
- **Rich alerts**
  - OPEN: value, invested, PnL, fee earned, DPR, tick range, in-range status
  - CLOSE: hold age, invested, returned, total fee, total PnL, range, wallet winrate
  - CHANGE: liquidity delta with configurable threshold
- **Inline buttons**
  - `[<label>]` → target's LPAgent portfolio page
  - `[🐸 GMGN <SYMBOL>]` → GMGN chart of the volatile token of the pair (auto-skips stablecoins), with referral tag
- **Telegram command interface** (auto-registered via `setMyCommands`, no BotFather needed)
  - `/status` — watcher health, uptime, open positions per target
  - `/positions [addr]` — current open positions of a target
  - `/targets` — list monitored wallets
  - `/add 0xADDR [label]` — add target (persisted)
  - `/remove 0xADDR` — remove target
  - `/test` — send test alert
  - `/help` — command reference
- **Optional on-chain fast path** — `eth_getLogs` watcher (4s) against the Robinhood NFPM V3/V4 contracts for sub-10s alerts; disabled by default
- **Chat authorization** — only the configured `chat_id` may interact with the bot

## Quickstart

### 1. Requirements

- Linux VPS, Python 3.10+
- A Telegram bot token (create via [@BotFather](https://t.me/BotFather))
- Your Telegram chat ID

### 2. Install

```bash
git clone <this-repo> /opt/lpagent-tracker
cd /opt/lpagent-tracker

python3 -m venv venv
venv/bin/pip install curl_cffi
```

### 3. Configure

```bash
cp config.example.json config.json
chmod 600 config.json

# Bot token (never commit this file)
echo "123456:ABC-your-bot-token" > .token
chmod 600 .token
```

Edit `config.json`:

| Key | Description |
|---|---|
| `targets` | List of `{address, label}` wallets to monitor |
| `telegram.chat_id` | Authorized chat that receives alerts and can use commands |
| `api_watcher_interval_s` | Fast-poll interval per target (default 5s) |
| `use_onchain_watcher` | Set `true` + `rpc_url` to enable the on-chain fast path |

### 4. Run

```bash
venv/bin/python monitor.py
```

Or install as a systemd service:

```bash
cp lpagent-tracker.service /etc/systemd/system/
# adjust ExecStart/WorkingDirectory paths if not using /opt/lpagent-tracker
systemctl daemon-reload
systemctl enable --now lpagent-tracker
journalctl -u lpagent-tracker -f
```

### 5. Verify

- Send `/status` to your bot in Telegram — it should reply with watcher uptime and open positions
- Send `/test` to fire a sample alert
- Add targets: `/add 0x39a329cdd8ac9d61155c6f15b36c7f95d3ea98c1 whale`

## Configuration flags

| Flag | Purpose |
|---|---|
| `--register-commands` | Only register the bot command menu, then exit |
| `--reset-state` | Clear position baseline (re-baselines on next poll, no alerts) |
| `--test-alert` | Send a test alert and exit |

## API notes

Full endpoint reference: `references/api-endpoints.md` (80 endpoints mapped from the web app).

- Base: `https://api.lpagent.xyz/api` (plain); `https://api.lpagent.io/api` is the CF-challenged mirror used as fallback
- Headers: `chain: ROBINHOOD`, `Origin: https://app.lpagent.io`
- Rate limits are lenient: 60-request bursts and sustained 1 rps verified with zero 429s
- Key endpoints: `lp-positions/opening/{owner}`, `historical/{owner}`, `overview/{owner}`, `pools/{id}/top-lpers`

## Project layout

```
monitor.py              # single-file bot: API client, watchers, alerts, TG commands
config.example.json     # sanitized config template (real config.json is gitignored)
references/api-endpoints.md  # LPAgent API recon
lpagent-tracker.service # systemd unit
.token                  # bot token (gitignored)
config.json             # live config (gitignored)
state.json              # position baseline (gitignored)
```

## Security

- Bot token lives in `.token` (gitignored); config carries only the path
- `config.json` may contain private RPC URLs — gitignored, `config.example.json` ships placeholders instead
- Only the authorized `chat_id` can issue commands; other chats are logged and ignored
