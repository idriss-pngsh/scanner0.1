"""
Solana memecoin quality scanner (DexScreener + RugCheck -> Telegram).

Finds tokens with decent distribution and low clustering, not just new launches.
Candidates: DexScreener latest profiles + boosts. Each one must survive an
age window and then pass hard filters and a quality score.

Env vars: TG_TOKEN, TG_CHAT
"""
import json, os, time
import requests

# ---------- SETTINGS (tune these) ----------
MIN_MCAP = 30_000          # market cap floor, no ceiling
MIN_AGE_MIN = 45           # skip anything younger than this (fresh rugs die here)
MAX_AGE_DAYS = 30
MIN_LIQ_RATIO = 0.05       # liquidity / mcap
MIN_LIQ_USD = 8_000
MAX_TOP1_PCT = 6.0         # biggest single holder
MAX_TOP10_PCT = 35.0       # top 10 holders combined
MIN_HOLDERS = 150
MIN_SCORE = 65             # 0-100
REALERT_HOLDER_GROWTH = 0.25   # follow-up alert if holders grow 25% after alert
MAX_CANDIDATES = 60
STATE_FILE = "state.json"
# --------------------------------------------

TG_TOKEN = os.environ.get("TG_TOKEN")
TG_CHAT = os.environ.get("TG_CHAT")
DEX = "https://api.dexscreener.com"
RUG = "https://api.rugcheck.xyz/v1/tokens"


def get(url, **kw):
    try:
        r = requests.get(url, timeout=20, **kw)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        print("req fail", url, e)
    return None


def candidates():
    mints = []
    for path in ("/token-profiles/latest/v1", "/token-boosts/latest/v1",
                 "/token-boosts/top/v1"):
        data = get(DEX + path) or []
        for x in data:
            if x.get("chainId") == "solana" and x.get("tokenAddress"):
                mints.append(x["tokenAddress"])
    return list(dict.fromkeys(mints))[:MAX_CANDIDATES]


def best_pairs(mints):
    """Return {mint: best pair by liquidity}, 30 addresses per call."""
    out = {}
    for i in range(0, len(mints), 30):
        chunk = mints[i:i + 30]
        data = get(f"{DEX}/latest/dex/tokens/{','.join(chunk)}") or {}
        for p in data.get("pairs") or []:
            if p.get("chainId") != "solana":
                continue
            m = p["baseToken"]["address"]
            liq = (p.get("liquidity") or {}).get("usd") or 0
            if m not in out or liq > ((out[m].get("liquidity") or {}).get("usd") or 0):
                out[m] = p
    return out


def pair_checks(p):
    """Hard filters from DexScreener data. Returns (ok, reason)."""
    mcap = p.get("marketCap") or p.get("fdv") or 0
    liq = (p.get("liquidity") or {}).get("usd") or 0
    created = p.get("pairCreatedAt")
    if mcap < MIN_MCAP:
        return False, "mcap"
    if not created:
        return False, "no age"
    age_min = (time.time() * 1000 - created) / 60000
    if age_min < MIN_AGE_MIN or age_min > MAX_AGE_DAYS * 1440:
        return False, "age"
    if liq < MIN_LIQ_USD or liq / mcap < MIN_LIQ_RATIO:
        return False, "liquidity"
    return True, ""


def rug_report(mint):
    return get(f"{RUG}/{mint}/report")


def holder_stats(rep):
    holders = rep.get("topHolders") or []
    pcts = sorted([h.get("pct") or 0 for h in holders], reverse=True)
    top1 = pcts[0] if pcts else 0
    top10 = sum(pcts[:10])
    insiders = sum(1 for h in holders if h.get("insider"))
    return top1, top10, insiders


def lp_locked(rep):
    best = 0
    for m in rep.get("markets") or []:
        lp = m.get("lp") or {}
        best = max(best, lp.get("lpLockedPct") or 0)
    return best


def evaluate(p, rep):
    """Return (score, reasons, failed_hard)."""
    if rep.get("mintAuthority") or rep.get("freezeAuthority"):
        return 0, ["mint/freeze authority active"], True
    if rep.get("graphInsidersDetected") or rep.get("insiderNetworks"):
        return 0, ["insider network detected"], True
    top1, top10, insiders = holder_stats(rep)
    holders = rep.get("totalHolders") or 0
    if top1 > MAX_TOP1_PCT or top10 > MAX_TOP10_PCT:
        return 0, [f"concentrated top1 {top1:.1f}% top10 {top10:.1f}%"], True
    if holders < MIN_HOLDERS:
        return 0, ["few holders"], True

    score, why = 0, []
    # distribution (35)
    d = 35 * (1 - top10 / MAX_TOP10_PCT)
    score += max(0, d)
    why.append(f"top1 {top1:.1f}% / top10 {top10:.1f}%")
    # clustering (15)
    if insiders == 0:
        score += 15
    else:
        score += max(0, 15 - insiders * 5)
        why.append(f"{insiders} insider-flagged holders")
    # holders (10)
    score += min(10, holders / 100)
    # lp lock/burn (10)
    lock = lp_locked(rep)
    score += 10 * min(1, lock / 90)
    why.append(f"LP locked/burned {lock:.0f}%")
    # buy pressure (15)
    tx = p.get("txns") or {}
    for key, pts in (("h1", 7), ("h6", 8)):
        b, s = (tx.get(key) or {}).get("buys", 0), (tx.get(key) or {}).get("sells", 0)
        if b + s >= 20:
            score += pts * min(1, (b / max(s, 1)) / 1.2)
    # price health (15)
    pc = p.get("priceChange") or {}
    h6, h24 = pc.get("h6") or 0, pc.get("h24") or 0
    if h6 > -25:
        score += 8
    if h24 > -50:
        score += 7
    if h24 > 400:
        score -= 15
        why.append("already pumped hard 24h")
    # liquidity depth bonus
    mcap = p.get("marketCap") or p.get("fdv") or 1
    liq = (p.get("liquidity") or {}).get("usd") or 0
    score += min(0, 0) + (5 if liq / mcap > 0.1 else 0)
    return round(min(score, 100)), why, False


def tg(msg):
    if not (TG_TOKEN and TG_CHAT):
        print(msg)
        return
    requests.post(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
        json={"chat_id": TG_CHAT, "text": msg, "parse_mode": "HTML",
              "disable_web_page_preview": True},
        timeout=20,
    )


def fmt(m, p, rep, score, why, tag):
    base = p["baseToken"]
    mcap = p.get("marketCap") or p.get("fdv") or 0
    liq = (p.get("liquidity") or {}).get("usd") or 0
    age_h = (time.time() * 1000 - p["pairCreatedAt"]) / 3.6e6
    pc = p.get("priceChange") or {}
    return (
        f"<b>{tag} ${base['symbol']}</b>  score {score}/100\n"
        f"MC ${mcap:,.0f} | Liq ${liq:,.0f} | Age {age_h:.1f}h\n"
        f"Holders {rep.get('totalHolders')} | 1h {pc.get('h1')}% | 24h {pc.get('h24')}%\n"
        f"{' | '.join(why)}\n"
        f"<code>{m}</code>\n"
        f"https://dexscreener.com/solana/{m}\n"
        f"https://rugcheck.xyz/tokens/{m}"
    )


def main():
    state = json.load(open(STATE_FILE)) if os.path.exists(STATE_FILE) else {}
    mints = candidates()
    pairs = best_pairs(mints)
    print(f"{len(mints)} candidates, {len(pairs)} with pairs")

    for m, p in pairs.items():
        ok, _ = pair_checks(p)
        if not ok:
            continue
        rep = rug_report(m)
        time.sleep(0.6)  # be nice to RugCheck
        if not rep:
            continue
        score, why, failed = evaluate(p, rep)
        if failed or score < MIN_SCORE:
            continue

        holders = rep.get("totalHolders") or 0
        prev = state.get(m)
        if not prev:
            tg(fmt(m, p, rep, score, why, "NEW"))
            state[m] = {"t": time.time(), "holders": holders, "score": score}
        elif holders >= prev["holders"] * (1 + REALERT_HOLDER_GROWTH):
            tg(fmt(m, p, rep, score, why, "STILL STRONG"))
            state[m].update({"holders": holders, "score": score, "t": time.time()})

    # drop stale state (> 14 days)
    cutoff = time.time() - 14 * 86400
    state = {k: v for k, v in state.items() if v["t"] > cutoff}
    json.dump(state, open(STATE_FILE, "w"))


if __name__ == "__main__":
    main()
