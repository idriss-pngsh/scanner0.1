#!/usr/bin/env python3
"""
Free pump.fun memecoin scanner -> Telegram alerts.
READ-ONLY: never touches a wallet or private key.
Usage:
  python scanner.py                 # run the live scanner
  python scanner.py --test <MINT>   # run the checks on one coin now
"""
import asyncio
import json
import logging
import os
import sys

import aiohttp
import websockets

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


def _f(name, default):
    return float(os.getenv(name, default))


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

WS_URL = "wss://pumpportal.fun/api/data"
log = logging.getLogger("scanner")

seen = set()
pending = set()
sem = asyncio.Semaphore(API_CONCURRENCY)


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
            log.debug("helius %s failed: %s", method, e)
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


async def send_telegram(session, text):
    if not (TG_TOKEN and TG_CHAT):
        log.warning("Telegram not configured; would have sent:\n%s", text)
        return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    payload = {
        "chat_id": TG_CHAT,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=20)) as r:
            if r.status != 200:
                log.error("Telegram error %s: %s", r.status, await r.text())
    except Exception as e:  # noqa
        log.error("Telegram send failed: %s", e)


def build_card(mint, name, symbol, dex, stats, funder):
    dev_coins = stats["dev_coins"]
    return (
        f"🟢 <b>{name or '?'}</b> (${symbol or '?'})\n"
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
        ok, result = await run_checks(session, mint, name, symbol, dev, exclude)
        if ok:
            seen.add(mint)
            log.info("PASS %s %s", symbol, mint)
            await send_telegram(session, result)
        else:
            log.debug("skip %s %s: %s", symbol, mint, result)
    except Exception:
        log.exception("error processing %s", mint)
    finally:
        pending.discard(mint)


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
            log.warning("websocket dropped (%s); reconnecting in %ss", e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)


async def main():
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    async with aiohttp.ClientSession() as session:
        if len(sys.argv) >= 3 and sys.argv[1] == "--test":
            ok, result = await run_checks(session, sys.argv[2])
            print("PASS\n" + result if ok else f"FAIL: {result}")
            return
        if not (TG_TOKEN and TG_CHAT):
            log.warning("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing: alerts will only print to the log")
        await ws_loop(session)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
