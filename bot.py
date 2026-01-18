import os
import re
import io
import json
import time
import random
from typing import Any, Dict, List, Optional, Tuple

import httpx
from telegram import Update, InputFile
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes


# -----------------------
# ENV
# -----------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()

CHAIN = os.getenv("CHAIN", "polygon").strip()

BROS = os.getenv("NEANDERBROS_CONTRACT", "").strip().lower()
GALS = os.getenv("NEANDERGALS_CONTRACT", "").strip().lower()

MAX_TRAITS = int(os.getenv("MAX_TRAITS", "50"))

# Alchemy (trait rarity + metadata + minted-so-far proxy)
ALCHEMY_API_KEY = os.getenv("ALCHEMY_API_KEY", "").strip()
ALCHEMY_NETWORK = os.getenv("ALCHEMY_NETWORK", "polygon-mainnet").strip()
ALCHEMY_BASE_URL = os.getenv("ALCHEMY_BASE_URL", "").strip().rstrip("/")

# OpenSea (overall rarity rank)
OPENSEA_API_KEY = os.getenv("OPENSEA_API_KEY", "").strip()

# -----------------------
# AUTPOST (Option B)
# -----------------------
AUTOPOST_ENABLED = os.getenv("AUTOPOST_ENABLED", "false").strip().lower() in ("1", "true", "yes", "y", "on")
AUTOPOST_CHAT_ID = os.getenv("AUTOPOST_CHAT_ID", "").strip()  # required if AUTOPOST_ENABLED=true

BRO_WEIGHT = float(os.getenv("BRO_WEIGHT", "0.80"))
GAL_WEIGHT = float(os.getenv("GAL_WEIGHT", "0.20"))

MIN_MINUTES = int(os.getenv("AUTOPOST_MIN_MINUTES", "30"))
MAX_MINUTES = int(os.getenv("AUTOPOST_MAX_MINUTES", "180"))

MAX_POSTS_PER_24H = int(os.getenv("AUTOPOST_MAX_POSTS_PER_24H", "5"))
WINDOW_HOURS = int(os.getenv("AUTOPOST_WINDOW_HOURS", "24"))

# TokenId floors (to avoid token 0 and other known ranges)
# Per your known info: Bros have no token 0; Gals typically match tokenId
BROS_MIN_TOKEN_ID = int(os.getenv("BROS_MIN_TOKEN_ID", "1"))
GALS_MIN_TOKEN_ID = int(os.getenv("GALS_MIN_TOKEN_ID", "0"))

# Supply cache refresh cadence
SUPPLY_REFRESH_SECONDS = int(os.getenv("SUPPLY_REFRESH_SECONDS", str(15 * 60)))  # default 15 minutes

# State persistence path (mount a volume to keep this across restarts)
STATE_PATH = os.getenv("BOT_STATE_PATH", "/app/state/neander_fetch_state.json").strip()

# Retry controls for random token selection
RANDOM_PICK_RETRIES = int(os.getenv("RANDOM_PICK_RETRIES", "6"))

if not TELEGRAM_BOT_TOKEN:
    raise SystemExit("Missing TELEGRAM_BOT_TOKEN")
if not ALCHEMY_API_KEY:
    raise SystemExit("Missing ALCHEMY_API_KEY")
if not OPENSEA_API_KEY:
    raise SystemExit("Missing OPENSEA_API_KEY")
if not BROS:
    raise SystemExit("Missing NEANDERBROS_CONTRACT")
if not GALS:
    raise SystemExit("Missing NEANDERGALS_CONTRACT")
if AUTOPOST_ENABLED and not AUTOPOST_CHAT_ID:
    raise SystemExit("AUTOPOST_ENABLED is true but missing AUTOPOST_CHAT_ID")


# -----------------------
# BEST-PRACTICE DISPLAY ID RULE
# -----------------------
DISPLAY_ID_OFFSETS: Dict[str, int] = {
    BROS: 1,  # NeanderBros: UI shows tokenId+1
    GALS: 0,  # NeanderGals: UI matches tokenId
}


def _alchemy_root() -> str:
    if ALCHEMY_BASE_URL:
        return f"{ALCHEMY_BASE_URL}/nft/v3/{ALCHEMY_API_KEY}"
    return f"https://{ALCHEMY_NETWORK}.g.alchemy.com/nft/v3/{ALCHEMY_API_KEY}"


def _parse_token_id(args: List[str]) -> Optional[int]:
    if not args:
        return None
    token = args[0].strip().lstrip("#")
    if not re.fullmatch(r"\d+", token):
        return None
    return int(token)


def _opensea_url(contract: str, token_id: int) -> str:
    return f"https://opensea.io/assets/{CHAIN}/{contract}/{token_id}"


def _collection_label(contract: str) -> str:
    c = (contract or "").lower()
    if c == BROS:
        return "NeanderBros"
    if c == GALS:
        return "NeanderGals"
    return "NFT"


def _display_nft_id(contract: str, token_id: int) -> int:
    c = (contract or "").lower()
    return token_id + DISPLAY_ID_OFFSETS.get(c, 0)


def _safe_int(v: Any) -> Optional[int]:
    try:
        if v is None or isinstance(v, bool):
            return None
        return int(str(v), 0) if isinstance(v, str) and v.strip().lower().startswith("0x") else int(str(v))
    except Exception:
        return None


def _norm(s: Any) -> str:
    return str(s).strip().casefold()


def _prevalence_to_pct(prevalence: Any) -> Optional[float]:
    try:
        p = float(prevalence)
    except Exception:
        return None
    if p <= 1.0:
        return p * 100.0
    return p


async def _get_json(
    url: str,
    params: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
    timeout: float = 25.0,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    h = {"accept": "application/json"}
    if headers:
        h.update(headers)

    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            r = await client.get(url, params=params, headers=h)
    except Exception as e:
        return None, f"Network error calling API: {e}"

    if r.status_code == 429:
        return None, "API throttled the request (429). Please try again in ~10–20 seconds."
    if r.status_code in (401, 403):
        return None, f"API auth error ({r.status_code}). Check your API key/env vars."
    if r.status_code >= 400:
        return None, f"API error {r.status_code}: {r.text[:200]}"

    try:
        data = r.json()
        if isinstance(data, dict):
            return data, None
        return None, "Unexpected API response (not a JSON object)."
    except Exception:
        return None, "Could not parse API response as JSON."


# -----------------------
# ALCHEMY CALLS
# -----------------------
async def fetch_nft_metadata_alchemy(contract: str, token_id: int) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    url = f"{_alchemy_root()}/getNFTMetadata"
    params = {"contractAddress": contract, "tokenId": str(token_id), "refreshCache": "false"}
    return await _get_json(url, params=params)


async def fetch_compute_rarity_alchemy(contract: str, token_id: int) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    url = f"{_alchemy_root()}/computeRarity"
    params = {"contractAddress": contract, "tokenId": str(token_id)}
    return await _get_json(url, params=params)


async def fetch_minted_so_far_alchemy(contract: str) -> Tuple[Optional[int], Optional[str]]:
    """
    Uses Alchemy getContractMetadata.totalSupply as a practical “minted/indexed so far” proxy.
    """
    url = f"{_alchemy_root()}/getContractMetadata"
    params = {"contractAddress": contract}
    data, err = await _get_json(url, params=params)
    if err:
        return None, err
    minted = _safe_int((data or {}).get("totalSupply"))
    return minted, None


def _pick_image_url(meta: Dict[str, Any]) -> Optional[str]:
    image = meta.get("image")
    if isinstance(image, dict):
        for k in ("pngUrl", "cachedUrl", "thumbnailUrl", "originalUrl"):
            v = image.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()

    raw = meta.get("raw")
    if isinstance(raw, dict):
        md = raw.get("metadata")
        if isinstance(md, dict):
            v = md.get("image") or md.get("image_url")
            if isinstance(v, str) and v.strip():
                return v.strip()

    return None


def _extract_traits(meta: Dict[str, Any]) -> List[Dict[str, Any]]:
    raw = meta.get("raw")
    if isinstance(raw, dict):
        md = raw.get("metadata")
        if isinstance(md, dict):
            attrs = md.get("attributes")
            if isinstance(attrs, list):
                return [a for a in attrs if isinstance(a, dict)]
    return []


def _build_trait_pct_map_from_alchemy(rarity_resp: Dict[str, Any]) -> Dict[Tuple[str, str], float]:
    out: Dict[Tuple[str, str], float] = {}
    rarities = rarity_resp.get("rarities")
    if not isinstance(rarities, list):
        return out

    for item in rarities:
        if not isinstance(item, dict):
            continue
        tt = item.get("trait_type") or item.get("traitType") or item.get("key") or item.get("trait")
        vv = item.get("value")
        if tt is None or vv is None:
            continue
        pct = _prevalence_to_pct(item.get("prevalence") or item.get("frequency") or item.get("pct"))
        if pct is None:
            continue
        out[(_norm(tt), _norm(vv))] = pct

    return out


def _format_trait_line(trait_type: str, value: str, pct: Optional[float], minted_so_far: Optional[int]) -> str:
    if pct is None:
        return f"{trait_type}: {value} — n/a"

    pct_str = f"{pct:.2f}%"
    if minted_so_far and minted_so_far > 0:
        count = int(round((pct / 100.0) * minted_so_far))
        return f"{trait_type}: {value} — {count} ({pct_str})"
    return f"{trait_type}: {value} — ({pct_str})"


async def _download_image_bytes(url: str) -> Optional[bytes]:
    try:
        async with httpx.AsyncClient(timeout=25, follow_redirects=True) as client:
            r = await client.get(url, headers={"user-agent": "Mozilla/5.0 (compatible; NeanderLookupBot/1.0)"})
            r.raise_for_status()
            return r.content
    except Exception:
        return None


# -----------------------
# OPENSEA CALL (RARITY RANK)
# -----------------------
async def fetch_opensea_rank(contract: str, token_id: int) -> Tuple[Optional[int], Optional[str]]:
    url = f"https://api.opensea.io/api/v2/chain/{CHAIN}/contract/{contract}/nfts/{token_id}"
    headers = {
        "accept": "application/json",
        "x-api-key": OPENSEA_API_KEY,
        "user-agent": "Mozilla/5.0 (compatible; NeanderLookupBot/1.0)",
    }
    data, err = await _get_json(url, params=None, headers=headers, timeout=25.0)
    if err:
        return None, err

    nft = data.get("nft") if isinstance(data, dict) else None
    if not isinstance(nft, dict):
        nft = data if isinstance(data, dict) else None
    if not isinstance(nft, dict):
        return None, "Unexpected OpenSea response."

    rarity = nft.get("rarity")
    if isinstance(rarity, dict):
        rank = _safe_int(rarity.get("rank"))
        if rank is not None:
            return rank, None

    rank = _safe_int(nft.get("rarity_rank") or nft.get("rarityRank"))
    return rank, None


# -----------------------
# STATE (persistent caps + cached supplies)
# -----------------------
def _load_state() -> Dict[str, Any]:
    try:
        with open(STATE_PATH, "r") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:
        return {}


def _save_state(state: Dict[str, Any]) -> None:
    folder = os.path.dirname(STATE_PATH) or "."
    os.makedirs(folder, exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, STATE_PATH)


def _prune_old_post_times(post_times: List[float], now_ts: float) -> List[float]:
    cutoff = now_ts - (WINDOW_HOURS * 3600)
    return [t for t in post_times if t >= cutoff]


def _next_delay_if_capped(post_times: List[float], now_ts: float) -> int:
    """
    If we already have MAX_POSTS_PER_24H in the last 24h (rolling window),
    schedule next attempt for when the oldest one expires, plus jitter.
    """
    post_times_sorted = sorted(post_times)
    oldest = post_times_sorted[0]
    window_seconds = WINDOW_HOURS * 3600
    seconds_until_allowed = int((oldest + window_seconds) - now_ts)

    # jitter: 1–10 minutes to keep it from becoming deterministic
    jitter = random.randint(60, 10 * 60)
    return max(60, seconds_until_allowed + jitter)


def _choose_collection_weighted() -> str:
    return random.choices(
        population=["bro", "gal"],
        weights=[max(BRO_WEIGHT, 0.0), max(GAL_WEIGHT, 0.0)],
        k=1,
    )[0]


def _min_token_id_for(contract: str) -> int:
    c = (contract or "").lower()
    if c == BROS:
        return BROS_MIN_TOKEN_ID
    if c == GALS:
        return GALS_MIN_TOKEN_ID
    return 0


async def _get_cached_supply(contract: str) -> Optional[int]:
    """
    Cached dynamic supply using Alchemy getContractMetadata.totalSupply.
    Cache is stored in STATE_PATH to persist across restarts.
    """
    now_ts = time.time()
    state = _load_state()

    supplies = state.get("supplies")
    if not isinstance(supplies, dict):
        supplies = {}

    entry = supplies.get(contract)
    if isinstance(entry, dict):
        cached_supply = _safe_int(entry.get("supply"))
        cached_at = float(entry.get("ts") or 0.0)
        if cached_supply is not None and (now_ts - cached_at) <= SUPPLY_REFRESH_SECONDS:
            return cached_supply

    supply, err = await fetch_minted_so_far_alchemy(contract)
    if err or supply is None:
        # fall back to cached value if any
        if isinstance(entry, dict):
            cached_supply = _safe_int(entry.get("supply"))
            if cached_supply is not None:
                return cached_supply
        return None

    supplies[contract] = {"supply": int(supply), "ts": now_ts}
    state["supplies"] = supplies
    _save_state(state)
    return int(supply)


# -----------------------
# MESSAGE BUILD (reusable for both commands + autopost)
# -----------------------
async def build_nft_message(contract: str, token_id: int) -> Tuple[Optional[str], Optional[bytes], Optional[str]]:
    """
    Returns: (caption_html, image_bytes, error)
    """
    # Alchemy metadata (image + traits)
    meta, err = await fetch_nft_metadata_alchemy(contract, token_id)
    if err or not isinstance(meta, dict):
        return None, None, err or "Metadata not available."

    # Minted so far (cached supply used for the header + trait estimates)
    minted_so_far = await _get_cached_supply(contract)

    # Trait prevalence map
    rarity_resp, r_err = await fetch_compute_rarity_alchemy(contract, token_id)
    trait_pct_map: Dict[Tuple[str, str], float] = {}
    if not r_err and isinstance(rarity_resp, dict):
        trait_pct_map = _build_trait_pct_map_from_alchemy(rarity_resp)

    # OpenSea overall rank
    os_rank, os_rank_err = await fetch_opensea_rank(contract, token_id)
    if os_rank_err:
        os_rank = None

    coll = _collection_label(contract)
    nft_id = _display_nft_id(contract, token_id)

    header1 = f"<b>{coll} NFT ID #{nft_id}</b>"
    header2 = f"Token ID #{token_id}"
    if minted_so_far and minted_so_far > 0:
        header2 += f" of {minted_so_far}"

    traits = _extract_traits(meta or {})
    trait_lines: List[str] = []
    for a in traits[:MAX_TRAITS]:
        tt = a.get("trait_type") or a.get("type") or a.get("traitType") or "Trait"
        vv = a.get("value")
        if not isinstance(tt, str) or vv is None:
            continue
        vv_s = str(vv)
        pct = trait_pct_map.get((_norm(tt), _norm(vv_s)))
        trait_lines.append(_format_trait_line(tt, vv_s, pct, minted_so_far))

    traits_text = "\n".join(trait_lines) if trait_lines else "(No traits returned.)"

    rarity_lines = ["<b>Rarity (OpenSea)</b>"]
    if os_rank is not None:
        rarity_lines.append(f"Rank: #{os_rank}")
    else:
        rarity_lines.append("Rank not available from OpenSea API for this item.")

    os_url = _opensea_url(contract, token_id)
    rarity_block = "\n".join(rarity_lines)

    caption = (
        f"{header1}\n"
        f"{header2}\n\n"
        f"{rarity_block}\n\n"
        f"<b>Traits</b>\n"
        f"{traits_text}\n\n"
        f"<a href=\"{os_url}\">View on OpenSea</a>"
    )

    image_url = _pick_image_url(meta or {})
    img_bytes: Optional[bytes] = None
    if image_url:
        img_bytes = await _download_image_bytes(image_url)

    return caption, img_bytes, None


# -----------------------
# HANDLERS (commands)
# -----------------------
async def handle_lookup(update: Update, context: ContextTypes.DEFAULT_TYPE, contract: str, cmd_label: str) -> None:
    token_id = _parse_token_id(context.args)
    if token_id is None:
        await update.message.reply_text(f"Usage: /{cmd_label} <tokenId>  (example: /{cmd_label} 33)")
        return

    await update.message.chat.send_action(action="typing")

    caption, img_bytes, err = await build_nft_message(contract, token_id)
    if err:
        await update.message.reply_text(err)
        return

    if img_bytes:
        bio = io.BytesIO(img_bytes)
        bio.name = "nft.png"
        try:
            await update.message.reply_photo(
                photo=InputFile(bio),
                caption=caption,
                parse_mode=ParseMode.HTML,
            )
            return
        except Exception:
            pass

    await update.message.reply_text(caption, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


async def bro_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await handle_lookup(update, context, BROS, "bro")


async def gal_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await handle_lookup(update, context, GALS, "gal")


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Ready. Use /bro <tokenId> or /gal <tokenId>.")


# -----------------------
# AUTPOST LOOP (Option B)
# -----------------------
async def _autopost_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Self-rescheduling job:
    - Enforces max 5 posts per rolling 24h window (persistent)
    - Uses weighted selection (80/20 default)
    - Uses dynamic supply from Alchemy (cached)
    - Retries if tokenId doesn't resolve (gaps, indexing lag)
    """
    # Always reschedule at the end of this function (or earlier when capped)
    now_ts = time.time()

    state = _load_state()

    # Post cap history
    post_times = state.get("autopost_times", [])
    if not isinstance(post_times, list):
        post_times = []
    post_times = [float(t) for t in post_times if isinstance(t, (int, float)) or (isinstance(t, str) and str(t).strip().isdigit())]
    post_times = _prune_old_post_times(post_times, now_ts)

    # If capped, schedule next attempt when allowed
    if len(post_times) >= MAX_POSTS_PER_24H:
        state["autopost_times"] = post_times
        _save_state(state)

        next_delay_seconds = _next_delay_if_capped(post_times, now_ts)
        context.job_queue.run_once(_autopost_job, when=next_delay_seconds, name="autopost_loop")
        return

    # Choose collection (weighted)
    choice = _choose_collection_weighted()
    contract = BROS if choice == "bro" else GALS

    # Fetch dynamic supply (cached)
    supply = await _get_cached_supply(contract)
    min_id = _min_token_id_for(contract)

    if supply is None or supply <= min_id:
        # If we can't get supply, do a conservative delay and try later.
        next_delay_seconds = random.randint(MIN_MINUTES * 60, MAX_MINUTES * 60)
        context.job_queue.run_once(_autopost_job, when=next_delay_seconds, name="autopost_loop")
        return

    # Try a few random token picks in case of gaps / indexing lag
    caption: Optional[str] = None
    img_bytes: Optional[bytes] = None
    last_err: Optional[str] = None

    for _ in range(max(1, RANDOM_PICK_RETRIES)):
        token_id = random.randint(min_id, supply - 1) if supply > (min_id + 1) else min_id
        cap, img, err = await build_nft_message(contract, token_id)
        if err:
            last_err = err
            continue
        caption = cap
        img_bytes = img
        break

    if caption is None:
        # Could not build a post; wait and try again
        next_delay_seconds = random.randint(MIN_MINUTES * 60, MAX_MINUTES * 60)
        context.job_queue.run_once(_autopost_job, when=next_delay_seconds, name="autopost_loop")
        return

    # Send to target chat/channel
    # Note: chat_id can be numeric (as string) or @channelusername
    try:
        if img_bytes:
            bio = io.BytesIO(img_bytes)
            bio.name = "nft.png"
            await context.bot.send_photo(
                chat_id=AUTOPOST_CHAT_ID,
                photo=InputFile(bio),
                caption=caption,
                parse_mode=ParseMode.HTML,
            )
        else:
            await context.bot.send_message(
                chat_id=AUTOPOST_CHAT_ID,
                text=caption,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )

        # Record success in rolling window
        post_times.append(now_ts)
        state["autopost_times"] = post_times
        _save_state(state)

    except Exception:
        # If send fails (permissions, wrong chat_id, etc.), do not count it against the cap.
        pass

    # Schedule next run (random delay)
    next_delay_seconds = random.randint(MIN_MINUTES * 60, MAX_MINUTES * 60)
    context.job_queue.run_once(_autopost_job, when=next_delay_seconds, name="autopost_loop")


def start_autopost_loop(job_queue) -> None:
    # prevent duplicate loops after restart/redeploy
    for job in job_queue.jobs():
        if job.name == "autopost_loop":
            job.schedule_removal()

    # start shortly after boot
    job_queue.run_once(_autopost_job, when=10, name="autopost_loop")


def main() -> None:
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("bro", bro_cmd))
    app.add_handler(CommandHandler("gal", gal_cmd))

    if AUTOPOST_ENABLED:
        start_autopost_loop(app.job_queue)

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
