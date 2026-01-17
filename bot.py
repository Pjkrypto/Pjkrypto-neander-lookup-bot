import os
import re
import math
from typing import Any, Dict, Optional, Tuple, List

import httpx
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
OPENSEA_API_KEY = os.getenv("OPENSEA_API_KEY", "").strip()

CHAIN = os.getenv("CHAIN", "polygon").strip()
BROS = os.getenv("NEANDERBROS_CONTRACT", "").strip().lower()
GALS = os.getenv("NEANDERGALS_CONTRACT", "").strip().lower()

# MAX_TRAITS:
# - set high (e.g. 50+) if you truly want "all traits"
# - Telegram captions have length limits; extremely trait-heavy NFTs may need a cap
MAX_TRAITS = int(os.getenv("MAX_TRAITS", "50"))

if not TELEGRAM_BOT_TOKEN:
    raise SystemExit("Missing TELEGRAM_BOT_TOKEN")
if not OPENSEA_API_KEY:
    raise SystemExit("Missing OPENSEA_API_KEY")
if not BROS:
    raise SystemExit("Missing NEANDERBROS_CONTRACT")
if not GALS:
    raise SystemExit("Missing NEANDERGALS_CONTRACT")


def _parse_token_id(args: List[str]) -> Optional[str]:
    if not args:
        return None
    token = args[0].strip().lstrip("#")
    if not re.fullmatch(r"\d+", token):
        return None
    return token


async def _http_get_json(url: str, headers: Dict[str, str], timeout_s: int = 20) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            r = await client.get(url, headers=headers)
    except Exception as e:
        return None, f"Network error calling OpenSea: {e}"

    if r.status_code == 429:
        return None, "OpenSea throttled the request (429). Please try again in ~10–20 seconds."
    if r.status_code >= 400:
        return None, f"OpenSea error {r.status_code}: {r.text[:200]}"

    try:
        data = r.json()
    except Exception:
        return None, "Could not parse OpenSea response as JSON."
    if not isinstance(data, dict):
        return None, "Unexpected OpenSea response (not a JSON object)."
    return data, None


async def fetch_opensea_nft(contract: str, token_id: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """
    Returns (data, error_message)
    """
    url = f"https://api.opensea.io/api/v2/chain/{CHAIN}/contract/{contract}/nfts/{token_id}"
    headers = {"accept": "application/json", "x-api-key": OPENSEA_API_KEY}
    return await _http_get_json(url, headers=headers)


async def fetch_opensea_collection(contract: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """
    Best-effort collection lookup to derive total supply / stats used for rarity percentage.
    OpenSea has multiple shapes/endpoints; this may not always return supply.
    """
    url = f"https://api.opensea.io/api/v2/chain/{CHAIN}/contract/{contract}"
    headers = {"accept": "application/json", "x-api-key": OPENSEA_API_KEY}
    return await _http_get_json(url, headers=headers)


def _safe_int(v: Any) -> Optional[int]:
    try:
        if v is None:
            return None
        if isinstance(v, bool):
            return None
        if isinstance(v, int):
            return v
        if isinstance(v, float) and v.is_integer():
            return int(v)
        if isinstance(v, str) and v.strip().isdigit():
            return int(v.strip())
    except Exception:
        return None
    return None


def _safe_float(v: Any) -> Optional[float]:
    try:
        if v is None:
            return None
        if isinstance(v, bool):
            return None
        if isinstance(v, (int, float)):
            return float(v)
        if isinstance(v, str):
            s = v.strip()
            return float(s)
    except Exception:
        return None
    return None


def _format_pct(p: Optional[float]) -> str:
    if p is None:
        return ""
    # show small values with 4 decimals
    if p < 0.01:
        return f"{p*100:.4f}%"
    return f"{p*100:.2f}%"


def _extract_nft_object(data: Dict[str, Any]) -> Dict[str, Any]:
    # OpenSea v2 commonly returns {"nft": {...}}
    nft = data.get("nft")
    if isinstance(nft, dict):
        return nft
    # Sometimes API returns object directly
    return data


def _extract_collection_supply(collection_data: Dict[str, Any]) -> Optional[int]:
    """
    OpenSea collection/contract responses vary. We try multiple likely fields.
    """
    # Common candidates
    candidates = [
        collection_data.get("total_supply"),
        collection_data.get("supply"),
        collection_data.get("collection", {}).get("total_supply") if isinstance(collection_data.get("collection"), dict) else None,
        collection_data.get("collection", {}).get("stats", {}).get("count") if isinstance(collection_data.get("collection"), dict) else None,
        collection_data.get("stats", {}).get("count") if isinstance(collection_data.get("stats"), dict) else None,
        collection_data.get("nft_collection", {}).get("total_supply") if isinstance(collection_data.get("nft_collection"), dict) else None,
    ]
    for c in candidates:
        iv = _safe_int(c)
        if iv and iv > 0:
            return iv
    return None


def _extract_overall_rarity(nft: Dict[str, Any]) -> Tuple[Optional[int], Optional[float], Optional[str]]:
    """
    Try to read OpenSea-provided overall rarity fields if present.
    Returns (rank, score, label_hint)
    """
    rarity = nft.get("rarity")
    if isinstance(rarity, dict):
        rank = _safe_int(rarity.get("rank"))
        score = _safe_float(rarity.get("score"))
        # Some variants may have other keys; we keep a hint
        return rank, score, "OpenSea"

    # Some payloads may nest rarity elsewhere; try a few soft guesses
    for key in ("rarity_data", "rarityInfo", "rarity_info"):
        r2 = nft.get(key)
        if isinstance(r2, dict):
            rank = _safe_int(r2.get("rank"))
            score = _safe_float(r2.get("score") or r2.get("rarityScore"))
            if rank is not None or score is not None:
                return rank, score, "OpenSea"

    return None, None, None


def _normalize_traits(nft: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Normalize trait list across possible OpenSea shapes.
    Expected canonical keys:
      - trait_type
      - value
      - trait_count (optional)
    """
    traits = nft.get("traits") or nft.get("attributes")
    if not isinstance(traits, list):
        return []

    out: List[Dict[str, Any]] = []
    for t in traits:
        if not isinstance(t, dict):
            continue
        trait_type = t.get("trait_type") or t.get("type") or t.get("name") or "Trait"
        value = t.get("value")
        if value is None:
            continue

        trait_count = _safe_int(t.get("trait_count") or t.get("count"))
        out.append(
            {
                "trait_type": str(trait_type),
                "value": value,
                "trait_count": trait_count,
            }
        )
    return out


def _compute_rarity_score(traits: List[Dict[str, Any]], total_supply: Optional[int]) -> Tuple[Optional[float], int]:
    """
    Compute a simple rarity score from trait frequencies:
      score = Σ (1 / p_trait)
    where p_trait = trait_count / total_supply.
    If missing counts/supply, score is partial or None.

    Returns (score, traits_used_count)
    """
    if not total_supply or total_supply <= 0:
        return None, 0

    score = 0.0
    used = 0
    for t in traits:
        c = t.get("trait_count")
        if not isinstance(c, int) or c <= 0:
            continue
        p = c / float(total_supply)
        if p <= 0:
            continue
        score += (1.0 / p)
        used += 1

    if used == 0:
        return None, 0
    return score, used


def extract_fields(
    nft_data: Dict[str, Any],
    collection_data: Optional[Dict[str, Any]],
    contract: str,
    token_id: str
) -> Dict[str, Any]:
    nft = _extract_nft_object(nft_data)

    name = nft.get("name")
    if not isinstance(name, str) or not name.strip():
        name = f"Token #{token_id}"
    else:
        name = name.strip()

    # Prefer display_image_url when present
    image_url = None
    for k in ("display_image_url", "image_url", "image"):
        v = nft.get(k)
        if isinstance(v, str) and v.strip():
            image_url = v.strip()
            break

    # OpenSea item link (construct if not provided)
    opensea_url = nft.get("opensea_url")
    if not isinstance(opensea_url, str) or not opensea_url.strip():
        opensea_url = f"https://opensea.io/assets/{CHAIN}/{contract}/{token_id}"

    traits = _normalize_traits(nft)
    # Cap for message size safety
    traits_limited = traits[:MAX_TRAITS] if MAX_TRAITS > 0 else traits

    total_supply = _extract_collection_supply(collection_data) if isinstance(collection_data, dict) else None

    # Trait lines with rarity
    trait_lines: List[str] = []
    for t in traits_limited:
        tt = t["trait_type"]
        val = t["value"]
        c = t.get("trait_count")

        if isinstance(c, int) and c > 0 and total_supply and total_supply > 0:
            pct = c / float(total_supply)
            trait_lines.append(f"• {tt}: {val}  ({c}/{total_supply} = {_format_pct(pct)})")
        elif isinstance(c, int) and c > 0:
            trait_lines.append(f"• {tt}: {val}  (count: {c})")
        else:
            trait_lines.append(f"• {tt}: {val}")

    # Overall rarity: OpenSea if present, else computed
    os_rank, os_score, os_label = _extract_overall_rarity(nft)
    computed_score, computed_used = _compute_rarity_score(traits, total_supply)

    rarity_lines: List[str] = []
    if os_rank is not None or os_score is not None:
        rarity_lines.append("<b>Rarity (OpenSea)</b>")
        if os_rank is not None:
            rarity_lines.append(f"Rank: <b>#{os_rank}</b>")
        if os_score is not None:
            rarity_lines.append(f"Score: <b>{os_score:.2f}</b>")
    else:
        rarity_lines.append("<b>Rarity (Computed)</b>")
        if computed_score is not None:
            rarity_lines.append(f"Score: <b>{computed_score:.2f}</b>")
            if total_supply:
                rarity_lines.append(f"Based on {computed_used} traits with counts (supply: {total_supply}).")
            else:
                rarity_lines.append(f"Based on {computed_used} traits with counts.")
        else:
            rarity_lines.append("Not enough data from OpenSea to compute overall rarity (missing counts/supply).")

    return {
        "name": name,
        "image_url": image_url,
        "trait_lines": trait_lines,
        "opensea_url": opensea_url,
        "rarity_lines": rarity_lines,
        "total_supply": total_supply,
        "trait_count_total": len(traits),
    }


async def handle_lookup(update: Update, context: ContextTypes.DEFAULT_TYPE, contract: str, label: str) -> None:
    token_id = _parse_token_id(context.args)
    if not token_id:
        await update.message.reply_text(f"Usage: /{label} <tokenId>  (example: /{label} 33)")
        return

    await update.message.chat.send_action(action="typing")

    nft_data, err = await fetch_opensea_nft(contract, token_id)
    if err:
        await update.message.reply_text(err)
        return

    # Collection/contract info (best-effort) for supply stats used in percentages
    collection_data, coll_err = await fetch_opensea_collection(contract)
    # We do not fail the request if collection lookup fails; we just reduce rarity detail fidelity
    if coll_err:
        collection_data = {}

    fields = extract_fields(nft_data, collection_data, contract, token_id)

    title = fields["name"]
    trait_lines = fields["trait_lines"]
    rarity_lines = fields["rarity_lines"]
    opensea_url = fields["opensea_url"]

    # If MAX_TRAITS caps the list, call it out
    total_traits = fields.get("trait_count_total", 0)
    shown_traits = len(trait_lines)
    traits_note = ""
    if MAX_TRAITS > 0 and total_traits > shown_traits:
        traits_note = f"\n<i>Showing {shown_traits} of {total_traits} traits (MAX_TRAITS={MAX_TRAITS}).</i>"

    traits_block = "\n".join(trait_lines) if trait_lines else "(No traits returned by OpenSea.)"
    rarity_block = "\n".join(rarity_lines)

    caption = (
        f"<b>{title}</b>\n\n"
        f"{rarity_block}\n\n"
        f"<b>Traits</b>\n{traits_block}"
        f"{traits_note}\n\n"
        f"<a href=\"{opensea_url}\">View on OpenSea</a>"
    )

    image_url = fields["image_url"]
    if image_url:
        try:
            await update.message.reply_photo(photo=image_url, caption=caption, parse_mode=ParseMode.HTML)
            return
        except Exception:
            pass

    await update.message.reply_text(caption, parse_mode=ParseMode.HTML, disable_web_page_preview=False)


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
