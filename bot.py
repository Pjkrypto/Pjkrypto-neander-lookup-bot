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
# Optional override if you prefer to paste a full base URL:
# e.g. https://polygon-mainnet.g.alchemy.com
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
        # user provided explicit base host, e.g. https://polygon-mainnet.g.alchemy.com
        return f"{ALCHEMY_BASE_URL}/nft/v3/{ALCHEMY_API_KEY}"
    # default
    return f"https://{ALCHEMY_NETWORK}.g.alchemy.com/nft/v3/{ALCHEMY_API_KEY}"


def _parse_token_id(args: List[str]) -> Optional[str]:
    if not args:
        return None
    token = args[0].strip().lstrip("#")
    if not re.fullmatch(r"\d+", token):
        return None
    return token


def _opensea_url(contract: str, token_id: str) -> str:
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
        return r.json(), None
    except Exception:
        return None, "Could not parse Alchemy response as JSON."


async def fetch_nft_metadata(contract: str, token_id: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    # getNFTMetadata v3 :contentReference[oaicite:3]{index=3}
    url = f"{_alchemy_root()}/getNFTMetadata"
    params = {
        "contractAddress": contract,
        "tokenId": token_id,          # decimal or hex supported :contentReference[oaicite:4]{index=4}
        "refreshCache": "false",
    }
    return await _get_json(url, params=params)


async def fetch_contract_metadata(contract: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    # getContractMetadata v3 (contains totalSupply) :contentReference[oaicite:5]{index=5}
    url = f"{_alchemy_root()}/getContractMetadata"
    params = {"contractAddress": contract}
    return await _get_json(url, params=params)


async def fetch_compute_rarity(contract: str, token_id: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    # computeRarity v3 :contentReference[oaicite:6]{index=6}
    url = f"{_alchemy_root()}/computeRarity"
    params = {"contractAddress": contract, "tokenId": token_id}
    return await _get_json(url, params=params)


def _safe_int(v: Any) -> Optional[int]:
    try:
        if v is None:
            return None
        # totalSupply can come as a string :contentReference[oaicite:7]{index=7}
        return int(str(v))
    except Exception:
        return None


def _pick_image_url(meta: Dict[str, Any]) -> Optional[str]:
    image = meta.get("image")
    if isinstance(image, dict):
        for k in ("pngUrl", "cachedUrl", "thumbnailUrl", "originalUrl"):
            v = image.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()

    # fallback: sometimes in raw metadata
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
                out = []
                for a in attrs:
                    if isinstance(a, dict):
                        out.append(a)
                return out
    return []


def _prevalence_to_pct(prevalence: Any) -> Optional[float]:
    """
    Alchemy returns 'prevalence' (number). Docs example isn't explicit on scale. :contentReference[oaicite:8]{index=8}
    In practice, it can be either:
      - fraction (0..1), or
      - percent (0..100)
    We normalize to percent.
    """
    try:
        p = float(prevalence)
    except Exception:
        return None

    # Heuristic: if <= 1.0, treat as fraction and convert to percent
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
        # count = pct * supply (rounded)
        count = int(round((pct / 100.0) * total_supply))
        return f"{trait_type}: {value} — {count} ({pct_str})"

    # supply unknown → show only %
    return f"{trait_type}: {value} — ({pct_str})"


async def handle_lookup(update: Update, context: ContextTypes.DEFAULT_TYPE, contract: str, label: str) -> None:
    token_id = _parse_token_id(context.args)
    if not token_id:
        await update.message.reply_text(f"Usage: /{label} <tokenId>  (example: /{label} 33)")
        return

    await update.message.chat.send_action(action="typing")

    meta, err = await fetch_nft_metadata(contract, token_id)
    if err:
        await update.message.reply_text(err)
        return
    if not isinstance(meta, dict):
        await update.message.reply_text("Unexpected response from Alchemy (metadata).")
        return

    contract_meta, cm_err = await fetch_contract_metadata(contract)
    total_supply = None
    if not cm_err and isinstance(contract_meta, dict):
        total_supply = _safe_int(contract_meta.get("totalSupply"))

    rarity, r_err = await fetch_compute_rarity(contract, token_id)
    rarity_map: Dict[Tuple[str, str], float] = {}
    if not r_err and isinstance(rarity, dict):
        rarities = rarity.get("rarities")
        if isinstance(rarities, list):
            for item in rarities:
                if not isinstance(item, dict):
                    continue
                tt = item.get("trait_type")
                vv = item.get("value")
                pp = _prevalence_to_pct(item.get("prevalence"))
                if isinstance(tt, str) and isinstance(vv, str) and pp is not None:
                    rarity_map[(tt, vv)] = pp

    name = meta.get("name") if isinstance(meta.get("name"), str) and meta.get("name").strip() else f"Token #{token_id}"
    image_url = _pick_image_url(meta)
    traits = _extract_traits(meta)

    # Token #X of Y
    of_total = ""
    if total_supply:
        of_total = f"Token: #{token_id} of {total_supply}"
    else:
        of_total = f"Token: #{token_id}"

    # Format traits
    lines: List[str] = []
    for a in traits[:MAX_TRAITS]:
        tt = a.get("trait_type") or a.get("type") or "Trait"
        vv = a.get("value")
        if not isinstance(tt, str) or vv is None:
            continue
        vv_s = str(vv)

        pct = rarity_map.get((tt, vv_s))
        lines.append(_format_trait_line(tt, vv_s, pct, total_supply))

    traits_text = "\n".join(lines) if lines else "(No traits returned.)"
    os_url = _opensea_url(contract, token_id)

    caption = (
        f"<b>{name}</b>\n"
        f"{of_total}\n\n"
        f"<b>Traits</b>\n"
        f"{traits_text}\n\n"
        f"<a href=\"{os_url}\">View on OpenSea</a>"
    )

    # Send image at top by uploading bytes (more reliable than URL-only)
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

    # Fallback text (no web preview so it doesn’t just show an OpenSea card)
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
