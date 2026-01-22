import os
import re
import io
from typing import Any, Dict, List, Optional, Tuple

import httpx
from telegram import Update, InputFile
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.request import HTTPXRequest


# -----------------------
# ENV
# -----------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()

CHAIN = os.getenv("CHAIN", "polygon").strip()

BROS = os.getenv("NEANDERBROS_CONTRACT", "").strip().lower()
GALS = os.getenv("NEANDERGALS_CONTRACT", "").strip().lower()

MAX_TRAITS = int(os.getenv("MAX_TRAITS", "50"))

# Alchemy
ALCHEMY_API_KEY = os.getenv("ALCHEMY_API_KEY", "").strip()
ALCHEMY_NETWORK = os.getenv("ALCHEMY_NETWORK", "polygon-mainnet").strip()
ALCHEMY_BASE_URL = os.getenv("ALCHEMY_BASE_URL", "").strip().rstrip("/")

# OpenSea
OPENSEA_API_KEY = os.getenv("OPENSEA_API_KEY", "").strip()

# Bros have no token 0 (per your known info)
BROS_MIN_TOKEN_ID = int(os.getenv("BROS_MIN_TOKEN_ID", "1"))
GALS_MIN_TOKEN_ID = int(os.getenv("GALS_MIN_TOKEN_ID", "0"))

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


# -----------------------
# DISPLAY ID OFFSETS
# -----------------------
# NeanderBros UI shows tokenId+1; NeanderGals UI matches tokenId
DISPLAY_ID_OFFSETS: Dict[str, int] = {
    BROS: 1,
    GALS: 0,
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
        s = str(v).strip()
        if s.lower().startswith("0x"):
            return int(s, 16)
        return int(s)
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


async def fetch_total_supply_alchemy(contract: str) -> Tuple[Optional[int], Optional[str]]:
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
# MESSAGE BUILD
# -----------------------
async def build_nft_message(contract: str, token_id: int) -> Tuple[Optional[str], Optional[bytes], Optional[str]]:
    meta, err = await fetch_nft_metadata_alchemy(contract, token_id)
    if err or not isinstance(meta, dict):
        return None, None, err or "Metadata not available."

    minted_so_far, _ = await fetch_total_supply_alchemy(contract)

    rarity_resp, r_err = await fetch_compute_rarity_alchemy(contract, token_id)
    trait_pct_map: Dict[Tuple[str, str], float] = {}
    if not r_err and isinstance(rarity_resp, dict):
        trait_pct_map = _build_trait_pct_map_from_alchemy(rarity_resp)

    os_rank, os_rank_err = await fetch_opensea_rank(contract, token_id)
    if os_rank_err:
        os_rank = None

    coll = _collection_label(contract)
    nft_id = _display_nft_id(contract, token_id)

    header1 = f"<b>{coll} NFT ID #{nft_id}</b>"
    header2 = f"Token ID #{token_id}"
    if minted_so_far and minted_so_far > 0:
        header2 += f" of {minted_so_far}"

    traits = _extract_traits(meta)
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

    caption = (
        f"{header1}\n"
        f"{header2}\n\n"
        f"{'\n'.join(rarity_lines)}\n\n"
        f"<b>Traits</b>\n"
        f"{traits_text}\n\n"
        f"<a href=\"{os_url}\">View on OpenSea</a>"
    )

    image_url = _pick_image_url(meta)
    img_bytes: Optional[bytes] = None
    if image_url:
        img_bytes = await _download_image_bytes(image_url)

    return caption, img_bytes, None


# -----------------------
# HANDLERS
# -----------------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Ready. Use /bro <tokenId> or /gal <tokenId>.")


async def _handle_lookup(update: Update, context: ContextTypes.DEFAULT_TYPE, contract: str, label: str, min_id: int) -> None:
    token_id = _parse_token_id(context.args)
    if token_id is None:
        await update.message.reply_text(f"Usage: /{label} <tokenId>  (example: /{label} 33)")
        return
    if token_id < min_id:
        await update.message.reply_text(f"{label.upper()} tokenId must be >= {min_id}.")
        return

    await update.message.chat.send_action(action="typing")

    caption, img_bytes, err = await build_nft_message(contract, token_id)
    if err:
        await update.message.reply_text(err)
        return

    if img_bytes:
        bio = io.BytesIO(img_bytes)
        bio.name = "nft.png"
        await update.message.reply_photo(
            photo=InputFile(bio),
            caption=caption,
            parse_mode=ParseMode.HTML,
        )
    else:
        await update.message.reply_text(caption, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


async def bro_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _handle_lookup(update, context, BROS, "bro", BROS_MIN_TOKEN_ID)


async def gal_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _handle_lookup(update, context, GALS, "gal", GALS_MIN_TOKEN_ID)


def main() -> None:
    request = HTTPXRequest(
        connect_timeout=15.0,
        read_timeout=45.0,
        write_timeout=45.0,
        pool_timeout=15.0,
    )

    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .request(request)
        .build()
    )

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("bro", bro_cmd))
    app.add_handler(CommandHandler("gal", gal_cmd))

    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
