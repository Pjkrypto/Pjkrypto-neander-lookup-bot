import os
import re
import time
from typing import Any, Dict, Optional, Tuple, List

import httpx
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

# -----------------------------
# Environment / config
# -----------------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
OPENSEA_API_KEY = os.getenv("OPENSEA_API_KEY", "").strip()

CHAIN = os.getenv("CHAIN", "polygon").strip()
BROS = os.getenv("NEANDERBROS_CONTRACT", "").strip().lower()
GALS = os.getenv("NEANDERGALS_CONTRACT", "").strip().lower()

# OpenSea collection slugs (default to the ones you provided)
BROS_SLUG = os.getenv("BROS_COLLECTION_SLUG", "neanderbros").strip()
GALS_SLUG = os.getenv("GALS_COLLECTION_SLUG", "neandergals").strip()

# Show up to 50 traits max (as requested)
MAX_TRAITS = int(os.getenv("MAX_TRAITS", "50"))

# Cache TTLs (seconds)
TRAITS_CACHE_TTL = int(os.getenv("TRAITS_CACHE_TTL_SECONDS", str(6 * 60 * 60)))  # 6 hours
STATS_CACHE_TTL = int(os.getenv("STATS_CACHE_TTL_SECONDS", str(30 * 60)))        # 30 minutes

if not TELEGRAM_BOT_TOKEN:
    raise SystemExit("Missing TELEGRAM_BOT_TOKEN")
if not OPENSEA_API_KEY:
    raise SystemExit("Missing OPENSEA_API_KEY")
if not BROS:
    raise SystemExit("Missing NEANDERBROS_CONTRACT")
if not GALS:
    raise SystemExit("Missing NEANDERGALS_CONTRACT")

# -----------------------------
# Simple in-memory caches
# -----------------------------
# traits_cache[slug] = (expires_at, trait_counts_map)
#   trait_counts_map: Dict[trait_type][value_str] = count_int
_traits_cache: Dict[str, Tuple[float, Dict[str, Dict[str, int]]]] = {}

# stats_cache[slug] = (expires_at, total_supply_int)
_stats_cache: Dict[str, Tuple[float, int]] = {}


def _now() -> float:
    return time.time()


def _parse_token_id(args: List[str]) -> Optional[str]:
    if not args:
        return None
    token = args[0].strip().lstrip("#")
    if not re.fullmatch(r"\d+", token):
        return None
    return token


def _safe_int(v: Any) -> Optional[int]:
    try:
        if v is None or isinstance(v, bool):
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
        if v is None or isinstance(v, bool):
            return None
        if isinstance(v, (int, float)):
            return float(v)
        if isinstance(v, str):
            return float(v.strip())
    except Exception:
        return None
    return None


def _format_pct(p: Optional[float]) -> str:
    if p is None:
        return "n/a"
    # small values: more precision
    if p < 0.01:
        return f"{p * 100:.4f}%"
    return f"{p * 100:.2f}%"


async def _http_get_json(url: str, headers: Dict[str, str], timeout_s: int = 25) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
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
    url = f"https://api.opensea.io/api/v2/chain/{CHAIN}/contract/{contract}/nfts/{token_id}"
    headers = {"accept": "application/json", "x-api-key": OPENSEA_API_KEY}
    return await _http_get_json(url, headers=headers)


async def fetch_collection_traits(slug: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    # OpenSea docs: GET https://api.opensea.io/api/v2/traits/{slug} :contentReference[oaicite:2]{index=2}
    url = f"https://api.opensea.io/api/v2/traits/{slug}"
    headers = {"accept": "application/json", "x-api-key": OPENSEA_API_KEY}
    return await _http_get_json(url, headers=headers)


async def fetch_collection_stats(slug: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    # OpenSea docs: GET https://api.opensea.io/api/v2/collections/{slug}/stats :contentReference[oaicite:3]{index=3}
    url = f"https://api.opensea.io/api/v2/collections/{slug}/stats"
    headers = {"accept": "application/json", "x-api-key": OPENSEA_API_KEY}
    return await _http_get_json(url, headers=headers)


def _extract_nft_object(data: Dict[str, Any]) -> Dict[str, Any]:
    nft = data.get("nft")
    if isinstance(nft, dict):
        return nft
    return data


def _extract_overall_rarity(nft: Dict[str, Any]) -> Tuple[Optional[int], Optional[float]]:
    rarity = nft.get("rarity")
    if isinstance(rarity, dict):
        return _safe_int(rarity.get("rank")), _safe_float(rarity.get("score"))

    # tolerant fallback keys
    for key in ("rarity_data", "rarityInfo", "rarity_info"):
        r2 = nft.get(key)
        if isinstance(r2, dict):
            rank = _safe_int(r2.get("rank"))
            score = _safe_float(r2.get("score") or r2.get("rarityScore"))
            if rank is not None or score is not None:
                return rank, score

    return None, None


def _normalize_traits(nft: Dict[str, Any]) -> List[Dict[str, Any]]:
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
        out.append({"trait_type": str(trait_type), "value": value, "trait_count": trait_count})
    return out


def _parse_traits_response_to_counts(traits_payload: Dict[str, Any]) -> Dict[str, Dict[str, int]]:
    """
    Convert OpenSea collection traits payload into:
      counts[trait_type][value_str] = count_int

    Payload shapes can vary; we handle common variants:
    - {"traits": {"Eyes": {"Blue": {"count": 42}, "Green": {"count": 12}}, ...}}
    - {"traits": {"Eyes": {"Blue": 42, "Green": 12}, ...}}
    - {"traits": {"Eyes": [{"value":"Blue","count":42}, ...], ...}}
    """
    traits = traits_payload.get("traits")
    if not isinstance(traits, dict):
        return {}

    out: Dict[str, Dict[str, int]] = {}

    for trait_type, values_obj in traits.items():
        if not isinstance(trait_type, str):
            continue

        # Case 1: dict of values -> counts/objects
        if isinstance(values_obj, dict):
            tmap: Dict[str, int] = {}
            for val, vinfo in values_obj.items():
                val_str = str(val)
                if isinstance(vinfo, int):
                    tmap[val_str] = vinfo
                elif isinstance(vinfo, dict):
                    c = _safe_int(vinfo.get("count") or vinfo.get("trait_count") or vinfo.get("value_count"))
                    if c is not None:
                        tmap[val_str] = c
                else:
                    # ignore unknown
                    pass
            if tmap:
                out[trait_type] = tmap

        # Case 2: list of {"value":..., "count":...}
        elif isinstance(values_obj, list):
            tmap2: Dict[str, int] = {}
            for item in values_obj:
                if not isinstance(item, dict):
                    continue
                v = item.get("value")
                c = _safe_int(item.get("count") or item.get("trait_count") or item.get("value_count"))
                if v is not None and c is not None:
                    tmap2[str(v)] = c
            if tmap2:
                out[trait_type] = tmap2

    return out


def _extract_supply_from_stats(stats_payload: Dict[str, Any]) -> Optional[int]:
    """
    OpenSea stats payload shapes vary. We try common candidates:
    - {"total": {"count": 5555, ...}}
    - {"stats": {"count": 5555, ...}}
    - {"count": 5555}
    """
    candidates = []

    total = stats_payload.get("total")
    if isinstance(total, dict):
        candidates.extend([total.get("count"), total.get("items"), total.get("total_supply")])

    stats = stats_payload.get("stats")
    if isinstance(stats, dict):
        candidates.extend([stats.get("count"), stats.get("items"), stats.get("total_supply")])

    candidates.append(stats_payload.get("count"))
    candidates.append(stats_payload.get("items"))
    candidates.append(stats_payload.get("total_supply"))

    for c in candidates:
        iv = _safe_int(c)
        if iv and iv > 0:
            return iv
    return None


async def get_cached_trait_counts(slug: str) -> Dict[str, Dict[str, int]]:
    now = _now()
    cached = _traits_cache.get(slug)
    if cached and cached[0] > now:
        return cached[1]

    payload, err = await fetch_collection_traits(slug)
    if err or not payload:
        # cache short negative to avoid rapid retry storms
        _traits_cache[slug] = (now + 60, {})
        return {}

    counts = _parse_traits_response_to_counts(payload)
    _traits_cache[slug] = (now + TRAITS_CACHE_TTL, counts)
    return counts


async def get_cached_supply(slug: str) -> Optional[int]:
    now = _now()
    cached = _stats_cache.get(slug)
    if cached and cached[0] > now:
        return cached[1]

    payload, err = await fetch_collection_stats(slug)
    if err or not payload:
        _stats_cache[slug] = (now + 60, 0)
        return None

    supply = _extract_supply_from_stats(payload)
    if supply and supply > 0:
        _stats_cache[slug] = (now + STATS_CACHE_TTL, supply)
        return supply

    _stats_cache[slug] = (now + 60, 0)
    return None


def _slug_for_contract(contract: str) -> Optional[str]:
    c = (contract or "").lower()
    if c == BROS:
        return BROS_SLUG
    if c == GALS:
        return GALS_SLUG
    return None


def extract_fields(
    nft_data: Dict[str, Any],
    contract: str,
    token_id: str,
    supply: Optional[int],
    trait_counts: Dict[str, Dict[str, int]],
) -> Dict[str, Any]:
    nft = _extract_nft_object(nft_data)

    # Title/name
    name = nft.get("name")
    if not isinstance(name, str) or not name.strip():
        name = f"Token #{token_id}"
    else:
        name = name.strip()

    # Image URL (best effort)
    image_url = None
    for k in ("display_image_url", "image_url", "image"):
        v = nft.get(k)
        if isinstance(v, str) and v.strip():
            image_url = v.strip()
            break

    # OpenSea item link
    opensea_url = nft.get("opensea_url")
    if not isinstance(opensea_url, str) or not opensea_url.strip():
        opensea_url = f"https://opensea.io/assets/{CHAIN}/{contract}/{token_id}"

    # Overall rarity (OpenSea rank/score if provided)
    os_rank, os_score = _extract_overall_rarity(nft)
    rarity_lines: List[str] = ["<b>Rarity (OpenSea)</b>"]
    if os_rank is not None:
        rarity_lines.append(f"Rank: <b>#{os_rank}</b>")
    if os_score is not None:
        rarity_lines.append(f"Score: <b>{os_score:.2f}</b>")
    if os_rank is None and os_score is None:
        rarity_lines.append("Rank/score not provided by OpenSea for this item.")

    # Minted out of total (best-effort)
    minted_line = None
    if supply and supply > 0:
        minted_line = f"<b>Minted</b>: <b>#{token_id}</b> of <b>{supply}</b>"

    # Traits (cap to MAX_TRAITS)
    traits_all = _normalize_traits(nft)
    traits = traits_all[:MAX_TRAITS] if MAX_TRAITS > 0 else traits_all

    trait_lines: List[str] = []
    for t in traits:
        tt = t["trait_type"]
        val = t["value"]
        val_str = str(val)

        # Prefer per-NFT trait_count if present; otherwise use collection traits endpoint counts
        c = t.get("trait_count")
        if not (isinstance(c, int) and c > 0):
            c = trait_counts.get(tt, {}).get(val_str)

        if isinstance(c, int) and c > 0 and supply and supply > 0:
            p = c / float(supply)
            # EXACT format requested:
            trait_lines.append(f"{tt}: {val_str} — {c} ({_format_pct(p)})")
        elif isinstance(c, int) and c > 0:
            trait_lines.append(f"{tt}: {val_str} — {c} (n/a)")
        else:
            trait_lines.append(f"{tt}: {val_str} — n/a")

    traits_note = ""
    if MAX_TRAITS > 0 and len(traits_all) > len(traits):
        traits_note = f"\n<i>Showing {len(traits)} of {len(traits_all)} traits (MAX_TRAITS={MAX_TRAITS}).</i>"

    return {
        "name": name,
        "image_url": image_url,
        "opensea_url": opensea_url,
        "minted_line": minted_line,
        "rarity_lines": rarity_lines,
        "trait_lines": trait_lines,
        "traits_note": traits_note,
    }


async def handle_lookup(update: Update, context: ContextTypes.DEFAULT_TYPE, contract: str, label: str) -> None:
    token_id = _parse_token_id(context.args)
    if not token_id:
        await update.message.reply_text(f"Usage: /{label} <tokenId>  (example: /{label} 33)")
        return

    await update.message.chat.send_action(action="typing")

    slug = _slug_for_contract(contract)

    nft_data, err = await fetch_opensea_nft(contract, token_id)
    if err:
        await update.message.reply_text(err)
        return

    # Best-effort enrichment (do not fail the whole request if these error)
    supply: Optional[int] = None
    trait_counts: Dict[str, Dict[str, int]] = {}
    if slug:
        supply = await get_cached_supply(slug)
        trait_counts = await get_cached_trait_counts(slug)

    fields = extract_fields(
        nft_data=nft_data,
        contract=contract,
        token_id=token_id,
        supply=supply,
        trait_counts=trait_counts,
    )

    header_lines = [f"<b>{fields['name']}</b>"]
    if fields["minted_line"]:
        header_lines.append(fields["minted_line"])

    rarity_block = "\n".join(fields["rarity_lines"])
    traits_block = "\n".join(fields["trait_lines"]) if fields["trait_lines"] else "(No traits returned by OpenSea.)"

    caption = (
        "\n".join(header_lines)
        + "\n\n"
        + rarity_block
        + "\n\n"
        + "<b>Traits</b>\n"
        + traits_block
        + fields["traits_note"]
        + f"\n\n<a href=\"{fields['opensea_url']}\">View on OpenSea</a>"
    )

    # Send image at top (preferred)
    image_url = fields["image_url"]
    if image_url:
        try:
            await update.message.reply_photo(photo=image_url, caption=caption, parse_mode=ParseMode.HTML)
            return
        except Exception:
            # Telegram can reject some URLs; fallback to text
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
