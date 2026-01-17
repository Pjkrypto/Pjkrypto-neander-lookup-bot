import os
import re
import io
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

ALCHEMY_API_KEY = os.getenv("ALCHEMY_API_KEY", "").strip()
ALCHEMY_NETWORK = os.getenv("ALCHEMY_NETWORK", "polygon-mainnet").strip()
ALCHEMY_BASE_URL = os.getenv("ALCHEMY_BASE_URL", "").strip().rstrip("/")

if not TELEGRAM_BOT_TOKEN:
    raise SystemExit("Missing TELEGRAM_BOT_TOKEN")
if not ALCHEMY_API_KEY:
    raise SystemExit("Missing ALCHEMY_API_KEY")
if not BROS:
    raise SystemExit("Missing NEANDERBROS_CONTRACT")
if not GALS:
    raise SystemExit("Missing NEANDERGALS_CONTRACT")


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


async def _get_json(url: str, params: Optional[Dict[str, Any]] = None, timeout: float = 25.0) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            r = await client.get(url, params=params, headers={"accept": "application/json"})
    except Exception as e:
        return None, f"Network error calling Alchemy: {e}"

    if r.status_code == 429:
        return None, "Alchemy throttled the request (429). Please try again in ~10–20 seconds."
    if r.status_code in (401, 403):
        return None, f"Alchemy auth error ({r.status_code}). Check ALCHEMY_API_KEY / ALCHEMY_NETWORK."
    if r.status_code >= 400:
        return None, f"Alchemy error {r.status_code}: {r.text[:200]}"

    try:
        data = r.json()
        if isinstance(data, dict):
            return data, None
        return None, "Unexpected Alchemy response (not a JSON object)."
    except Exception:
        return None, "Could not parse Alchemy response as JSON."


async def fetch_nft_metadata(contract: str, token_id: int) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    url = f"{_alchemy_root()}/getNFTMetadata"
    params = {
        "contractAddress": contract,
        "tokenId": str(token_id),
        "refreshCache": "false",
    }
    return await _get_json(url, params=params)


async def fetch_contract_metadata(contract: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    url = f"{_alchemy_root()}/getContractMetadata"
    params = {"contractAddress": contract}
    return await _get_json(url, params=params)


async def fetch_compute_rarity(contract: str, token_id: int) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    url = f"{_alchemy_root()}/computeRarity"
    params = {"contractAddress": contract, "tokenId": str(token_id)}
    return await _get_json(url, params=params)


def _safe_int(v: Any) -> Optional[int]:
    try:
        if v is None:
            return None
        if isinstance(v, bool):
            return None
        return int(str(v), 0) if isinstance(v, str) and v.strip().lower().startswith("0x") else int(str(v))
    except Exception:
        return None


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


def _norm(s: Any) -> str:
    return str(s).strip().casefold()


def _prevalence_to_pct(prevalence: Any) -> Optional[float]:
    try:
        p = float(prevalence)
    except Exception:
        return None
    # Heuristic: <= 1.0 => fraction
    if p <= 1.0:
        return p * 100.0
    return p


async def _download_image_bytes(url: str) -> Optional[bytes]:
    try:
        async with httpx.AsyncClient(timeout=25, follow_redirects=True) as client:
            r = await client.get(url, headers={"user-agent": "Mozilla/5.0 (compatible; NeanderLookupBot/1.0)"})
            r.raise_for_status()
            return r.content
    except Exception:
        return None


def _format_trait_line(trait_type: str, value: str, pct: Optional[float], total_supply: Optional[int]) -> str:
    if pct is None:
        return f"{trait_type}: {value} — n/a"
    pct_str = f"{pct:.2f}%"
    if total_supply and total_supply > 0:
        count = int(round((pct / 100.0) * total_supply))
        return f"{trait_type}: {value} — {count} ({pct_str})"
    return f"{trait_type}: {value} — ({pct_str})"


def _collection_label(contract: str) -> str:
    c = (contract or "").lower()
    if c == BROS:
        return "NeanderBros"
    if c == GALS:
        return "NeanderGals"
    return "NFT"


def _extract_overall_rarity(rarity_resp: Dict[str, Any]) -> Tuple[Optional[int], Optional[float]]:
    """
    Alchemy's computeRarity can include overall fields depending on collection/indexing.
    We try common variants safely.
    """
    rank = _safe_int(
        rarity_resp.get("rank")
        or rarity_resp.get("rarityRank")
        or (rarity_resp.get("rarity") or {}).get("rank") if isinstance(rarity_resp.get("rarity"), dict) else None
    )
    score = None
    try:
        score_val = (
            rarity_resp.get("score")
            or rarity_resp.get("rarityScore")
            or (rarity_resp.get("rarity") or {}).get("score") if isinstance(rarity_resp.get("rarity"), dict) else None
        )
        if score_val is not None:
            score = float(score_val)
    except Exception:
        score = None
    return rank, score


def _build_rarity_map(rarity_resp: Dict[str, Any]) -> Dict[Tuple[str, str], float]:
    """
    Map (trait_type_norm, value_norm) -> percent
    Handles key naming differences: traitType vs trait_type, etc.
    """
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


async def handle_lookup(update: Update, context: ContextTypes.DEFAULT_TYPE, contract: str, cmd_label: str) -> None:
    requested_id = _parse_token_id(context.args)
    if requested_id is None:
        await update.message.reply_text(f"Usage: /{cmd_label} <tokenId>  (example: /{cmd_label} 33)")
        return

    await update.message.chat.send_action(action="typing")

    meta, err = await fetch_nft_metadata(contract, requested_id)
    if err:
        await update.message.reply_text(err)
        return

    # Determine on-chain tokenId from Alchemy response if present (can be hex or decimal)
    onchain_id = _safe_int(meta.get("tokenId")) or requested_id

    # total supply
    contract_meta, cm_err = await fetch_contract_metadata(contract)
    total_supply = None
    if not cm_err and isinstance(contract_meta, dict):
        total_supply = _safe_int(contract_meta.get("totalSupply"))

    # rarity
    rarity_resp, r_err = await fetch_compute_rarity(contract, onchain_id)
    rarity_map: Dict[Tuple[str, str], float] = {}
    overall_rank: Optional[int] = None
    overall_score: Optional[float] = None
    if not r_err and isinstance(rarity_resp, dict):
        rarity_map = _build_rarity_map(rarity_resp)
        overall_rank, overall_score = _extract_overall_rarity(rarity_resp)

    # name + image + traits
    name = meta.get("name") if isinstance(meta.get("name"), str) and meta.get("name").strip() else f"Token #{onchain_id}"
    image_url = _pick_image_url(meta)
    traits = _extract_traits(meta)

    coll = _collection_label(contract)

    # Header formatting you requested
    # If requested != onchain, show both to avoid confusion.
    if onchain_id != requested_id:
        header = f"<b>{coll} NFT ID #{requested_id}</b>\n<i>On-chain Token ID #{onchain_id}</i>"
        token_line = f"Token ID #{onchain_id}" + (f" of {total_supply}" if total_supply else "")
    else:
        header = f"<b>{coll} NFT ID #{onchain_id}</b>"
        token_line = f"Token ID #{onchain_id}" + (f" of {total_supply}" if total_supply else "")

    rarity_lines: List[str] = []
    rarity_lines.append("<b>Rarity</b>")
    if overall_rank is not None:
        rarity_lines.append(f"Rank: #{overall_rank}")
    if overall_score is not None:
        rarity_lines.append(f"Score: {overall_score:.2f}")
    if overall_rank is None and overall_score is None:
        rarity_lines.append("Overall rank not available from Alchemy for this item.")

    # Traits
    lines: List[str] = []
    for a in traits[:MAX_TRAITS]:
        tt = a.get("trait_type") or a.get("type") or a.get("traitType") or "Trait"
        vv = a.get("value")
        if not isinstance(tt, str) or vv is None:
            continue
        vv_s = str(vv)

        pct = rarity_map.get((_norm(tt), _norm(vv_s)))
        lines.append(_format_trait_line(tt, vv_s, pct, total_supply))

    traits_text = "\n".join(lines) if lines else "(No traits returned.)"
    os_url = _opensea_url(contract, onchain_id)

    caption = (
        f"{header}\n"
        f"{token_line}\n\n"
        f"{name}\n\n"
        f"{'\n'.join(rarity_lines)}\n\n"
        f"<b>Traits</b>\n"
        f"{traits_text}\n\n"
        f"<a href=\"{os_url}\">View on OpenSea</a>"
    )

    # Send image at top by uploading bytes
    if image_url:
        img_bytes = await _download_image_bytes(image_url)
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


def main() -> None:
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("bro", bro_cmd))
    app.add_handler(CommandHandler("gal", gal_cmd))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
