"""
Halal breaking-news watcher.

Polls fast news sources every few seconds, keeps only news about stocks on the
halal list (SP Funds SPUS Shariah holdings + your own stocks, minus BDS
boycott targets), and pushes a phone alert via ntfy within ~1 minute.

Sources:
  1. Press-release wires (PR Newswire, GlobeNewswire, Business Wire) - official
     deal/guidance announcements, matched by "(NASDAQ: PTC)" style tags.
  2. Google News search RSS - "reportedly in talks", takeover, surge/plunge
     headlines from Reuters/Bloomberg/CNBC etc, matched by company name.
  3. Price moves - any halal stock moving >= MOVE_PCT% vs previous close,
     with the latest headline attached as the reason.

Env vars:
  NTFY_TOPIC   (required) secret ntfy topic name your phone subscribes to
  NTFY_SERVER  default https://ntfy.sh
  MOVE_PCT     default 8
  PORT         set by Render
"""
import csv
import html
import io
import json
import os
import re
import threading
import time
import traceback
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from zoneinfo import ZoneInfo

UK = ZoneInfo("Europe/London")
HERE = os.path.dirname(os.path.abspath(__file__))
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
MOVE_PCT = float(os.environ.get("MOVE_PCT", "3"))
# Alert when a stock first crosses MOVE_PCT, then again at each bigger step.
MOVE_STEPS = sorted({MOVE_PCT, 8.0, 15.0, 25.0, 40.0, 60.0})
MOVE_STEPS = [x for x in MOVE_STEPS if x >= MOVE_PCT]
EXTRA_SCREENED = int(os.environ.get("EXTRA_SCREENED", "300"))
SELF_URL = os.environ.get("RENDER_EXTERNAL_URL", "")
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")

# Boycott / BDS exclusions (also applied to SPUS refreshes).
BDS_EXCLUDE = {
    "MSFT", "GOOGL", "GOOG", "CSCO", "PEP", "BKNG", "EXPE", "ABNB", "AMZN",
    "INTC", "DELL", "HPQ", "HPE", "CVX", "DIS", "KO", "MCD", "YUM", "QSR",
    "PZZA", "WIX", "TEVA", "LMT", "RTX", "BA", "NOC", "GD", "LHX", "ESLT",
    "CAT", "HON", "PLTR", "RMAX", "SIEGY",
    # Israeli companies / Israel-headquartered
    "CHKP", "CYBR", "NICE", "MNDY", "WIX", "SEDG", "ICL", "TSEM", "GLBE",
    "FVRR", "NVMI", "CAMT", "INMD", "KMDA", "ORA", "TEVA", "ESLT", "CEVA",
    "NNOX", "PERI", "SPNS", "SMWB", "RDWR", "ALLT", "GILT", "AUDC", "ELBIT",
    "ZIM", "TARO", "ITRN", "OPRX", "NGMS", "INVZ", "MBLY", "PAYO", "LMND",
    "RSKD", "TBLA", "CGNT", "SILC", "NVCR", "NYMX",
}

# Your own picks - always watched (Intel left out: BDS priority target).
MY_PICKS = {
    "ASML": "ASML Holding NV", "TSM": "Taiwan Semiconductor Manufacturing",
    "CELH": "Celsius Holdings Inc", "RIOT": "Riot Platforms Inc",
    "AAOI": "Applied Optoelectronics Inc", "MRVL": "Marvell Technology Inc",
    "BYDDY": "BYD Co Ltd", "HIMS": "Hims & Hers Health Inc",
    "SKHY": "SK Hynix Inc",
}
for _t in os.environ.get("MY_PICKS", "").split(","):
    if ":" in _t:
        _k, _v = _t.split(":", 1)
        MY_PICKS[_k.strip().upper()] = _v.strip()

# Known non-compliant names the sector data misses (tobacco, gambling,
# cruise/alcohol, payments/interest, asset managers, defence, health
# insurers, pork/alcohol food distributors, entertainment, high-debt telecom).
EXCLUDE_TICKERS = {
    "MO", "PM", "BTI", "IMBBY", "CCL", "RCL", "NCLH", "VIK", "FLUT", "DKNG",
    "LVS", "WYNN", "MGM", "CZR", "LYV", "NFLX", "TKO", "WMG", "SONY", "SPOT",
    "WBD", "PARA", "FOXA", "FOX", "CMCSA", "CHTR", "MA", "V", "PYPL", "FIS",
    "FISV", "FI", "GPN", "CPAY", "XYZ", "SQ", "SSNC", "MSCI", "SPGI", "MCO",
    "BAM", "BN", "BX", "KKR", "APO", "FTAI", "CACI", "LDOS", "HWM", "TDY",
    "WWD", "GE", "HII", "TXT", "KTOS", "AVAV", "SAIC", "BWXT", "SPCX", "KHC",
    "SYY", "USFD", "PFGC", "CASY", "TSN", "HRL", "SFD", "CVS", "CI", "ELV",
    "HUM", "CNC", "MOH", "UNH", "VZ", "T", "TMUS", "BCE", "TU", "VOD", "STZ",
    "BUD", "DEO", "TAP", "SAM", "BF.B", "ABEV", "CCU", "AFRM", "SOFI", "UPST",
    "COIN", "HOOD", "IBKR", "SCHW", "ICE", "CME", "NDAQ", "CBOE", "MSGS",
    "MSGE", "SUNB", "DUKB", "BNJ", "AQNB",
}

# Industries excluded by Shariah business screens (used for the extra
# sector-screened list only; ETF lists are already screened by Shariah boards).
EXCLUDE_INDUSTRY_RE = re.compile(
    r"bank|insur|financ|invest|broker|lending|credit|mortgage|savings|trust|"
    r"real estate investment|reit|blank check|casino|gaming|beverages|brew|"
    r"distill|wine|tobacco|cigar|military|ordnance|defen[cs]e|aerospace|"
    r"movie|entertainment|broadcast|pay television|hotel|resort|restaurant|"
    r"meat|poultry|adult|marijuana|cannabis|exchange|electric utilit|"
    r"power generation|gas distribution|water supply|cruise|"
    r"packaged foods|food distributors|telecommunications services", re.I)

# Company names too generic to match on their own in headlines.
NAME_OVERRIDES = {
    "TGT": ["Target Corp", "Target's sales", "Target shares", "Target stock"],
    "FLEX": ["Flex Ltd", "Flex shares", "Flex stock"],
    "WAT": ["Waters Corp", "Waters shares", "Waters stock"],
    "DOV": ["Dover Corp", "Dover shares", "Dover stock"],
    "CARR": ["Carrier Global", "Carrier shares", "Carrier stock"],
    "ON": ["ON Semiconductor", "onsemi"],
    "A": ["Agilent"],
    "P": ["Everpure"],
    "Q": ["Qnity"],
    "IT": ["Gartner"],
    "NOW": ["ServiceNow"],
    "BE": ["Bloom Energy"],
    "COR": ["Cencora"],
    "FIX": ["Comfort Systems"],
    "TECH": ["Bio-Techne"],
    "FAST": ["Fastenal"],
    "ALL": [],
    "MMM": ["3M"],
    "HD": ["Home Depot"],
    "LOW": ["Lowe's"],
    "GLW": ["Corning"],
    "TSM": ["TSMC", "Taiwan Semiconductor"],
    "ASML": ["ASML"],
    "PTC": ["PTC Inc", "PTC shares", "PTC stock", "of PTC", "PTC's"],
    "EL": ["Estee Lauder", "Estée Lauder"],
    "CRH": ["CRH"],
    "UPS": ["UPS", "United Parcel"],
    "IBM": ["IBM"],
    "AMD": ["AMD", "Advanced Micro Devices"],
    "TT": ["Trane"],
    "WM": ["Waste Management"],
    "CL": ["Colgate"],
    "PG": ["Procter & Gamble", "P&G"],
    "JNJ": ["Johnson & Johnson", "J&J"],
    "LLY": ["Eli Lilly", "Lilly"],
    "MRK": ["Merck"],
    "SLB": ["SLB", "Schlumberger"],
    "GE": [],
    "GEV": ["GE Vernova"],
    "GEHC": ["GE HealthCare"],
    "DD": ["DuPont"],
    "CF": ["CF Industries"],
    "RL": ["Ralph Lauren"],
    "NKE": ["Nike"],
    "ROL": ["Rollins Inc"],
    "OTIS": ["Otis Worldwide", "Otis shares", "Otis stock"],
    "COO": ["Cooper Cos", "CooperCompanies"],
    "COHR": ["Coherent Corp", "Coherent shares", "Coherent stock"],
    "KLAC": ["KLA"],
    "CSX": ["CSX"],
    "CDW": ["CDW"],
    "FFIV": ["F5 Inc", "F5 Networks", "F5 shares"],
    "EOG": ["EOG Resources", "EOG"],
    "CELH": ["Celsius Holdings", "Celsius shares", "Celsius stock", "Celsius energy drink"],
    "AAOI": ["Applied Optoelectronics", "AAOI"],
    "RIOT": ["Riot Platforms"],
    "BYDDY": ["BYD"],
    "HIMS": ["Hims & Hers", "Hims and Hers", "HIMS"],
    "SKHY": ["SK Hynix", "SK hynix"],
    "MRVL": ["Marvell"],
}

COMMON_WORDS = {
    "target", "block", "arm", "match", "snap", "square", "gap", "visa", "unity",
    "toast", "dropbox", "zoom", "shift", "ford", "best buy", "intuit", "apple",
    "oracle", "monster", "flex", "waters", "dover", "carrier", "coherent",
    "ross", "nucor", "ball", "crown", "graham", "fair", "clear", "global",
    "general", "american", "united", "national", "first", "pool", "sun",
    "align", "insulet", "coupang", "kenvue", "everest", "progressive",
    "public", "premier", "core", "lumen", "rambus", "affirm", "elastic",
}

SUFFIX_RE = re.compile(
    r"\b(american depositary shares?|depositary shares?|ordinary shares?|"
    r"common stock|class [a-c]|sponsored adr|adr|ads|each representing.*$|"
    r"inc|corp|corporation|co|cos|company|plc|ltd|limited|nv|n\.v|s\.a|sa|ag|se|"
    r"holdings?|group|the|international|technologies|technology)\b\.?|"
    r"/the|/de|/md|/ny|[,.()]",
    re.I)

CATALYST_RE = re.compile(
    r"definitive agreement|acquir|acquisition|takeover|take-over|buyout|buy-out|merger|merge |"
    r"to be bought|to buy |agrees? to buy|in talks|nears? deal|close to (a )?deal|"
    r"bid for|offer for|tender offer|go(ing)? private|strategic alternatives|"
    r"explor(es|ing) (a )?sale|activist|stake in|"
    r"soar|surge|jump|skyrocket|rocket|rall(y|ies)|plunge|tumble|sink|crater|"
    r"plummet|slump|nosedive|"
    r"raises? (its )?(guidance|outlook|forecast)|cuts? (its )?(guidance|outlook|forecast)|"
    r"lowers? (its )?(guidance|outlook|forecast)|preliminary|pre-announce|"
    r"beats? estimates|misses estimates|record revenue|profit warning|"
    r"fda approv|approval|clears|wins? .{0,30}contract|awarded|billion deal|"
    r"partnership with|downgrade|upgrade|halted|investigation|probe|recall|"
    r"bankrupt|resign|steps down|ceo departs", re.I)

TAKEOVER_RE = re.compile(
    r"acquir|takeover|take-over|buyout|merger|to be bought|to buy|in talks|"
    r"nears? deal|bid for|offer for|tender offer|go(ing)? private|explor\w+ (a )?sale",
    re.I)

PR_FEEDS = [
    "https://www.prnewswire.com/rss/news-releases-list.rss",
    "https://www.globenewswire.com/RssFeed/orgclass/1/feedTitle/GlobeNewswire%20-%20News%20about%20Public%20Companies",
]

GN_QUERIES = [
    "takeover OR acquire OR acquisition OR buyout OR \"in talks\" stock when:1h",
    "shares soar OR surge OR jump OR skyrocket when:1h",
    "shares plunge OR tumble OR sink OR crater when:1h",
    "raises guidance OR cuts guidance OR preliminary results shares when:1h",
    "\"premarket\" movers stocks when:1h",
    "FDA approval OR contract win shares when:1h",
]

state = {
    "started": datetime.now(timezone.utc).isoformat(),
    "last_cycle": None,
    "sources": {},
    "alerts_sent": [],
    "universe_size": 0,
    "universe_source": "",
}
seen = set()
moved_today = {}  # ticker -> (date, highest bucket alerted)
lock = threading.Lock()


# ---------------------------------------------------------------- utilities
def log(*a):
    print(datetime.now(UK).strftime("%H:%M:%S"), *a, flush=True)


def fetch(url, timeout=15, headers=None):
    h = {"User-Agent": UA, "Accept": "*/*"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def mark(source, ok, info=""):
    state["sources"][source] = {
        "ok": ok, "info": str(info)[:200],
        "at": datetime.now(UK).strftime("%H:%M:%S"),
    }


def clean(s):
    s = html.unescape(re.sub(r"<[^>]+>", " ", s or ""))
    return re.sub(r"\s+", " ", s).strip()


def parse_date(s):
    if not s:
        return None
    try:
        d = parsedate_to_datetime(s)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            return None


def parse_rss(raw):
    items = []
    root = ET.fromstring(raw)
    for it in root.iter("item"):
        g = lambda tag: (it.findtext(tag) or "")
        items.append({
            "title": clean(g("title")),
            "link": g("link").strip(),
            "desc": clean(g("description"))[:600],
            "date": parse_date(g("pubDate")),
            "guid": (g("guid") or g("link")).strip(),
            "source": clean(g("source")),
        })
    return items


# ----------------------------------------------------------------- universe
UNIVERSE = {}
NAME_PATTERNS = []  # (regex, ticker)


def short_name(name):
    n = SUFFIX_RE.sub(" ", name)
    return re.sub(r"\s+", " ", n).strip()


def build_patterns():
    pats = []
    for t, name in UNIVERSE.items():
        if t in NAME_OVERRIDES:
            aliases, flags = NAME_OVERRIDES[t], re.I
        else:
            sn = short_name(name)
            if sn.isupper() and len(sn) > 4:
                sn = sn.title()
            if len(sn) < 3:
                continue
            if sn.lower() in COMMON_WORDS:
                aliases = [f"{sn} Inc", f"{sn} shares", f"{sn} stock", f"{sn}'s shares"]
            else:
                aliases = [sn]
            flags = 0
        for a in aliases:
            strict = a.isupper() and len(a) <= 4
            pats.append((re.compile(r"(?<![\w-])" + re.escape(a) + r"(?![\w-])",
                                    0 if strict else flags), t))
    NAME_PATTERNS[:] = pats


TIER = {}  # ticker -> "etf:SPUS" / "screened" / "yours"


def parse_holdings_csv(raw):
    out = {}
    rows = list(csv.reader(io.StringIO(raw)))
    hdr_i = None
    for i, r in enumerate(rows[:15]):
        low = [c.lower().strip() for c in r]
        if any(c in ("ticker", "stockticker", "symbol", "ticker symbol") for c in low):
            hdr_i = i
            break
    if hdr_i is None:
        return out
    low = [c.lower().strip() for c in rows[hdr_i]]
    ti = next(i for i, c in enumerate(low) if c in ("ticker", "stockticker", "symbol", "ticker symbol"))
    ni = next((i for i, c in enumerate(low) if c in ("securityname", "name", "security name",
                                                      "company", "description", "holding", "security")), None)
    for r in rows[hdr_i + 1:]:
        if len(r) <= ti:
            continue
        t = r[ti].strip().upper().replace("/", ".")
        n = r[ni].strip() if ni is not None and len(r) > ni else t
        if re.fullmatch(r"[A-Z]{1,5}(\.[A-Z])?", t):
            out[t] = n or t
    return out


ETF_SOURCES = {
    "SPUS": "https://www.sp-funds.com/wp-content/uploads/data/TidalFG_Holdings_SPUS.csv",
    "SPTE": "https://www.sp-funds.com/wp-content/uploads/data/TidalFG_Holdings_SPTE.csv",
    "HLAL": "https://docs.google.com/spreadsheets/d/1UC1Bk67bGuYsos_i8y_HQpNoHpVHAvqf71MbgrafJOQ/export?format=csv&gid=0",
}


def nasdaq_screened(exclude, limit):
    """Largest US-listed stocks passing Shariah business-sector screens
    (financial ratios NOT checked) and not Israeli."""
    raw = fetch("https://api.nasdaq.com/api/screener/stocks?tableonly=true&limit=10000&download=true",
                timeout=40)
    rows = json.loads(raw)["data"]["rows"]
    cands = []
    for r in rows:
        sym = (r.get("symbol") or "").strip().upper()
        if (not re.fullmatch(r"[A-Z]{1,5}", sym) or sym in exclude
                or sym in BDS_EXCLUDE or sym in EXCLUDE_TICKERS):
            continue
        if (r.get("country") or "").strip().lower() == "israel":
            continue
        ind = f"{r.get('sector', '')} {r.get('industry', '')}"
        if not r.get("industry") or EXCLUDE_INDUSTRY_RE.search(ind):
            continue
        name = r.get("name") or sym
        if re.search(r"warrant|right|unit|preferred|notes? due|note|debenture|"
                     r"subordinated|%|depositary share.*preferred", name, re.I):
            continue
        try:
            mc = float(r.get("marketCap") or 0)
        except ValueError:
            mc = 0
        if mc < 2e9:
            continue
        cands.append((mc, sym, name))
    cands.sort(reverse=True)
    return {sym: name for _, sym, name in cands[:limit]}


def load_universe():
    uni, tier, counts = {}, {}, {}
    for etf, url in ETF_SOURCES.items():
        try:
            got = parse_holdings_csv(fetch(url, timeout=30).decode("utf-8", "ignore"))
            counts[etf] = len(got)
            for t, n in got.items():
                if t not in uni:
                    uni[t], tier[t] = n, f"etf:{etf}"
        except Exception as e:
            counts[etf] = f"failed: {e}"
            log(f"{etf} holdings failed:", e)
    if len(uni) < 100:  # fall back to bundled SPUS list
        for t, n in json.load(open(os.path.join(HERE, "universe.json"))).items():
            uni.setdefault(t, n)
            tier.setdefault(t, "etf:SPUS (bundled)")
    for t in BDS_EXCLUDE:
        uni.pop(t, None)
        tier.pop(t, None)
    try:
        extra = nasdaq_screened(set(uni) | set(MY_PICKS), EXTRA_SCREENED)
        counts["sector-screened"] = len(extra)
        for t, n in extra.items():
            uni[t], tier[t] = n, "screened"
    except Exception as e:
        counts["sector-screened"] = f"failed: {e}"
        log("nasdaq screen failed:", e)
    for t, n in MY_PICKS.items():
        if t not in BDS_EXCLUDE:
            uni[t] = n
            tier[t] = "yours" if t not in tier or tier[t] == "screened" else tier[t] + "+yours"
    UNIVERSE.clear()
    UNIVERSE.update(uni)
    TIER.clear()
    TIER.update(tier)
    build_patterns()
    state["universe_size"] = len(UNIVERSE)
    state["universe_source"] = counts
    log(f"Universe: {len(UNIVERSE)} stocks {counts}")


def tier_note(t):
    tr = TIER.get(t, "")
    if tr.startswith("etf:"):
        return f"✅ In Shariah ETF ({tr[4:].replace('+yours', '')})"
    if tr == "screened":
        return "⚠️ Sector-screened only - check debt ratios on Musaffa/Zoya"
    return "⭐ Your pick - check Musaffa/Zoya"


def match_tickers(text):
    found = []
    # Exchange tags like (NASDAQ: PTC) / NYSE:PTC
    for m in re.finditer(r"(?:NASDAQ|Nasdaq|NYSE|NYSE American)\s*(?:GS|GM|CM)?\s*:\s*"
                         r"([A-Z]{1,5}(?:\.[A-Z])?)", text):
        if m.group(1) in UNIVERSE and m.group(1) not in found:
            found.append(m.group(1))
    for m in re.finditer(r"\$([A-Z]{1,5})\b", text):
        if m.group(1) in UNIVERSE and m.group(1) not in found:
            found.append(m.group(1))
    for rx, t in NAME_PATTERNS:
        if t not in found and rx.search(text):
            found.append(t)
    return found


# ------------------------------------------------------------------- alerts
def tg_chat_ids():
    """Comma-separated TELEGRAM_CHAT_ID (personal and/or group chats).
    Falls back to auto-discovering the most recent chat if none is set."""
    global TG_CHAT
    if TG_CHAT or not TG_TOKEN:
        return [c.strip() for c in TG_CHAT.split(",") if c.strip()]
    for c in tg_list_chats():
        TG_CHAT = c["id"]
        log("Telegram chat id discovered:", TG_CHAT)
        break
    return [TG_CHAT] if TG_CHAT else []


def tg_list_chats():
    """Every chat the bot has seen recently (newest first): private chats and groups."""
    chats, seen_ids = [], set()
    try:
        d = json.loads(fetch(f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates"
                             "?allowed_updates=%5B%22message%22%2C%22my_chat_member%22%5D"))
        for u in reversed(d.get("result", [])):
            m = u.get("message") or u.get("my_chat_member") or {}
            c = m.get("chat") or {}
            if c.get("id") and str(c["id"]) not in seen_ids:
                seen_ids.add(str(c["id"]))
                chats.append({"id": str(c["id"]), "type": c.get("type"),
                              "title": c.get("title") or c.get("first_name")})
    except Exception as e:
        log("getUpdates failed:", e)
    return chats


def push_telegram(title, body, url=None):
    chats = tg_chat_ids()
    if not chats:
        state["last_push_error"] = "Telegram: no chat yet - send /start to your bot"
        return False
    text = f"{title}\n{body}" + (f"\n{url}" if url else "")
    any_ok = False
    for chat in chats:
        data = json.dumps({"chat_id": chat, "text": text[:4000],
                           "disable_web_page_preview": True}).encode()
        for attempt in range(3):
            try:
                req = urllib.request.Request(
                    f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", data=data,
                    headers={"Content-Type": "application/json"}, method="POST")
                urllib.request.urlopen(req, timeout=15).read()
                any_ok = True
                break
            except Exception as e:
                state["last_push_error"] = f"Telegram {chat}: {e}"
                log("telegram push failed:", chat, e)
                time.sleep(3)
    return any_ok


def push(title, body, url=None, priority=4, tags="zap"):
    if TG_TOKEN:
        ok = push_telegram(title, body, url)
        if ok:
            with lock:
                state["alerts_sent"].insert(0, {
                    "at": datetime.now(UK).strftime("%a %H:%M"), "title": title})
                del state["alerts_sent"][30:]
            log("PUSHED (telegram):", title)
            return True
    if not NTFY_TOPIC:
        log("NTFY_TOPIC not set; would push:", title, body)
        return False
    payload = {"topic": NTFY_TOPIC, "title": title[:200], "message": body,
               "priority": priority, "tags": [tags]}
    if url:
        payload["click"] = url
    req = urllib.request.Request(NTFY_SERVER + "/",
                                 data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": UA},
                                 method="POST")
    ok = False
    for attempt in range(4):
        try:
            urllib.request.urlopen(req, timeout=15).read()
            ok = True
            break
        except Exception as e:
            eb = ""
            if hasattr(e, "read"):
                try:
                    eb = e.read()[:200].decode("utf-8", "ignore")
                except Exception:
                    pass
            log(f"push attempt {attempt+1} failed:", e, eb)
            state["last_push_error"] = f"{e} {eb}"[:300]
            if "quota" in eb:
                break
            time.sleep([3, 10, 30, 0][attempt])
    if ok:
        with lock:
            state["alerts_sent"].insert(0, {
                "at": datetime.now(UK).strftime("%a %H:%M"), "title": title})
            del state["alerts_sent"][30:]
        log("PUSHED:", title)
    return ok


def quote_line(t):
    q = last_quotes.get(t)
    if not q:
        return ""
    return f"{t} {q['pct']:+.1f}% (${q['price']:.2f})"


def note_for(text):
    if TAKEOVER_RE.search(text):
        if re.search(r"report|talks|sources|people familiar|consider|explor", text, re.I):
            return ("Takeover report, not confirmed yet - the deal could fall "
                    "through. Most of the jump usually happens straight away.")
        return ("Takeover/deal news - the stock usually trades just under the "
                "offer price, so most of the gain has likely happened.")
    return ""


def alert_news(tickers, item, kind):
    text = f"{item['title']} {item['desc']}"
    for t in tickers[:2]:
        key = ("news", t, re.sub(r"\W+", "", item["title"].lower())[:60])
        if key in seen:
            continue
        seen.add(key)
        q = last_quotes.get(t)
        move = f" {q['pct']:+.1f}%" if q else ""
        icon = "📉" if q and q["pct"] < 0 else "⚡"
        title = f"{icon} {t}{move} - {UNIVERSE.get(t, '')[:40]}"
        body = item["title"]
        n = note_for(text)
        if n:
            body += f"\n{n}"
        src = item.get("source") or kind
        body += f"\n({src}) {tier_note(t)}. Not advice."
        push(title, body, item["link"], priority=5 if TAKEOVER_RE.search(text) else 4)


# ------------------------------------------------------------------ sources
def fresh(item, minutes=90):
    d = item["date"]
    return d is None or d > datetime.now(timezone.utc) - timedelta(minutes=minutes)


backoff = {}


def poll_pr(initial=False):
    for url in PR_FEEDS:
        name = "PR:" + urllib.parse.urlparse(url).netloc
        if time.time() < backoff.get(name, 0):
            continue
        try:
            items = parse_rss(fetch(url, timeout=12))
            mark(name, True, f"{len(items)} items")
            backoff.pop(name, None)
        except Exception as e:
            mark(name, False, f"{e} (retry in 5 min)")
            backoff[name] = time.time() + 300
            continue
        for it in items:
            gid = ("pr", it["guid"])
            if gid in seen:
                continue
            seen.add(gid)
            if initial or not fresh(it, 120):
                continue
            text = f"{it['title']} {it['desc']}"
            tickers = [t for t in match_tickers(text)]
            if tickers and CATALYST_RE.search(it["title"]):
                it["source"] = "Press release"
                alert_news(tickers, it, "press release")


gn_index = [0]


def poll_google(initial=False, queries=None):
    qs = queries or [GN_QUERIES[gn_index[0] % len(GN_QUERIES)]]
    gn_index[0] += 1
    for q in qs:
        url = ("https://news.google.com/rss/search?hl=en-US&gl=US&ceid=US:en&q="
               + urllib.parse.quote(q))
        try:
            items = parse_rss(fetch(url))
            mark("GoogleNews", True, f"{len(items)} items for: {q[:40]}")
        except Exception as e:
            mark("GoogleNews", False, e)
            continue
        for it in items:
            gid = ("gn", re.sub(r"\W+", "", it["title"].lower())[:80])
            if gid in seen:
                continue
            seen.add(gid)
            if initial or not fresh(it, 75):
                continue
            # Google appends " - Source" to titles; match on the headline part.
            head = re.sub(r"\s+-\s+[^-]{2,40}$", "", it["title"])
            tickers = match_tickers(head)
            if tickers and CATALYST_RE.search(head):
                it["title"] = head
                alert_news(tickers, it, "news")


last_quotes = {}


def yahoo_screener():
    """Market-wide top gainers + losers in one or two calls (includes pre-market)."""
    out = {}
    for scr in ("day_gainers", "day_losers"):
        url = ("https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved?"
               f"scrIds={scr}&count=250")
        data = json.loads(fetch(url, timeout=20))
        for q in data["finance"]["result"][0]["quotes"]:
            sym = q.get("symbol")
            pct = q.get("regularMarketChangePercent")
            price = q.get("regularMarketPrice")
            if q.get("marketState") == "PRE" and q.get("preMarketChangePercent") is not None:
                pct, price = q["preMarketChangePercent"], q.get("preMarketPrice", price)
            elif q.get("marketState") in ("POST", "POSTPOST") and q.get("postMarketChangePercent") is not None:
                # after-hours move on top of the day's move
                base = q.get("regularMarketPreviousClose")
                if base and q.get("postMarketPrice"):
                    price = q["postMarketPrice"]
                    pct = (price / base - 1) * 100
            if sym and pct is not None and price:
                out[sym] = {"price": float(price), "pct": float(pct)}
    return out


def yahoo_spark(symbols):
    url = ("https://query1.finance.yahoo.com/v8/finance/spark?"
           f"symbols={','.join(symbols)}&range=1d&interval=5m")
    data = json.loads(fetch(url))
    out = {}
    for sym, r in data.items():
        if not isinstance(r, dict):
            continue
        closes = [c for c in (r.get("close") or []) if c]
        prev = r.get("chartPreviousClose") or r.get("previousClose")
        if prev and closes:
            out[sym] = {"price": closes[-1], "pct": (closes[-1] / prev - 1) * 100}
    return out


def latest_headline(ticker):
    name = (NAME_OVERRIDES.get(ticker) or [short_name(UNIVERSE.get(ticker, ticker))])[0]
    q = f"\"{name}\" stock when:1d"
    url = ("https://news.google.com/rss/search?hl=en-US&gl=US&ceid=US:en&q="
           + urllib.parse.quote(q))
    try:
        items = parse_rss(fetch(url))
        items.sort(key=lambda i: i["date"] or datetime.min.replace(tzinfo=timezone.utc),
                   reverse=True)
        for it in items[:5]:
            head = re.sub(r"\s+-\s+[^-]{2,40}$", "", it["title"])
            if ticker in match_tickers(head) or name.lower() in head.lower():
                return head, it["link"], it["source"]
    except Exception:
        pass
    return None, None, None


def poll_moves(initial=False):
    got = {}
    errors = 0
    last_err = None
    try:
        allq = yahoo_screener()
        got = {t: q for t, q in allq.items() if t in UNIVERSE}
        mark("PriceMoves", True, f"screener: {len(allq)} movers, {len(got)} halal")
        picks = [t for t in MY_PICKS if t in UNIVERSE]
        for i in range(0, len(picks), 10):
            try:
                for t, q in yahoo_spark(picks[i:i + 10]).items():
                    got.setdefault(t, q)
            except Exception:
                pass
    except Exception as e:
        last_err = e
        syms = sorted(UNIVERSE)
        for i in range(0, len(syms), 10):
            try:
                got.update(yahoo_spark(syms[i:i + 10]))
            except Exception as e2:
                errors += 1
                last_err = e2
            time.sleep(0.3)
        if got:
            mark("PriceMoves", True, f"spark fallback: {len(got)} quotes, {errors} failed")
        else:
            mark("PriceMoves", False, last_err)
            return
    last_quotes.update(got)
    today = datetime.now(UK).date()
    for t, q in got.items():
        pct = q["pct"]
        if abs(pct) < MOVE_PCT:
            continue
        bucket = sum(1 for x in MOVE_STEPS if abs(pct) >= x)  # 3%, 8%, 15%, 25%...
        prev = moved_today.get(t)
        if prev and prev[0] == today and prev[1] >= bucket:
            continue
        moved_today[t] = (today, bucket)
        if initial:
            continue
        head, link, src = latest_headline(t)
        icon = "🚀" if pct > 0 else "📉"
        title = f"{icon} {t} {pct:+.1f}% - {UNIVERSE[t][:40]}"
        body = (f"Now ${q['price']:.2f}. "
                + (f"Likely why: {head}" if head else "No headline found yet."))
        n = note_for(head or "")
        if n:
            body += f"\n{n}"
        body += f"\n{tier_note(t)}. Not advice."
        push(title, body, link, priority=5 if abs(pct) >= 15 else 4,
             tags="chart_with_upwards_trend" if pct > 0 else "chart_with_downwards_trend")


# --------------------------------------------------------------- schedule
def active_now():
    n = datetime.now(UK)
    if n.weekday() >= 5:
        return False
    return (n.hour, n.minute) >= (6, 0) and (n.hour, n.minute) <= (22, 30)


def keepalive():
    while True:
        time.sleep(600)
        if SELF_URL and active_now():
            try:
                fetch(SELF_URL + "/health", timeout=20)
            except Exception:
                pass


def loop():
    load_universe()
    log("Warm-up pass (marking existing news as seen)...")
    for fn, kw in ((poll_pr, {}), (poll_google, {"queries": GN_QUERIES}), (poll_moves, {})):
        try:
            fn(initial=True, **kw)
        except Exception:
            traceback.print_exc()
    state["startup_ok"] = os.environ.get("STARTUP_MSG") != "1" or push("✅ Halal news watcher is running",
         f"Watching {len(UNIVERSE)} halal stocks. "
         f"Alerts for takeovers, big news and {MOVE_PCT:.0f}%+ moves, "
         "weekdays 06:00-22:30 UK.", priority=3, tags="white_check_mark")
    tick = 0
    last_day = datetime.now(UK).date()
    while True:
        start = time.time()
        try:
            if datetime.now(UK).date() != last_day:
                last_day = datetime.now(UK).date()
                load_universe()
                moved_today.clear()
                if len(seen) > 50000:
                    seen.clear()
            if not state.get("startup_ok") and tick % 9 == 0 and TG_TOKEN:
                state["startup_ok"] = os.environ.get("STARTUP_MSG") != "1" or push("✅ Halal news watcher is running",
                                           f"Watching {len(UNIVERSE)} halal stocks. Alerts for takeovers, big news and {MOVE_PCT:.0f}%+ moves, weekdays 06:00-22:30 UK.")
            if active_now():
                poll_pr()                    # every ~20s
                poll_google()                # one query per cycle, rotating
                if tick % 3 == 0:
                    poll_moves()             # every ~60s
            state["last_cycle"] = datetime.now(UK).strftime("%a %H:%M:%S")
        except Exception:
            traceback.print_exc()
        tick += 1
        time.sleep(max(5, 20 - (time.time() - start)))


# ------------------------------------------------------------------ web
class H(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        b = json.dumps(obj, indent=2, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        p = urllib.parse.urlparse(self.path)
        if p.path == "/robots.txt":
            b = b"User-agent: *\nAllow: /\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b)
            return
        if p.path == "/debug":
            qs = urllib.parse.parse_qs(p.query)
            if qs.get("topic", [""])[0] != NTFY_TOPIC:
                return self._send(403, {"error": "pass ?topic=<your topic>"})
            out = {}
            urls = {
                "spark_1d": "https://query1.finance.yahoo.com/v8/finance/spark?symbols=PTC,AAPL&range=1d&interval=1d",
                "spark_5m": "https://query1.finance.yahoo.com/v8/finance/spark?symbols=PTC,AAPL&range=1d&interval=5m",
                "chart": "https://query1.finance.yahoo.com/v8/finance/chart/PTC?range=1d&interval=5m",
                "quote_v7": "https://query1.finance.yahoo.com/v7/finance/quote?symbols=PTC",
                "screener": "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved?scrIds=day_gainers&count=25",
                "nasdaq_movers": "https://api.nasdaq.com/api/marketmovers?assetclass=stocks&exchangestatus=currentMarket&limit=20",
                "nasdaq_quote": "https://api.nasdaq.com/api/quote/PTC/info?assetclass=stocks",
                "globe": PR_FEEDS[1],
                "ntfy_health": NTFY_SERVER + "/v1/health",
            }
            if TG_TOKEN:
                urls["tg_getMe"] = f"https://api.telegram.org/bot{TG_TOKEN}/getMe"
                out["tg_chat"] = tg_chat_ids()
            for k, u in urls.items():
                try:
                    b = fetch(u, timeout=20)
                    out[k] = {"ok": True, "len": len(b), "head": b[:300].decode("utf-8", "ignore")}
                except Exception as e:
                    body = ""
                    if hasattr(e, "read"):
                        try:
                            body = e.read()[:200].decode("utf-8", "ignore")
                        except Exception:
                            pass
                    out[k] = {"ok": False, "err": str(e), "body": body}
            return self._send(200, out)
        if p.path == "/universe":
            qs = urllib.parse.parse_qs(p.query)
            check = [x.strip().upper() for x in qs.get("check", [""])[0].split(",") if x.strip()]
            by_tier = {}
            for t, tr in TIER.items():
                by_tier[tr] = by_tier.get(tr, 0) + 1
            return self._send(200, {
                "total": len(UNIVERSE), "sources": state["universe_source"],
                "by_tier": by_tier, "move_steps": MOVE_STEPS,
                "check": {c: ("BDS-excluded" if c in BDS_EXCLUDE else
                              (TIER.get(c) if c in UNIVERSE else "not in list"))
                          for c in check},
                "screened_sample": sorted(t for t, tr in TIER.items() if tr == "screened")[:400],
            })
        if p.path == "/chats":
            qs = urllib.parse.parse_qs(p.query)
            if qs.get("topic", [""])[0] != NTFY_TOPIC:
                return self._send(403, {"error": "pass ?topic=<your topic>"})
            return self._send(200, {"sending_to": tg_chat_ids(), "bot_sees": tg_list_chats()})
        if p.path == "/test":
            qs = urllib.parse.parse_qs(p.query)
            if qs.get("topic", [""])[0] != NTFY_TOPIC:
                return self._send(403, {"error": "pass ?topic=<your topic>"})
            ok = push("⚡ PTC +35.7% - TEST alert",
                      "Schneider Electric reportedly close to a $20B takeover of PTC.\n"
                      "This is a test so you know alerts reach your phone.",
                      "https://www.trading212.com", priority=4)
            return self._send(200, {"sent": ok})
        self._send(200, {"status": "ok", "active_hours_now": active_now(), **state})

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    threading.Thread(target=loop, daemon=True).start()
    threading.Thread(target=keepalive, daemon=True).start()
    port = int(os.environ.get("PORT", "10000"))
    log("HTTP on", port)
    HTTPServer(("0.0.0.0", port), H).serve_forever()
