#!/usr/bin/env python3
"""
Rugscaningbot v2: free pump.fun scanner with a Telegram menu.
READ-ONLY: never touches a wallet or private key.
Usage:
  python scanner.py                 # run the bot
  python scanner.py --test <MINT>   # run the checks on one coin now
"""
import asyncio
import html
import json
import logging
import os
import signal
import sys
import time
import uuid

import aiohttp
import websockets

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


def _f(name, default):
    return float(os.getenv(name, default))


# ---------- config ----------
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "")
HELIUS_KEY = os.getenv("HELIUS_API_KEY", "")

WAIT_SECONDS = int(_f("WAIT_SECONDS", 600))
CHECK_MIGRATIONS = os.getenv("CHECK_MIGRATIONS", "1") == "1"

MAX_INSIDERS_PCT = _f("MAX_INSIDERS_PCT", 20)
MAX_TOP10_PCT = _f("MAX_TOP10_PCT", 30)
MAX_DEV_COINS = int(_f("MAX_DEV_COINS", 5))
MIN_LIQUIDITY_USD = _f("MIN_LIQUIDITY_USD", 5000)
MIN_MCAP_USD = _f("MIN_MCAP_USD", 8000)

MAX_PENDING = int(_f("MAX_PENDING", 20000))
API_CONCURRENCY = int(_f("API_CONCURRENCY", 3))
STATE_FILE = os.getenv("STATE_FILE", "state.json")

WS_URL = "wss://pumpportal.fun/api/data"
log = logging.getLogger("scanner")

seen = set()
pending = set()
sem = asyncio.Semaphore(API_CONCURRENCY)

state = {"offset": 0, "users": {}, "stats": {"checked": 0, "alerts": 0}}
bot_username = "Rugscaningbot"


# ---------- state ----------
def load_state():
    try:
        with open(STATE_FILE) as f:
            state.update(json.load(f))
    except FileNotFoundError:
        pass
    except Exception:
        log.exception("could not read state file; starting fresh")
    state.setdefault("offset", 0)
    state.setdefault("users", {})
    state.setdefault("stats", {"checked": 0, "alerts": 0})


def save_state():
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, STATE_FILE)


async def saver():
    while True:
        await asyncio.sleep(30)
        try:
            save_state()
        except Exception:
            log.exception("state save failed")


def get_user(chat_id, name=""):
    key = str(chat_id)
    u = state["users"].get(key)
    if u is None:
        u = {
            "name": name[:40],
            "code": uuid.uuid4().hex[:8],
            "active": True,
            "ref_by": None,
            "refs": 0,
            "joined": int(time.time()),
        }
        state["users"][key] = u
        return u, True
    return u, False


def find_by_code(code):
    for cid, u in state["users"].items():
        if u.get("code") == code:
            return cid, u
    return None, None


# ---------- Telegram helpers ----------
async def tg(session, method, **payload):
    if not TG_TOKEN:
        return None
    url = f"https://api.telegram.org/bot{TG_TOKEN}/{method}"
    total = int(payload.get("timeout", 0)) + 20
    try:
        async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=total)) as r:
            return await r.json(content_type=None)
    except Exception as e:  # never log the exception text: it can contain the URL
        log.error("telegram %s failed: %s", method, type(e).__name__)
        return None


async def send(session, chat_id, text, keyboard=None):
    p = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if keyboard:
        p["reply_markup"] = {"inline_keyboard": keyboard}
    data = await tg(session, "sendMessage", **p)
    if data and not data.get("ok") and data.get("error_code") == 403:
        u = state["users"].get(str(chat_id))
        if u:
            u["active"] = False  # user blocked the bot
    return data


async def broadcast(session, text):
    targets = [cid for cid, u in state["users"].items() if u.get("active")]
    if not targets:
        log.warning("no active subscribers; alert not sent")
        return
    for cid in targets:
        await send(session, int(cid), text)
        await asyncio.sleep(0.05)
    state["stats"]["alerts"] += 1


# ---------- bot menu / commands ----------
COMMANDS = [
    ("start", "Subscribe and get started"),
    ("refer", "Get your referral link"),
    ("filters", "See the safety filters"),
    ("status", "Bot status"),
    ("stop", "Pause alerts"),
    ("help", "How the bot works"),
]

MENU_KEYBOARD = [
    [
        {"text": "📋 Filters", "callback_data": "filters"},
        {"text": "🎁 Refer", "callback_data": "refer"},
    ],
    [{"text": "📊 Status", "callback_data": "status"}],
]

DISCLAIMER = "⚠️ A filter, not a signal. Passing the checks does not make a coin safe. Do your own research. Not financial advice."


def filters_text():
    return (
        "📋 <b>Safety filters</b>\n"
        "A coin is skipped if:\n"
        "❌ Mint authority is on\n"
        "❌ Freeze authority is on\n"
        f"❌ Insiders hold more than {MAX_INSIDERS_PCT:g}%\n"
        f"❌ Top 10 holders own more than {MAX_TOP10_PCT:g}%\n"
        f"❌ Dev launched {MAX_DEV_COINS}+ coins before\n"
        f"❌ Liquidity under ${MIN_LIQUIDITY_USD:,.0f}\n"
        f"❌ Market cap under ${MIN_MCAP_USD:,.0f}\n"
        "❌ Flagged as rugged\n\n"
        f"Each coin is checked {WAIT_SECONDS // 60} minutes after launch.\n\n" + DISCLAIMER
    )


def help_text():
    return (
        "🔎 <b>Rugscaningbot</b>\n"
        "I watch every new pump.fun coin live and send an alert only when a coin passes my checks "
        "(RugCheck, DexScreener and the dev wallet's funding source).\n\n"
        "/start subscribe\n/filters see the rules\n/refer invite friends\n"
        "/status bot status\n/stop pause alerts\n\n" + DISCLAIMER
    )


def welcome_text(name):
    who = html.escape(name) if name else "there"
    return (
        f"👋 Welcome, {who}!\n\n"
        "You're subscribed ✅ Alerts arrive here when a new pump.fun coin passes the safety checks.\n\n"
        "Tap a button below or use the menu.\n\n" + DISCLAIMER
    )


async def handle_command(session, chat_id, name, cmd, payload):
    u, is_new = get_user(chat_id, name)

    if cmd == "start":
        if not u.get("active"):
            u["active"] = True
        if is_new and payload.startswith("ref_"):
            rid, ref = find_by_code(payload[4:])
            if ref and rid != str(chat_id):
                u["ref_by"] = ref["code"]
                ref["refs"] += 1
                await send(session, int(rid), f"🎉 Someone joined with your link! Total referrals: {ref['refs']}")
        await send(session, chat_id, welcome_text(name), MENU_KEYBOARD)

    elif cmd == "refer":
        link = f"https://t.me/{bot_username}?start=ref_{u['code']}"
        await send(
            session,
            chat_id,
            "🎁 <b>Invite friends</b>\nShare your personal link:\n"
            f"<code>{link}</code>\n\nFriends joined: <b>{u['refs']}</b>",
        )

    elif cmd == "filters":
        await send(session, chat_id, filters_text())

    elif cmd == "status":
        subs = sum(1 for x in state["users"].values() if x.get("active"))
        st = state["stats"]
        mine = "on ✅" if u.get("active") else "paused ⏸ (send /start to resume)"
        await send(
            session,
            chat_id,
            "📊 <b>Status</b>\nScanner: running\n"
            f"Subscribers: {subs}\nCoins checked: {st['checked']:,}\nAlerts sent: {st['alerts']:,}\n"
            f"Your alerts: {mine}",
        )

    elif cmd == "stop":
        u["active"] = False
        await send(session, chat_id, "⏸ Alerts paused. Send /start any time to resume.")

    elif cmd == "help":
        await send(session, chat_id, help_text(), MENU_KEYBOARD)

    else:
        await send(session, chat_id, "I don't know that command. Try /help.")


async def handle_update(session, upd):
    cb = upd.get("callback_query")
    if cb:
        await tg(session, "answerCallbackQuery", callback_query_id=cb["id"])
        chat_id = cb["message"]["chat"]["id"]
        await handle_command(session, chat_id, cb["from"].get("first_name", ""), cb.get("data", ""), "")
        return
    msg = upd.get("message") or {}
    chat = msg.get("chat") or {}
    if chat.get("type") != "private":
        return
    text = (msg.get("text") or "").strip()
    name = (msg.get("from") or {}).get("first_name", "")
    if not text.startswith("/"):
        await send(session, chat["id"], "Use the menu or send /help.")
        return
    cmd, _, payload = text.partition(" ")
    cmd = cmd[1:].split("@")[0].lower()
    await handle_command(session, chat["id"], name, cmd, payload.strip())


async def poll_loop(session):
    await tg(session, "deleteWebhook")
    while True:
        data = await tg(
            session,
            "getUpdates",
            offset=state["offset"],
            timeout=25,
            allowed_updates=["message", "callback_query"],
        )
        if not data or not data.get("ok"):
            await asyncio.sleep(5)
            continue
        for upd in data.get("result", []):
            state["offset"] = upd["update_id"] + 1
            try:
                await handle_update(session, upd)
            except Exception:
                log.exception("update handling failed")


async def setup_bot(session):
    global bot_username
    me = await tg(session, "getMe")
    if me and me.get("ok"):
        bot_username = me["result"].get("username") or bot_username
    await tg(session, "setMyCommands", commands=[{"command": c, "description": d} for c, d in COMMANDS])
    await tg(session, "setChatMenuButton", menu_button={"type": "commands"})
    if TG_CHAT and TG_CHAT not in state["users"]:
        u, _ = get_user(TG_CHAT, "owner")
        u["active"] = True


# ---------- HTTP helper ----------
async def get_json(session, url, retries=3):
    for attempt in range(retries):
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=20)) as r:
                if r.status == 429:
                    await asyncio.sleep(2 * (attempt + 1))
                    continue
                if r.status != 200:
                    return None
                return await r.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            await asyncio.sleep(1 + attempt)
    return None


# ---------- DexScreener ----------
async def dexscreener(session, mint):
    data = await get_json(session, f"https://api.dexscreener.com/latest/dex/tokens/{mint}")
    pairs = [p for p in ((data or {}).get("pairs") or []) if p.get("chainId") == "solana"]
    if not pairs:
        return None
    best = max(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd") or 0)
    return {
        "price": best.get("priceUsd"),
        "liquidity": (best.get("liquidity") or {}).get("usd") or 0,
        "mcap": best.get("marketCap") or best.get("fdv") or 0,
        "volume24h": (best.get("volume") or {}).get("h24") or 0,
        "url": best.get("url") or f"https://dexscreener.com/solana/{mint}",
    }


# ---------- RugCheck ----------
async def rugcheck(session, mint):
    return await get_json(session, f"https://api.rugcheck.xyz/v1/tokens/{mint}/report")


def evaluate_rugcheck(rep, exclude):
    fails = []
    token = rep.get("token") or {}
    mint_auth = rep.get("mintAuthority") or token.get("mintAuthority")
    freeze_auth = rep.get("freezeAuthority") or token.get("freezeAuthority")

    for m in rep.get("markets") or []:
        for k in ("pubkey", "liquidityA", "liquidityB"):
            if m.get(k):
                exclude.add(m[k])

    holders = [
        h for h in (rep.get("topHolders") or [])
        if h.get("address") not in exclude and h.get("owner") not in exclude
    ]
    top10 = sum(h.get("pct") or 0 for h in holders[:10])
    insiders = sum((h.get("pct") or 0) for h in holders if h.get("insider"))

    creator_tokens = rep.get("creatorTokens")
    dev_coins = len(creator_tokens) if isinstance(creator_tokens, list) else None

    if mint_auth:
        fails.append("mint authority ON")
    if freeze_auth:
        fails.append("freeze authority ON")
    if rep.get("rugged"):
        fails.append("flagged as rugged")
    if insiders > MAX_INSIDERS_PCT:
        fails.append(f"insiders {insiders:.1f}%")
    if top10 > MAX_TOP10_PCT:
        fails.append(f"top10 {top10:.1f}%")
    if dev_coins is not None and dev_coins >= MAX_DEV_COINS:
        fails.append(f"dev launched {dev_coins} coins")

    stats = {
        "insiders": insiders,
        "top10": top10,
        "dev_coins": dev_coins,
        "creator": rep.get("creator"),
    }
    return fails, stats


# ---------- Helius: who funded the dev wallet ----------
async def find_funder(session, dev):
    if not (HELIUS_KEY and dev):
        return None
    url = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_KEY}"

    async def rpc(method, params):
        try:
            async with session.post(
                url,
                json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                timeout=aiohttp.ClientTimeout(total=20),
            ) as r:
                return (await r.json()).get("result")
        except Exception as e:  # noqa
            log.debug("helius %s failed: %s", method, type(e).__name__)
            return None

    sigs = await rpc("getSignaturesForAddress", [dev, {"limit": 1000}])
    if not sigs:
        return None
    tx = await rpc(
        "getTransaction",
        [sigs[-1]["signature"], {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}],
    )
    try:
        for ix in tx["transaction"]["message"]["instructions"]:
            p = ix.get("parsed")
            if (
                ix.get("program") == "system"
                and isinstance(p, dict)
                and p.get("type") in ("transfer", "transferWithSeed")
                and p["info"].get("destination") == dev
            ):
                return p["info"]["source"]
    except (TypeError, KeyError):
        pass
    return None


# ---------- alert card ----------
def build_card(mint, name, symbol, dex, stats, funder):
    dev_coins = stats["dev_coins"]
    return (
        f"🟢 <b>{html.escape(name or '?')}</b> (${html.escape(symbol or '?')})\n"
        f"<b>CA:</b> <code>{mint}</code>\n\n"
        f"💰 MCap: ${dex['mcap']:,.0f}\n"
        f"💧 Liquidity: ${dex['liquidity']:,.0f}\n"
        f"📊 24h Vol: ${dex['volume24h']:,.0f}\n\n"
        f"👥 Insiders: {stats['insiders']:.1f}% | Top10: {stats['top10']:.1f}%\n"
        f"🧑‍💻 Dev coins launched: {dev_coins if dev_coins is not None else 'unknown'}\n"
        f"🏦 Dev funded by: <code>{funder or 'unknown'}</code>\n\n"
        f'<a href="{dex["url"]}">DexScreener</a> · '
        f'<a href="https://rugcheck.xyz/tokens/{mint}">RugCheck</a> · '
        f'<a href="https://pump.fun/coin/{mint}">pump.fun</a>\n'
        f"⚠️ Passed filters only. Not a safety guarantee."
    )


# ---------- core check ----------
async def run_checks(session, mint, name=None, symbol=None, dev=None, exclude=None):
    exclude = set(exclude or [])
    async with sem:
        dex = await dexscreener(session, mint)
        if not dex:
            return False, "no DexScreener pair"
        if dex["liquidity"] < MIN_LIQUIDITY_USD:
            return False, f"liquidity ${dex['liquidity']:,.0f} too low"
        if dex["mcap"] < MIN_MCAP_USD:
            return False, f"mcap ${dex['mcap']:,.0f} too low"

        rep = await rugcheck(session, mint)
        if not rep:
            return False, "no RugCheck report"
        fails, stats = evaluate_rugcheck(rep, exclude)
        if fails:
            return False, ", ".join(fails)

        dev = dev or stats["creator"]
        funder = await find_funder(session, dev)

    return True, build_card(mint, name, symbol, dex, stats, funder)


async def process(session, mint, name, symbol, dev, delay, exclude):
    try:
        if delay:
            await asyncio.sleep(delay)
        if mint in seen:
            return
        state["stats"]["checked"] += 1
        ok, result = await run_checks(session, mint, name, symbol, dev, exclude)
        if ok:
            seen.add(mint)
            log.info("PASS %s %s", symbol, mint)
            await broadcast(session, result)
        else:
            log.debug("skip %s %s: %s", symbol, mint, result)
    except Exception:
        log.exception("error processing %s", mint)
    finally:
        pending.discard(mint)


# ---------- PumpPortal websocket (ONE connection for everything) ----------
async def ws_loop(session):
    backoff = 1
    while True:
        try:
            async with websockets.connect(WS_URL, ping_interval=20, ping_timeout=20) as ws:
                await ws.send(json.dumps({"method": "subscribeNewToken"}))
                if CHECK_MIGRATIONS:
                    await ws.send(json.dumps({"method": "subscribeMigration"}))
                log.info("connected to PumpPortal")
                backoff = 1
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except ValueError:
                        continue
                    mint = msg.get("mint")
                    if not mint or mint in pending or mint in seen:
                        continue
                    if len(pending) >= MAX_PENDING:
                        continue

                    tx_type = msg.get("txType")
                    if tx_type == "create":
                        delay = WAIT_SECONDS
                    elif tx_type == "migrate":
                        delay = 30
                    else:
                        continue

                    exclude = {msg.get("bondingCurveKey"), msg.get("pool")}
                    exclude.discard(None)
                    pending.add(mint)
                    asyncio.create_task(
                        process(
                            session, mint, msg.get("name"), msg.get("symbol"),
                            msg.get("traderPublicKey"), delay, exclude,
                        )
                    )
        except Exception as e:  # noqa
            log.warning("websocket dropped (%s); reconnecting in %ss", type(e).__name__, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)


async def main():
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    load_state()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    async with aiohttp.ClientSession() as session:
        if len(sys.argv) >= 3 and sys.argv[1] == "--test":
            ok, result = await run_checks(session, sys.argv[2])
            print("PASS\n" + result if ok else f"FAIL: {result}")
            return
        if not TG_TOKEN:
            log.warning("TELEGRAM_BOT_TOKEN missing: the bot cannot send or receive messages")
        await setup_bot(session)
        tasks = [
            asyncio.create_task(ws_loop(session)),
            asyncio.create_task(poll_loop(session)),
            asyncio.create_task(saver()),
        ]
        await stop.wait()
        for t in tasks:
            t.cancel()
        save_state()
        log.info("stopped; state saved")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
