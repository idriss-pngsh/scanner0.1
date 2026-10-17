"""
Solana memecoin scanner, two-tier alerts (DexScreener + RugCheck -> Telegram).
Env vars: TG_TOKEN, TG_CHAT
"""
import html, json, os, time
import requests

# ---------- SETTINGS ----------
MIN_MCAP = 30_000
MIN_AGE_MIN = 30            # pair must be at least this old (post-migration settle time)
MAX_AGE_DAYS = 30
MIN_LP_BURNED_PCT = 90      # RugCheck lpLockedPct needed to count as "burned"
REQUIRE_DEX_PAID = False    # set True to require a paid DexScreener profile
REALERT_HOLDER_GROWTH = 0.25
MAX_CANDIDATES = 80
STATE_FILE = "state.json"

LIMITS = {
    "A": dict(top10=30, top1=8,  holders=100, dev=5, bundle=12, liq=0.10),
    "B": dict(top10=35, top1=10, holders=80,  dev=7, bundle=15, liq=0.08),
}
# ------------------------------

TG_TOKEN = os.environ.get("TG_TOKEN")
TG_CHAT = os.environ.get("TG_CHAT")
DEX = "https://api.dexscreener.com"
RUG = "https://api.rugcheck.xyz/v1/tokens"
RANK = {"A": 2, "B": 1}
ICON = {"A": "🟢 A-TIER", "B": "🟡 B-TIER"}
BURN_ADDR = {
    "1nc1nerator11111111111111111111111111111111",
    "11111111111111111111111111111111",
    "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1",  # Raydium authority
}


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
        for x in get(DEX + path) or []:
            if x.get("chainId") == "solana" and x.get("tokenAddress"):
                mints.append(x["tokenAddress"])
    return list(dict.fromkeys(mints))[:MAX_CANDIDATES]


def best_pairs(mints):
    out = {}
    for i in range(0, len(mints), 30):
        data = get(f"{DEX}/latest/dex/tokens/{','.join(mints[i:i+30])}") or {}
        for p in data.get("pairs") or []:
            if p.get("chainId") != "solana":
                continue
            m = p["baseToken"]["address"]
            liq = (p.get("liquidity") or {}).get("usd") or 0
            if m not in out or liq > ((out[m].get("liquidity") or {}).get("usd") or 0):
                out[m] = p
    return out


def dex_paid(mint):
    data = get(f"{DEX}/orders/v1/solana/{mint}")
    if not data:
        return False
    orders = data if isinstance(data, list) else data.get("orders", [])
    return any(o.get("type") == "tokenProfile" and o.get("status") == "approved"
               for o in orders)


def pair_precheck(p):
    mcap = p.get("marketCap") or p.get("fdv") or 0
    created = p.get("pairCreatedAt")
    if mcap < MIN_MCAP or not created:
        return False, "mcap/age"
    age_min = (time.time() * 1000 - created) / 60000
    if age_min < MIN_AGE_MIN or age_min > MAX_AGE_DAYS * 1440:
        return False, "age window"
    info = p.get("info") or {}
    if not (info.get("socials") or info.get("websites")):
        return False, "no socials"
    return True, ""


def clean_holders(rep):
    """Top holders excluding LP/pool/burn accounts."""
    excluded = set(BURN_ADDR)
    for mk in rep.get("markets") or []:
        for k in ("pubkey", "liquidityA", "liquidityB"):
            if mk.get(k):
                excluded.add(mk[k])
    return [h for h in rep.get("topHolders") or []
            if h.get("address") not in excluded and h.get("owner") not in excluded]


def lp_burned(rep):
    best = 0
    for mk in rep.get("markets") or []:
        best = max(best, (mk.get("lp") or {}).get("lpLockedPct") or 0)
    return best


def metrics(p, rep):
    hs = clean_holders(rep)
    pcts = sorted([h.get("pct") or 0 for h in hs], reverse=True)
    creator = rep.get("creator")
    dev = sum(h.get("pct") or 0 for h in hs
              if creator and creator in (h.get("owner"), h.get("address")))
    bundle = sum(h.get("pct") or 0 for h in hs if h.get("insider"))
    mcap = p.get("marketCap") or p.get("fdv") or 1
    liq = (p.get("liquidity") or {}).get("usd") or 0
    return dict(
        top1=pcts[0] if pcts else 0,
        top10=sum(pcts[:10]),
        holders=rep.get("totalHolders") or 0,
        dev=dev,
        bundle=bundle,
        liq=liq / mcap,
        lp=lp_burned(rep),
        mcap=mcap,
        liq_usd=liq,
    )


def fails_for(mt, lim):
    f = []
    if mt["top10"] > lim["top10"]: f.append(f"top10 {mt['top10']:.1f}%")
    if mt["top1"] > lim["top1"]: f.append(f"top1 {mt['top1']:.1f}%")
    if mt["holders"] < lim["holders"]: f.append(f"holders {mt['holders']}")
    if mt["dev"] > lim["dev"]: f.append(f"dev {mt['dev']:.1f}%")
    if mt["bundle"] > lim["bundle"]: f.append(f"bundle {mt['bundle']:.1f}%")
    if mt["liq"] < lim["liq"]: f.append(f"liq {mt['liq']*100:.1f}% of mcap")
    return f


def classify(rep, mt):
    """Return (tier or None, fail reasons)."""
    if rep.get("mintAuthority"):
        return None, ["mint authority active"]
    if rep.get("freezeAuthority"):
        return None, ["freeze authority active"]
    if mt["lp"] < MIN_LP_BURNED_PCT:
        return None, [f"LP burned {mt['lp']:.0f}%"]
    if not fails_for(mt, LIMITS["A"]):
        return "A", []
    fb = fails_for(mt, LIMITS["B"])
    if not fb:
        return "B", []
    return None, fb


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


def fmt(m, p, mt, tier, tag):
    sym = html.escape(p["baseToken"]["symbol"])
    age_h = (time.time() * 1000 - p["pairCreatedAt"]) / 3.6e6
    pc = p.get("priceChange") or {}
    return (
        f"<b>{ICON[tier]} | {tag} ${sym}</b>\n"
        f"MC ${mt['mcap']:,.0f} | Liq ${mt['liq_usd']:,.0f} ({mt['liq']*100:.1f}%) | Age {age_h:.1f}h\n"
        f"Holders {mt['holders']} | 1h {pc.get('h1')}% | 24h {pc.get('h24')}%\n"
        f"Top10 {mt['top10']:.1f}% | Top1 {mt['top1']:.1f}% | Dev {mt['dev']:.1f}% | Bundle {mt['bundle']:.1f}%\n"
        f"LP burned {mt['lp']:.0f}% | Mint/Freeze revoked\n"
        f"<code>{m}</code>\n"
        f"https://dexscreener.com/solana/{m}\n"
        f"https://rugcheck.xyz/tokens/{m}"
    )


def main()

    state = json.load(open(STATE_FILE)) if os.path.exists(STATE_FILE) else {}
    mints = candidates()
    pairs = best_pairs(mints)
    print(f"{len(mints)} candidates, {len(pairs)} with pairs")

    for m, p in pairs.items():
        sym = p["baseToken"]["symbol"]
        ok, why = pair_precheck(p)
        if not ok:
            continue
        if REQUIRE_DEX_PAID and not dex_paid(m):
            print(f"REJECT {sym}: not dex paid")
            continue
        rep = get(f"{RUG}/{m}/report")
        time.sleep(0.6)
        if not rep:
            continue
        mt = metrics(p, rep)
        tier, reasons = classify(rep, mt)
        if not tier:
            print(f"REJECT {sym}: {', '.join(reasons)}")
            continue

        prev = state.get(m)
        if not prev:
            tg(fmt(m, p, mt, tier, "NEW"))
        elif RANK[tier] > RANK[prev.get("tier", "B")]:
            tg(fmt(m, p, mt, tier, "⬆️ UPGRADED"))
        elif mt["holders"] >= prev["holders"] * (1 + REALERT_HOLDER_GROWTH):
            tg(fmt(m, p, mt, tier, "🔥 STILL STRONG"))
        else:
            continue
        state[m] = {"t": time.time(), "holders": mt["holders"], "tier": tier}

    cutoff = time.time() - 14 * 86400
    state = {k: v for k, v in state.items() if v["t"] > cutoff}
    json.dump(state, open(STATE_FILE, "w"))


if __name__ == "__main__":
    main()
