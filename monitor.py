#!/usr/bin/env python3
"""
LPAgent LP Position Monitor
===========================
Monitors Uniswap V3/V4 LP positions of target addresses on Robinhood Chain
(chainId 4663) via the LPAgent public API (api.lpagent.xyz) and sends
Telegram alerts when positions are opened, closed, or their liquidity changes.

Fully HTTP-only: no browser, no login. The LPAgent portfolio endpoints are
unauthenticated (the web app relies on `x-lp-portfolio-preview` for guest
viewing, but the API itself answers without it).

Endpoints used (all GET, header `chain: ROBINHOOD`):
  GET {base}/v1/lp-bot/lp-positions/opening/{owner}          -> open positions
  GET {base}/v1/lp-bot/lp-positions/overview/{owner}?protocol=uniswap_v3,uniswap_v4
  GET {base}/v1/lp-bot/lp-positions/revenue/{owner}?period=day&range=7D&protocol=...

Position object fields used:
  tokenId, pairName, pool, currentValue, inputValue, collectedFee,
  uncollectedFee, tickLower, tickUpper, inRange, liquidity, pnl{value,percent},
  createdAt, updatedAt, owner, token0, token1, dpr

Usage:
  python3 monitor.py                # run forever
  python3 monitor.py --once         # single poll cycle, no alerts (baseline-safe)
  python3 monitor.py --test-alert   # send a Telegram test message
  python3 monitor.py --reset-state  # wipe state (next run re-baselines)
  python3 monitor.py --status       # print current state summary
"""

import json
import logging
import os
import signal
import sys
import time
from pathlib import Path

try:
    from curl_cffi import requests as cffi_requests
    HAS_CFFI = True
except ImportError:  # graceful fallback
    import requests as cffi_requests
    HAS_CFFI = False

BASE_DIR = Path(__file__).resolve().parent
CONFIG = json.loads((BASE_DIR / "config.json").read_text())

DASH = "\u2014"  # em dash used as "no data" marker

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
log = logging.getLogger("lpagent-monitor")
log.setLevel(logging.INFO)
_fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
_sh = logging.StreamHandler(sys.stdout)
_sh.setFormatter(_fmt)
log.addHandler(_sh)
_fh = logging.FileHandler(CONFIG.get("log_file", BASE_DIR / "monitor.log"))
_fh.setFormatter(_fmt)
log.addHandler(_fh)

# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------
SESSION = None


def _session():
    global SESSION
    if SESSION is None:
        if HAS_CFFI:
            SESSION = cffi_requests.Session(
                impersonate=CONFIG["http"]["impersonate"], timeout=CONFIG["http"]["timeout_s"]
            )
        else:
            SESSION = cffi_requests.Session()
    return SESSION


def api_get(path, params=None):
    """GET against the primary API host with fallback + retries."""
    last_err = None
    for host in (CONFIG["api_base"], CONFIG["api_base_fallback"]):
        for attempt in range(1, CONFIG["http"]["max_retries"] + 1):
            try:
                r = _session().get(
                    host + path,
                    params=params,
                    headers={
                        "chain": CONFIG["chain"],
                        "accept": "application/json",
                        "origin": "https://app.lpagent.io",
                        "referer": "https://app.lpagent.io/",
                        "user-agent": (
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/146.0.0.0 Safari/537.36"
                        ),
                    },
                )
                if r.status_code == 200:
                    return r.json()
                # 403 from .io = Cloudflare interstitial -> try next host
                if r.status_code in (403, 503):
                    last_err = f"HTTP {r.status_code} from {host}"
                    break
                last_err = f"HTTP {r.status_code} from {host}: {r.text[:200]}"
            except Exception as exc:  # noqa: BLE001
                last_err = f"{type(exc).__name__}: {exc} ({host})"
            time.sleep(CONFIG["http"]["retry_backoff_s"] * attempt)
    raise RuntimeError(f"API request failed for {path}: {last_err}")


def get_open_positions(owner: str) -> list:
    j = api_get(f"/v1/lp-bot/lp-positions/opening/{owner}")
    if j.get("status") != "success":
        raise RuntimeError(f"API error for {owner}: {j.get('message')}")
    return j.get("data") or []


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------


def _tg_token() -> str:
    tok = os.environ.get(CONFIG["telegram"].get("token_env", "LPAGENT_TG_TOKEN"), "")
    if not tok:
        tf = Path(CONFIG["telegram"].get("token_file", ""))
        if tf.is_file():
            tok = tf.read_text().strip()
    if not tok:
        raise RuntimeError(
            "No Telegram token. Set LPAGENT_TG_TOKEN env or "
            f"{CONFIG['telegram'].get('token_file')} file."
        )
    return tok


def lpagent_portfolio_url(owner: str) -> str:
    return f"https://app.lpagent.io/portfolio?address={owner}&chain=ROBINHOOD"


def tg_send(html: str, disable_preview: bool = True, reply_markup: dict | None = None) -> bool:
    """Send an HTML message. Returns True on success."""
    url = f"https://api.telegram.org/bot{_tg_token()}/sendMessage"
    payload = {
        "chat_id": CONFIG["telegram"]["chat_id"],
        "text": html,
        "parse_mode": "HTML",
        "disable_web_page_preview": disable_preview,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    last = None
    for attempt in range(3):
        try:
            r = cffi_requests.post(url, json=payload, timeout=30)
            j = r.json()
            if j.get("ok"):
                return True
            last = json.dumps(j)[:300]
        except Exception as exc:  # noqa: BLE001
            last = f"{type(exc).__name__}: {exc}"
        time.sleep(3 * (attempt + 1))
    log.error("Telegram send failed: %s", last)
    return False


def portfolio_button(owner: str) -> dict:
    """Inline keyboard with a link to the target's LPAgent portfolio page."""
    return {"inline_keyboard": [[
        {"text": "📊 View Portfolio", "url": lpagent_portfolio_url(owner)}
    ]]}


GMGN_REF = "GRrbRWbG"  # referral tag for gmgn.ai links
STABLE_SYMBOLS = {"USDG", "USDC", "USDT", "DAI", "USDE", "FRAX", "BUSD",
                   "TUSD", "USDS", "RLUSD", "USD", "USDC.E"}


def gmgn_token_url(ca: str) -> str:
    return f"https://gmgn.ai/robinhood/token/{ca}?ref={GMGN_REF}"


def _token_meta(pos: dict, idx: int) -> tuple:
    """Return (address, symbol) for token{idx} from a position dict."""
    info = pos.get(f"token{idx}Info") or {}
    addr = info.get("token_address") or pos.get(f"token{idx}")
    sym = info.get("token_symbol") or pos.get(f"tokenName{idx}") or ""
    return addr, str(sym).upper()


def pick_volatile_token(pos: dict) -> tuple:
    """Pick the non-stable token of the pair (for GMGN chart link).

    Falls back to token1 when both/neither are stables.
    """
    a0, s0 = _token_meta(pos, 0)
    a1, s1 = _token_meta(pos, 1)
    if s0 in STABLE_SYMBOLS and a1:
        return a1, s1
    if s1 in STABLE_SYMBOLS and a0:
        return a0, s0
    if a1:
        return a1, s1
    return a0, s0


def target_identity(target: dict) -> str:
    """Display identity for a target wallet: name > label > short address."""
    return (target.get("name") or target.get("label")
            or short_addr(target.get("address", "")))


def position_buttons(target: dict, pos: dict) -> dict:
    """Inline keyboard for position alerts.

    Row: [📈 <identity> —> Portfolio] [🐸 GMGN <SYMBOL>]
    Portfolio button text adapts to the target identity label; GMGN button
    links the volatile token of the opened pair.
    """
    owner = (target.get("address") or pos.get("owner") or "").lower()
    identity = target_identity(target)
    ca, sym = pick_volatile_token(pos)
    row = [{"text": f"\U0001F4C8 {identity}",
            "url": lpagent_portfolio_url(owner)}]
    if ca:
        gmgn_text = f"\U0001F438 GMGN {sym}" if sym else "\U0001F438 GMGN"
        row.append({"text": gmgn_text, "url": gmgn_token_url(ca)})
    return {"inline_keyboard": [row]}


def robinscan_button(owner: str) -> dict:
    """Dual inline buttons: LPAgent portfolio + Robinscan explorer."""
    return {"inline_keyboard": [[
        {"text": "📊 Portfolio", "url": lpagent_portfolio_url(owner)},
        {"text": "🔎 Robinscan", "url": robinscan_addr_url(owner)},
    ]]}


# ---------------------------------------------------------------------------
# Formatting helpers (lp-bot conventions: 6dp stripped, $X,XXX.XX, <code> mono)
# ---------------------------------------------------------------------------


def fmt_usd(v) -> str:
    try:
        return f"${float(v):,.2f}"
    except (TypeError, ValueError):
        return "\u2014"


def fmt_num6(v) -> str:
    """fmt_eth_amount style: up to 6 decimals, trailing zeros stripped."""
    try:
        s = f"{float(v):.6f}".rstrip("0").rstrip(".")
        return s or "0"
    except (TypeError, ValueError):
        return "\u2014"


def fmt_pct(v) -> str:
    try:
        return f"{float(v):+.2f}%"
    except (TypeError, ValueError):
        return "\u2014"


def short_addr(a: str) -> str:
    return f"{a[:8]}...{a[-6:]}" if a and len(a) > 16 else (a or "\u2014")


def robinscan_addr_url(a: str) -> str:
    return f"https://robinscan.io/address/{a}"


def position_alert_html(target, pos, event: str) -> str:
    """Build the Telegram HTML alert for a single position event."""
    owner = (target.get("address") or pos.get("owner") or "").lower()
    identity = target_identity(target)
    token_id = pos.get("tokenId") or "?"
    pair = pos.get("pairName") or "?"
    pnl = pos.get("pnl") or {}

    icons = {"OPEN": "\U0001f7e2", "CLOSE": "\U0001f534", "CHANGE": "\U0001f7e1"}
    tags = {"OPEN": "OPEN", "CLOSE": "CLOSED", "CHANGE": "LIQUIDITY CHANGE"}
    icon = icons.get(event, "\u2139")
    rng = "\u2705 in range" if pos.get("inRange") else "\u26a0 OUT of range"

    lines = [
        f"{icon} <b>{tags.get(event, event)} \u2014 {pair}</b>",
        f"\U0001f3af {identity} \u00b7 <code>{short_addr(owner)}</code>",
        f"CA: <code>{pos.get('pool') or token_id}</code>",
        "",
        f"\U0001f4b0 <b>Value</b>: {fmt_usd(pos.get('currentValue'))}",
    ]
    if event == "OPEN":
        lines += [
            f"\U0001f4e5 <b>Invested</b>: {fmt_usd(pos.get('inputValue'))}",
            f"\U0001f4ca <b>PnL</b>: {fmt_usd(pnl.get('value'))} ({fmt_pct(pnl.get('percent'))})",
            f"\U0001fa96 <b>Fee earned</b>: {fmt_usd(pos.get('collectedFee'))}",
            f"\U0001f4c8 <b>DPR</b>: {fmt_pct(pos.get('dpr'))}",
            f"\U0001f4d0 <b>Range</b>: {pos.get('tickLower', DASH)} \u2013 {pos.get('tickUpper', DASH)} \u00b7 {rng}",
        ]
    elif event == "CLOSE":
        lines += [
            f"\U0001f4e5 <b>Invested</b>: {fmt_usd(pos.get('inputValue'))}",
            f"\U0001fa96 <b>Fee earned</b>: {fmt_usd(pos.get('collectedFee'))}",
            f"\U0001f4ca <b>PnL</b>: {fmt_usd(pnl.get('value'))} ({fmt_pct(pnl.get('percent'))})",
        ]
    else:  # CHANGE (liquidity add/remove)
        lines += [
            f"\U0001f4ca <b>PnL</b>: {fmt_usd(pnl.get('value'))} ({fmt_pct(pnl.get('percent'))})",
            f"\U0001f4a7 <b>Liquidity</b>: {fmt_num6(pos.get('liquidity'))} "
            f"({fmt_pct(pos.get('_liq_delta_pct'))})",
            f"\U0001fa96 <b>Uncollected fee</b>: {fmt_usd(pos.get('uncollectedFee'))}",
            f"\U0001f4d0 <b>Range</b>: {pos.get('tickLower', DASH)} \u2013 {pos.get('tickUpper', DASH)} \u00b7 {rng}",
        ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


def load_state() -> dict:
    p = Path(CONFIG["state_file"])
    if p.is_file():
        try:
            return json.loads(p.read_text())
        except json.JSONDecodeError:
            log.warning("state file corrupt, starting fresh")
    return {}


def save_state(state: dict) -> None:
    p = Path(CONFIG["state_file"])
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(p)


# ---------------------------------------------------------------------------
# Bot commands (Telegram long-polling, registered via setMyCommands)
# ---------------------------------------------------------------------------

import threading

BOT_COMMANDS = [
    ("status", "Monitor status: targets, open positions, uptime"),
    ("positions", "List open positions (all targets or one address)"),
    ("targets", "List monitored targets"),
    ("add", "Add target: /add 0xADDRESS [label]"),
    ("remove", "Remove target: /remove 0xADDRESS"),
    ("test", "Send a test alert"),
    ("help", "Show this help"),
]

START_TIME = time.time()
CONFIG_LOCK = threading.Lock()
POLL_LOCK = threading.Lock()  # serializes poll_target/poll_target_realtime (dedupe alerts)
STATE = {}  # shared state dict (main loop + api watcher thread)


def _authorized(chat_id) -> bool:
    return str(chat_id) == str(CONFIG["telegram"]["chat_id"])


def _fmt_position_line(pos) -> str:
    pnl = pos.get("pnl") or {}
    rng = "in" if pos.get("inRange") else "OUT"
    return (
        f"\u2022 <b>{pos.get('pairName', '?')}</b> \u2014 {fmt_usd(pos.get('currentValue'))} "
        f"| PnL {fmt_usd(pnl.get('value'))} ({fmt_pct(pnl.get('percent'))}) "
        f"| fee {fmt_usd(pos.get('collectedFee'))} | {rng}\n"
        f"  <code>{pos.get('tokenId', '?')}</code>"
    )


def cmd_status() -> str:
    lines = [
        f"\u2139 <b>lpagent-monitor</b> \u2014 {CONFIG['chain']} (Robinhood Chain)",
        f"<b>Uptime</b>: {int(time.time() - START_TIME) // 60} min",
        f"<b>Poll interval</b>: {CONFIG['poll_interval_s']}s",
        f"<b>Targets</b>: {len(CONFIG['targets'])}",
        "",
    ]
    state = load_state()
    for t in CONFIG["targets"]:
        a = t["address"].lower()
        ts = state.get("targets", {}).get(a, {})
        lines.append(
            f"\u2022 <code>{short_addr(a)}</code> [{t.get('label', '')}]: "
            f"{ts.get('open_count', '?')} open"
        )
    return "\n".join(lines)


def cmd_positions(args) -> str:
    addr = args[0].lower() if args else None
    chosen = [t for t in CONFIG["targets"] if not addr or t["address"].lower() == addr]
    if addr and not chosen:
        return f"\u274c <code>{short_addr(args[0])}</code> is not a monitored target. See /targets."
    out = []
    for t in chosen:
        a = t["address"].lower()
        try:
            positions = get_open_positions(a)
        except Exception as exc:  # noqa: BLE001
            out.append(f"\u26a0 <b>{t.get('label', short_addr(a))}</b>: fetch failed \u2014 {str(exc)[:120]}")
            continue
        out.append(f"\n\U0001f4bc <b>{t.get('label', short_addr(a))}</b> \u2014 {len(positions)} open")
        for pos in positions:
            out.append(_fmt_position_line(pos))
    return "\n".join(out) if out else "No positions."


def cmd_targets() -> str:
    lines = ["\U0001f3af <b>Monitored targets</b>:", ""]
    for i, t in enumerate(CONFIG["targets"], 1):
        lines.append(f"{i}. <code>{t['address'].lower()}</code> [{t.get('label', '')}]")
    lines.append("\n/add 0xADDRESS [label] \u2014 add\n/remove 0xADDRESS \u2014 remove")
    return "\n".join(lines)


def cmd_add(args) -> str:
    if not args:
        return "Usage: <code>/add 0xADDRESS [label]</code>"
    raw = args[0].lower()
    if not (raw.startswith("0x") and len(raw) == 42):
        return "\u274c Invalid EVM address. Expected <code>0x</code> + 40 hex chars."
    label = " ".join(args[1:]) or short_addr(raw)
    with CONFIG_LOCK:
        if any(t["address"].lower() == raw for t in CONFIG["targets"]):
            return f"\u274c <code>{short_addr(raw)}</code> is already monitored."
        CONFIG["targets"].append({"address": raw, "label": label})
        (BASE_DIR / "config.json").write_text(json.dumps(CONFIG, indent=2))
    # baseline immediately (no alert spam)
    state = load_state()
    try:
        positions = get_open_positions(raw)
    except Exception as exc:  # noqa: BLE001
        return f"\u274c Added, but first fetch failed: {str(exc)[:150]}"
    tstate = state.setdefault("targets", {}).setdefault(raw, {})
    tstate["positions"] = {
        p["tokenId"]: {"liquidity": p.get("liquidity"), "pairName": p.get("pairName"),
                       "currentValue": p.get("currentValue")}
        for p in positions if p.get("tokenId")
    }
    tstate["initialized"] = True
    tstate["last_poll"] = int(time.time())
    tstate["open_count"] = len(positions)
    save_state(state)
    return (f"\u2705 Target added: <code>{short_addr(raw)}</code> [{label}]\n"
            f"Baseline: {len(positions)} open position(s). Alerts active.")


def cmd_remove(args) -> str:
    if not args:
        return "Usage: <code>/remove 0xADDRESS</code>"
    raw = args[0].lower()
    with CONFIG_LOCK:
        before = len(CONFIG["targets"])
        CONFIG["targets"] = [t for t in CONFIG["targets"] if t["address"].lower() != raw]
        if len(CONFIG["targets"]) == before:
            return f"\u274c <code>{short_addr(raw)}</code> is not monitored."
        (BASE_DIR / "config.json").write_text(json.dumps(CONFIG, indent=2))
    state = load_state()
    state.get("targets", {}).pop(raw, None)
    save_state(state)
    return f"\u2705 Removed <code>{short_addr(raw)}</code>."


def cmd_test() -> str:
    ok = tg_send(
        "\u2705 <b>lpagent-monitor</b> test alert.\n"
        f"Chain: <code>{CONFIG['chain']}</code> | Targets: <code>{len(CONFIG['targets'])}</code>"
    )
    return "Sent." if ok else "\u274c Send failed (see monitor.log)."


def cmd_help() -> str:
    lines = ["\U0001f916 <b>lpagent-monitor commands</b>", ""]
    lines += [f"/{c} \u2014 {d}" for c, d in BOT_COMMANDS]
    lines.append("\nAlerts: \U0001f7e2 OPEN \u00b7 \U0001f534 CLOSE \u00b7 \U0001f7e1 CHANGE (liquidity \u00b115%)")
    return "\n".join(lines)


def handle_update(update: dict) -> None:
    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return
    chat_id = (msg.get("chat") or {}).get("id")
    if not _authorized(chat_id):
        log.warning("unauthorized chat_id=%s ignored", chat_id)
        return
    text = (msg.get("text") or "").strip()
    if not text.startswith("/"):
        return
    parts = text.split()
    cmd = parts[0].lstrip("/").split("@")[0].lower()
    args = parts[1:]
    log.info("command received: /%s %s", cmd, " ".join(args))
    reply = ""
    if cmd in ("start", "help"):
        reply = cmd_help()
    elif cmd == "status":
        reply = cmd_status()
    elif cmd == "positions":
        reply = cmd_positions(args)
    elif cmd in ("targets", "list"):
        reply = cmd_targets()
    elif cmd == "add":
        reply = cmd_add(args)
    elif cmd == "remove":
        reply = cmd_remove(args)
    elif cmd == "test":
        reply = cmd_test()
    else:
        reply = f"Unknown command /{cmd}. See /help."
    if reply:
        tg_send(reply)


def bot_poll_loop() -> None:
    """Long-poll getUpdates in a background thread."""
    token = _tg_token()
    offset = 0
    log.info("bot command listener started (long-poll)")
    while RUNNING:
        try:
            r = cffi_requests.get(
                f"https://api.telegram.org/bot{token}/getUpdates",
                params={"timeout": 25, "offset": offset, "allowed_updates": json.dumps(["message"])},
                timeout=35,
            )
            j = r.json()
            for upd in j.get("result", []):
                offset = max(offset, upd["update_id"] + 1)
                handle_update(upd)
        except Exception as exc:  # noqa: BLE001
            log.error("getUpdates error: %s", exc)
            time.sleep(5)


def register_commands() -> bool:
    """Register the command menu with Telegram (replaces manual @BotFather setup)."""
    payload = {"commands": [{"command": c, "description": d} for c, d in BOT_COMMANDS]}
    try:
        r = cffi_requests.post(
            f"https://api.telegram.org/bot{_tg_token()}/setMyCommands",
            json=payload, timeout=30,
        )
        return r.json().get("ok", False)
    except Exception as exc:  # noqa: BLE001
        log.error("setMyCommands failed: %s", exc)
        return False


# ---------------------------------------------------------------------------
# On-chain realtime watcher (Robinhood Chain 4663)
# ---------------------------------------------------------------------------

# Event topic0 hashes (keccak256 of event signatures)
TOPIC_TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
TOPIC_INCREASE_LIQ = "0x3067048beee31b25b2f1681f88dac838c8bba36af25bfb2b7cf7473a5847e35f"
TOPIC_DECREASE_LIQ = "0x26f6a048ee9138f2c0ce266f322cb99228e8d619ae2bff30c67f8dcf9d2377b4"
TOPIC_MODIFY_LIQ = "0xb05ce04f5ed5b1098e7bde60889169590ebb280ae0a9d8187f884abd2a6a0f99"
TOPIC_COLLECT = "0x40d0efd1a53d60ecbf40971b9daf7dc90178c3aadc7aab1765632738fa8b8f01"

# Uniswap NonfungiblePositionManager contracts on Robinhood Chain (4663)
NFPM_CONTRACTS = {
    "0x73991a25c818bf1f1128deaab1492d45638de0d3": "uniswap_v3",
    "0x58daec3116aae6d93017baaea7749052e8a04fa7": "uniswap_v4",
}

WATCHER_STATE_FILE = BASE_DIR / "watcher_state.json"


def _rpc_url() -> str:
    return CONFIG.get("rpc_url") or "https://rpc.mainnet.chain.robinhood.com"


def _rpc_fallback_url():
    return CONFIG.get("rpc_url_fallback")


def _rpc_call_once(url: str, method: str, params: list, timeout: int):
    """Single JSON-RPC attempt. Returns (result, None) or (None, error)."""
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    r = cffi_requests.post(
        url, data=payload,
        headers={"Content-Type": "application/json"},
        timeout=timeout,
    )
    j = r.json()
    if "error" in j:
        return None, j["error"]
    return j.get("result"), None


def rpc_call(method: str, params: list, timeout: int = 20, allow_fallback: bool = True) -> dict:
    """JSON-RPC with 429 backoff + automatic failover to the fallback RPC.

    Primary (rpc_url, QuickNode) is tried first with up to 3 attempts when
    rate-limited (429). Any other error (e.g. discover-plan getLogs range
    cap, code -32615) or exhausted retries fail over to the fallback
    (public Robinhood RPC) which accepts large block ranges.
    """
    urls = [_rpc_url()]
    if allow_fallback and _rpc_fallback_url():
        urls.append(_rpc_fallback_url())
    last_err = "no RPC configured"
    for url in urls:
        for attempt in range(3):
            try:
                res, err = _rpc_call_once(url, method, params, timeout)
            except Exception as exc:  # noqa: BLE001
                last_err = f"{type(exc).__name__}: {exc} ({url})"
                time.sleep(1)
                continue
            if err is None:
                return res
            last_err = f"{err} ({url})"
            if err.get("code") == 429 and attempt < 2:
                time.sleep(2 * (attempt + 1))
                continue
            break  # non-429 error -> next provider (fallback)
    raise RuntimeError(f"RPC {method} failed: {last_err}")


def load_watcher_state() -> dict:
    if WATCHER_STATE_FILE.is_file():
        try:
            return json.loads(WATCHER_STATE_FILE.read_text())
        except json.JSONDecodeError:
            log.warning("watcher state corrupt, starting fresh")
    return {}


def save_watcher_state(ws: dict) -> None:
    tmp = WATCHER_STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(ws))
    tmp.replace(WATCHER_STATE_FILE)


def _topic_addr(topic: str) -> str:
    """0x-padded 32-byte topic -> 20-byte address."""
    return "0x" + topic[-40:].lower()


def _token_id_of(log: dict) -> str:
    """Build the LPAgent-style tokenId '<nfpm>-<nftId>' from a log."""
    addr = log["address"].lower()
    if log["topics"][0] == TOPIC_TRANSFER and len(log["topics"]) >= 4:
        nft_id = str(int(log["topics"][3], 16))
    else:  # Increase/DecreaseLiquidity/Collect: tokenId is first data param
        nft_id = str(int(log["data"][2:66], 16))
    return f"{addr}-{nft_id}"


def onchain_owner_activity(owner: str, seen: dict, from_block: int) -> list:
    """Fetch NFPM events for `owner` since `from_block`. Returns raw logs."""
    owner_topic = "0x" + "0" * 24 + owner[2:].lower()
    return watcher_fetch_events([owner], seen, from_block)


def watcher_fetch_events(owners: list, seen: dict, from_block: int) -> list:
    """Fetch NFPM events for owners since from_block.

    Topic filter: topic0 IN (Transfer, IncreaseLiquidity, DecreaseLiquidity,
    ModifyLiquidity, Collect) AND topic1 IN (owner topics). topic1 is the
    sender/from slot for all these events, which covers opens (liquidity
    events accompany every mint), closes (burns) and liquidity changes.

    QuickNode (primary) caps eth_getLogs at a 5-block range on the discover
    plan, so ranges are chunked into <=GETLOGS_CHUNK calls; if the range is
    too large (or QN errors), a single call goes to the fallback public RPC
    which accepts big ranges.
    """
    owner_topics = ["0x" + "0" * 24 + o[2:].lower() for o in owners]
    topics = [
        [TOPIC_TRANSFER, TOPIC_INCREASE_LIQ, TOPIC_DECREASE_LIQ,
         TOPIC_MODIFY_LIQ, TOPIC_COLLECT],
        owner_topics,
    ]
    head = int(rpc_call("eth_blockNumber", []), 16)
    if head <= from_block:
        return []
    address = list(NFPM_CONTRACTS.keys())
    span = head - from_block

    def _dedupe(logs):
        out = []
        for lg in logs:
            key = lg["transactionHash"] + lg["logIndex"]
            if key in seen:
                continue
            seen[key] = True
            out.append(lg)
        return out

    if span <= GETLOGS_CHUNK:
        # single call; if QN rejects (range cap/plan error), fallback RPC
        got = rpc_call("eth_getLogs", [{
            "address": address, "topics": topics,
            "fromBlock": hex(from_block), "toBlock": "latest",
        }])
        return _dedupe(got)

    # large span: chunk on QN, but cap the number of chunks; beyond that,
    # prefer one big call on the public fallback RPC (no range limit there)
    n_chunks = (span + GETLOGS_CHUNK - 1) // GETLOGS_CHUNK
    if n_chunks > GETLOGS_MAX_CHUNKS:
        got = rpc_call("eth_getLogs", [{
            "address": address, "topics": topics,
            "fromBlock": hex(from_block), "toBlock": "latest",
        }])  # QN will reject range -> auto-failover inside rpc_call
        return _dedupe(got)

    logs = []
    for i in range(n_chunks):
        frm = from_block + i * GETLOGS_CHUNK
        to = min(frm + GETLOGS_CHUNK - 1, head)
        got = rpc_call("eth_getLogs", [{
            "address": address, "topics": topics,
            "fromBlock": hex(frm), "toBlock": hex(to),
        }])
        logs.extend(got)
    return _dedupe(logs)


def fmt_age(age_hours) -> str:
    try:
        h = float(age_hours)
        if h < 1:
            return f"{int(h * 60)}m"
        if h < 48:
            return f"{h:.1f}h"
        return f"{h / 24:.1f}d"
    except (TypeError, ValueError):
        return DASH


def close_alert_rich_html(target, hist_pos) -> str:
    """CLOSE alert with Age, Invested, Total fee, Total PnL, Range, Winrate."""
    owner = (target.get("address") or hist_pos.get("owner") or "").lower()
    identity = target_identity(target)
    pnl = hist_pos.get("pnl") or {}
    age_h = hist_pos.get("ageHour") or hist_pos.get("age")
    lines = [
        f"\U0001f534 <b>CLOSED \u2014 {hist_pos.get('pairName', '?')}</b>",
        f"\U0001f3af {identity} \u00b7 <code>{short_addr(owner)}</code>",
        f"CA: <code>{hist_pos.get('pool') or hist_pos.get('tokenId', '?')}</code>",
        "",
        f"\u23f1 <b>Age (hold)</b>: {fmt_age(age_h)}",
        f"\U0001f4e5 <b>Invested</b>: {fmt_usd(hist_pos.get('inputValue'))}",
        f"\U0001f4e4 <b>Returned</b>: {fmt_usd(hist_pos.get('outputValue'))}",
        f"\U0001fa96 <b>Total fee</b>: {fmt_usd(hist_pos.get('collectedFee'))}",
        f"\U0001f4ca <b>Total PnL</b>: {fmt_usd(pnl.get('value'))} ({fmt_pct(pnl.get('percent'))})",
        f"\U0001f4d0 <b>Range</b>: {hist_pos.get('tickLower', DASH)} \u2013 {hist_pos.get('tickUpper', DASH)}",
    ]
    # winrate from overview (best-effort)
    try:
        ov = api_get(f"/v1/lp-bot/lp-positions/overview/{owner}",
                     params={"protocol": "uniswap_v3,uniswap_v4"})
        for o in ov.get("data") or []:
            wr = o.get("win_rate") or {}
            wr_all = wr.get("ALL")
            if wr_all is not None:
                lines.append(f"\U0001f3af <b>Winrate</b>: {float(wr_all) * 100:.1f}% "
                             f"({o.get('win_lp')}/{o.get('total_lp')} LP)")
                break
    except Exception:  # noqa: BLE001
        pass
    return "\n".join(lines)


def api_watcher_loop() -> None:
    """Low-latency HTTP-only watcher: poll LPAgent opening/{owner} directly.

    Experiment per user direction: no on-chain RPC at all — the LPAgent
    API itself is the realtime source. Measured: unauthenticated, ~0.4 rps
    for 2 targets at 5s interval (30 rapid requests earlier showed no 429).
    Latency = poll interval + indexer lag of LPAgent's own pipeline
    (which is the same lag any RPC+API hybrid would pay for rich data).
    """
    interval = CONFIG.get("api_watcher_interval_s", 5)
    log.info("api watcher started (HTTP-only): %d target(s), poll %ds",
             len(CONFIG["targets"]), interval)
    while RUNNING:
        t0 = time.time()
        for target in list(CONFIG["targets"]):
            if not RUNNING:
                break
            addr = target["address"].lower()
            label = target.get("label") or short_addr(addr)
            try:
                poll_target(target, STATE)
            except Exception as exc:  # noqa: BLE001
                log.error("[%s] api-watch poll failed: %s", label, exc)
            time.sleep(1)  # spacing between targets
        save_state(STATE)
        sleep_for = max(1, interval - (time.time() - t0))
        for _ in range(int(sleep_for)):
            if not RUNNING:
                break
            time.sleep(1)


def watcher_loop() -> None:
    """Low-latency on-chain watcher: poll eth_getLogs every few seconds.

    When a target's NFPM activity is seen on-chain, immediately refresh that
    target's positions from the LPAgent API and emit alerts. This gives
    ~4-10s detection latency vs the 60s API polling loop.
    """
    seen = {}
    ws = load_watcher_state()
    # allow a small re-scan window to survive restarts
    last_block = ws.get("last_block")
    try:
        last_block = int(last_block) if last_block is not None else int(rpc_call("eth_blockNumber", []), 16) - 5
    except Exception as exc:  # noqa: BLE001
        log.error("watcher init failed: %s", exc)
        return
    interval = CONFIG.get("watcher_interval_s", 4)
    log.info("on-chain watcher started: %d target(s), poll %ds, from block %d",
             len(CONFIG["targets"]), interval, last_block)

    while RUNNING:
        try:
            head = int(rpc_call("eth_blockNumber", []), 16)
            if head <= last_block:
                time.sleep(interval)
                continue
            owners = [t["address"].lower() for t in CONFIG["targets"]]
            logs = watcher_fetch_events(owners, seen, last_block)
            if logs:
                for lg in logs:
                    log.info("on-chain event: %s %s tx=%s actor=%s",
                             NFPM_CONTRACTS.get(lg["address"].lower(), "?"),
                             lg["topics"][0][:10], lg["transactionHash"],
                             _topic_addr(lg["topics"][1]))
                # immediate API refresh for affected targets (fast path)
                state = load_state()
                for target in CONFIG["targets"]:
                    try:
                        poll_target_realtime(target, state)
                    except Exception as exc:  # noqa: BLE001
                        log.error("realtime refresh %s failed: %s",
                                  short_addr(target["address"]), exc)
                save_state(state)
            last_block = head
            ws["last_block"] = last_block
            save_watcher_state(ws)
        except Exception as exc:  # noqa: BLE001
            log.error("watcher cycle failed: %s", exc)
            time.sleep(10)


def poll_target_realtime(target: dict, state: dict) -> None:
    """Fast-path refresh after on-chain activity. Emits alerts for diffs.

    OPEN events reuse the standard alert; CLOSE events fetch the historical
    record for rich detail (age, invested, fees, winrate).
    """
    addr = target["address"].lower()
    alerts_cfg = CONFIG["alerts"]
    label = target.get("label") or short_addr(addr)

    with POLL_LOCK:
        current_list = get_open_positions(addr)
        current = {p["tokenId"]: p for p in current_list if p.get("tokenId")}
        prev = state.get("targets", {}).get(addr, {}).get("positions", {})
        tstate = state.setdefault("targets", {}).setdefault(addr, {})

        # new positions -> OPEN alert
        for tid, pos in current.items():
            if tid not in prev and alerts_cfg.get("on_new_position"):
                log.info("[%s] REALTIME OPEN %s", label, tid)
                tg_send(position_alert_html(target, pos, "OPEN"),
                        reply_markup=position_buttons(target, pos))

        # closed positions -> rich CLOSE alert from historical endpoint
        for tid in prev:
            if tid not in current and alerts_cfg.get("on_position_closed"):
                hist = _confirm_closed(state, addr, tid)
                if not hist:
                    continue  # debounce: transient disappearance or not yet in historical
                log.info("[%s] REALTIME CLOSE %s", label, tid)
                tg_send(close_alert_rich_html(target, hist),
                        reply_markup=position_buttons(target, hist))

        # reset pending-close counters for positions that are back/open
        pending = tstate.setdefault("_pending_close", {})
        for tid in list(pending):
            if tid in current:
                pending.pop(tid, None)

        # keep pending-close entries so the debounce counter survives
        new_positions = {
            tid: {"liquidity": p.get("liquidity"), "pairName": p.get("pairName"),
                  "currentValue": p.get("currentValue")}
            for tid, p in current.items()
        }
        for tid, old in prev.items():
            if tid not in current and tid in (tstate.get("_pending_close") or {}):
                new_positions[tid] = old
        tstate["positions"] = new_positions
        tstate["open_count"] = len(current)
        tstate["last_poll"] = int(time.time())


def _fetch_closed_position(owner: str, token_id: str) -> dict | None:
    """Fetch a closed position record from the historical endpoint."""
    try:
        # historical endpoint is paginated; find the position by scanning
        # recent pages for the tokenId
        for page in (1, 2, 3):
            j = api_get(f"/v1/lp-bot/lp-positions/historical/{owner}",
                        params={"page": page, "pageSize": 50,
                                "order_by": "closeAt", "sort_order": "desc"})
            data = j.get("data") or {}
            rows = data.get("data") if isinstance(data, dict) else data
            for row in rows or []:
                if row.get("tokenId") == token_id or row.get("position") == token_id:
                    return row
            pg = data.get("pagination") if isinstance(data, dict) else None
            if not pg or page >= pg.get("totalPages", 1):
                break
    except Exception as exc:  # noqa: BLE001
        log.error("historical fetch failed: %s", exc)
    return None


CLOSE_CONFIRM_CYCLES = 2   # position must be missing N consecutive polls before CLOSE alerts
CLOSE_GIVEUP_CYCLES = 12   # drop silently after this many misses without a historical record


def _confirm_closed(state: dict, addr: str, tid: str) -> dict | None:
    """Return the historical record if tid qualifies as truly closed, else None.

    Debounces transient indexer glitches where a position briefly
    disappears from the opening endpoint: the tid must be missing for
    CLOSE_CONFIRM_CYCLES consecutive polls AND have a record in the
    historical endpoint. When the position reappears, the caller resets
    the counter. After CLOSE_GIVEUP_CYCLES misses without a historical
    record, the tid is dropped silently (no empty alert spam).
    """
    tstate = state.setdefault("targets", {}).setdefault(addr, {})
    pending = tstate.setdefault("_pending_close", {})
    n = pending.get(tid, 0) + 1

    if n >= CLOSE_GIVEUP_CYCLES:
        log.info("[%s] giving up on close detection for %s (no historical record)",
                 tstate.get("label") or short_addr(addr), tid)
        pending.pop(tid, None)
        return None
    if n < CLOSE_CONFIRM_CYCLES:
        pending[tid] = n
        return None

    # debounce window passed -> verify against historical
    hist = _fetch_closed_position(addr, tid)
    if hist:
        pending.pop(tid, None)
        return hist
    # no historical record yet -> likely still closing on the indexer
    pending[tid] = n
    log.info("[%s] close pending for %s: not yet in historical (n=%d)",
             tstate.get("label") or short_addr(addr), tid, n)
    return None




RUNNING = True


def _stop(_sig, _frm):
    global RUNNING
    RUNNING = False
    log.info("shutdown signal received")


def poll_target(target: dict, state: dict) -> None:
    """Fetch current open positions for one target and emit diff alerts."""
    addr = target["address"].lower()
    alerts_cfg = CONFIG["alerts"]
    label = target.get("label") or short_addr(addr)

    with POLL_LOCK:
        positions = get_open_positions(addr)
        current = {}
        for pos in positions:
            tid = pos.get("tokenId")
            if not tid:
                continue
            current[tid] = pos

        prev = state.get("targets", {}).get(addr, {}).get("positions", {})
        first_seen = not bool(prev) and not state.get("targets", {}).get(addr, {}).get("initialized")

        tstate = state.setdefault("targets", {}).setdefault(addr, {})
        new_state_positions = {}

        for tid, pos in current.items():
            old = prev.get(tid)
            if old is None:
                if first_seen:
                    log.info("[%s] baseline: %s %s (open)", label, pair_of(pos), tid)
                elif alerts_cfg.get("on_new_position"):
                    log.info("[%s] NEW position %s %s", label, pair_of(pos), tid)
                    tg_send(position_alert_html(target, pos, "OPEN"),
                            reply_markup=position_buttons(target, pos))
            else:
                # liquidity change detection
                if alerts_cfg.get("on_liquidity_change"):
                    old_liq = _to_float(old.get("liquidity"))
                    new_liq = _to_float(pos.get("liquidity"))
                    if old_liq is not None and new_liq is not None and old_liq > 0:
                        delta_pct = (new_liq - old_liq) / old_liq * 100
                        if abs(delta_pct) >= alerts_cfg.get("min_liquidity_change_pct", 15):
                            pos = dict(pos)
                            pos["_liq_delta_pct"] = delta_pct
                            log.info("[%s] LIQUIDITY %s %s (%.1f%%)", label, pair_of(pos), tid, delta_pct)
                            tg_send(position_alert_html(target, pos, "CHANGE"),
                                    reply_markup=position_buttons(target, pos))
            new_state_positions[tid] = {
                "liquidity": pos.get("liquidity"),
                "pairName": pos.get("pairName"),
                "currentValue": pos.get("currentValue"),
            }

        for tid, old in prev.items():
            if tid not in current and not first_seen:
                if alerts_cfg.get("on_position_closed"):
                    hist = _confirm_closed(state, addr, tid)
                    if not hist:
                        continue  # debounce: transient disappearance or not yet in historical
                    log.info("[%s] CLOSED position %s", label, tid)
                    tg_send(close_alert_rich_html(target, hist),
                            reply_markup=position_buttons(target, hist))

        # keep pending-close entries in state so the debounce counter survives
        # across cycles until confirmed closed or the position reappears
        for tid, old in prev.items():
            if tid not in current:
                if tstate.get("_pending_close", {}).get(tid) is not None:
                    new_state_positions[tid] = old

        tstate["positions"] = new_state_positions
        tstate["initialized"] = True
        tstate["last_poll"] = int(time.time())
        tstate["open_count"] = len(current)

        # reset pending-close counters for positions that are back/open
        pending = tstate.setdefault("_pending_close", {})
        for tid in list(pending):
            if tid in current:
                pending.pop(tid, None)


def pair_of(pos) -> str:
    return pos.get("pairName") or (pos.get("token0", "")[:8] + "/" + pos.get("token1", "")[:8] or "?")


def _to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def cycle(state: dict) -> None:
    for target in CONFIG["targets"]:
        addr = target["address"].lower()
        label = target.get("label") or short_addr(addr)
        try:
            poll_target(target, state)
        except Exception as exc:  # noqa: BLE001
            log.error("[%s] poll failed: %s", label, exc)
            if CONFIG["alerts"].get("error_alerts"):
                tg_send(f"\u26a0 <b>lpagent-monitor</b> poll error for <code>{short_addr(addr)}</code>: "
                        f"<code>{str(exc)[:200]}</code>")
        time.sleep(2)  # be gentle with the API between targets
    save_state(state)


def main() -> int:
    global RUNNING
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    args = sys.argv[1:]

    if "--test-alert" in args:
        ok = tg_send(
            "\u2705 <b>lpagent-monitor</b> is alive.\n"
            f"Chain: <code>{CONFIG['chain']}</code> (Robinhood Chain, id 4663)\n"
            f"Targets: <code>{len(CONFIG['targets'])}</code>"
        )
        return 0 if ok else 1

    if "--reset-state" in args:
        Path(CONFIG["state_file"]).unlink(missing_ok=True)
        log.info("state reset")
        return 0

    if "--register-commands" in args:
        ok = register_commands()
        log.info("setMyCommands: %s", "ok" if ok else "FAILED")
        return 0 if ok else 1

    state = load_state()

    if "--status" in args:
        for t in CONFIG["targets"]:
            a = t["address"].lower()
            ts = state.get("targets", {}).get(a, {})
            print(f"{a} [{t.get('label', '')}]: open={ts.get('open_count', '?')} "
                  f"last_poll={ts.get('last_poll', 'never')}")
        return 0

    interval = CONFIG["poll_interval_s"]
    log.info(
        "monitoring %d target(s) on %s every %ds (http=%s)",
        len(CONFIG["targets"]), CONFIG["chain"], interval,
        "curl_cffi/" + CONFIG["http"]["impersonate"] if HAS_CFFI else "requests",
    )

    # register command menu + start Telegram long-poll listener
    if register_commands():
        log.info("bot commands registered via setMyCommands")
    else:
        log.error("setMyCommands failed — commands may be missing from the menu")
    bot_thread = threading.Thread(target=bot_poll_loop, name="tg-commands", daemon=True)
    bot_thread.start()

    # low-latency layers
    if CONFIG.get("use_onchain_watcher", False):
        watcher_thread = threading.Thread(target=watcher_loop, name="onchain-watcher", daemon=True)
        watcher_thread.start()

    if CONFIG.get("use_api_watcher", True):
        # HTTP-only fast path: LPAgent API is the realtime source, no RPC.
        # The api watcher thread owns polling; main loop idles as watchdog.
        global STATE
        STATE = state
        api_thread = threading.Thread(target=api_watcher_loop, name="api-watcher", daemon=True)
        api_thread.start()
        while RUNNING:
            time.sleep(1)
        save_state(state)
        log.info("bye")
        return 0

    while RUNNING:
        started = time.time()
        cycle(state)
        elapsed = time.time() - started
        sleep_for = max(5, interval - elapsed)
        for _ in range(int(sleep_for)):
            if not RUNNING:
                break
            time.sleep(1)

    save_state(state)
    log.info("bye")
    return 0


if __name__ == "__main__":
    sys.exit(main())
