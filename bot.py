import os
import re
import io
import asyncio
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx
import discord
from PIL import Image, ImageOps, UnidentifiedImageError
from discord import app_commands
from telegram import Update, InputFile
from telegram.constants import ParseMode
from telegram.error import BadRequest
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

# Metadata retry protection. OpenSea is the primary source; Alchemy is an
# optional final fallback for older/cached items.
METADATA_FETCH_RETRIES = int(os.getenv("METADATA_FETCH_RETRIES", "3"))
METADATA_RETRY_SECONDS = float(os.getenv("METADATA_RETRY_SECONDS", "2"))

# Alchemy
ALCHEMY_API_KEY = os.getenv("ALCHEMY_API_KEY", "").strip()
ALCHEMY_NETWORK = os.getenv("ALCHEMY_NETWORK", "polygon-mainnet").strip()
ALCHEMY_BASE_URL = os.getenv("ALCHEMY_BASE_URL", "").strip().rstrip("/")

# OpenSea
OPENSEA_API_KEY = os.getenv("OPENSEA_API_KEY", "").strip()
OPENSEA_TRAIT_CACHE_SECONDS = int(os.getenv("OPENSEA_TRAIT_CACHE_SECONDS", "900"))

# Discord lookup bot
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "").strip()
DISCORD_APPLICATION_ID = os.getenv("DISCORD_APPLICATION_ID", "").strip()
DISCORD_GUILD_ID_RAW = os.getenv("DISCORD_GUILD_ID", "").strip()
DISCORD_CHANNEL_ID_RAW = os.getenv("DISCORD_CHANNEL_ID", "").strip()

try:
    DISCORD_GUILD_ID = int(DISCORD_GUILD_ID_RAW) if DISCORD_GUILD_ID_RAW else 0
except ValueError:
    DISCORD_GUILD_ID = 0

try:
    DISCORD_CHANNEL_ID = int(DISCORD_CHANNEL_ID_RAW) if DISCORD_CHANNEL_ID_RAW else 0
except ValueError:
    DISCORD_CHANNEL_ID = 0

DISCORD_ENABLED = bool(
    DISCORD_BOT_TOKEN and DISCORD_GUILD_ID and DISCORD_CHANNEL_ID
)

# Bros have no token 0 (per your known info)
BROS_MIN_TOKEN_ID = int(os.getenv("BROS_MIN_TOKEN_ID", "1"))
GALS_MIN_TOKEN_ID = int(os.getenv("GALS_MIN_TOKEN_ID", "0"))

if not TELEGRAM_BOT_TOKEN:
    raise SystemExit("Missing TELEGRAM_BOT_TOKEN")
if not OPENSEA_API_KEY:
    raise SystemExit("Missing OPENSEA_API_KEY")
if not BROS:
    raise SystemExit("Missing NEANDERBROS_CONTRACT")
if not GALS:
    raise SystemExit("Missing NEANDERGALS_CONTRACT")

if not DISCORD_ENABLED:
    print(
        "Discord lookup disabled: set DISCORD_BOT_TOKEN, "
        "DISCORD_GUILD_ID, and DISCORD_CHANNEL_ID to enable /bro and /gal.",
        flush=True,
    )


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
async def fetch_nft_metadata_alchemy(
    contract: str,
    token_id: int,
    refresh_cache: bool = False,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    url = f"{_alchemy_root()}/getNFTMetadata"
    params = {
        "contractAddress": contract,
        "tokenId": str(token_id),
        "refreshCache": "true" if refresh_cache else "false",
    }
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
        return f"{trait_type}: {value} — {count:,} ({pct_str})"
    return f"{trait_type}: {value} — ({pct_str})"


def _format_trait_count_line(
    trait_type: str,
    value: str,
    count: int,
    minted_so_far: Optional[int],
) -> str:
    if minted_so_far and minted_so_far > 0:
        pct = (count / float(minted_so_far)) * 100.0
        return f"{trait_type}: {value} — {count:,} ({pct:.2f}%)"
    return f"{trait_type}: {value} — {count:,}"


def _normalize_media_url(url: str) -> str:
    value = (url or "").strip()
    if value.startswith("ipfs://"):
        return "https://ipfs.io/ipfs/" + value[len("ipfs://"):]
    return value


async def _download_image_bytes(url: str) -> Optional[bytes]:
    media_url = _normalize_media_url(url)
    if not media_url:
        return None

    try:
        async with httpx.AsyncClient(timeout=25, follow_redirects=True) as client:
            r = await client.get(
                media_url,
                headers={"user-agent": "Mozilla/5.0 (compatible; NeanderLookupBot/1.0)"},
            )
            r.raise_for_status()
            if not r.content:
                print(f"Image download returned empty body: {media_url[:180]}", flush=True)
                return None
            return r.content
    except Exception as e:
        print(f"Image download failed: {type(e).__name__}: {e}", flush=True)
        return None


def _prepare_telegram_photo(image_bytes: bytes) -> Optional[bytes]:
    """Convert OpenSea/CDN formats such as WebP or GIF to Telegram-safe PNG."""
    try:
        with Image.open(io.BytesIO(image_bytes)) as source:
            source.seek(0)
            image = ImageOps.exif_transpose(source.copy())
            image.thumbnail((4096, 4096), Image.Resampling.LANCZOS)
            if image.mode not in ("RGB", "RGBA"):
                image = image.convert("RGBA")

            output = io.BytesIO()
            image.save(output, format="PNG", optimize=True)
            converted = output.getvalue()

            # Telegram photos are limited to 10 MB. Use JPEG as a compact
            # fallback for an unusually large decoded PNG.
            if len(converted) > 9_500_000:
                if image.mode == "RGBA":
                    background = Image.new("RGB", image.size, "black")
                    background.paste(image, mask=image.getchannel("A"))
                    image = background
                else:
                    image = image.convert("RGB")
                output = io.BytesIO()
                image.save(output, format="JPEG", quality=92, optimize=True)
                converted = output.getvalue()

            return converted if converted else None
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        print(
            f"Telegram image conversion failed: {type(exc).__name__}: {exc}",
            flush=True,
        )
        return None


# -----------------------
# OPENSEA CALLS (RANK + COLLECTION TRAIT COUNTS)
# -----------------------
def _opensea_headers() -> Dict[str, str]:
    return {
        "accept": "application/json",
        "x-api-key": OPENSEA_API_KEY,
        "user-agent": "Mozilla/5.0 (compatible; NeanderLookupBot/1.0)",
    }


def _extract_opensea_nft(data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    nft = data.get("nft") if isinstance(data, dict) else None
    if isinstance(nft, dict):
        return nft
    return data if isinstance(data, dict) else None


def _extract_opensea_collection_slug(nft: Dict[str, Any]) -> Optional[str]:
    collection = nft.get("collection")
    if isinstance(collection, str) and collection.strip():
        return collection.strip()
    if isinstance(collection, dict):
        for key in ("slug", "collection", "collection_slug"):
            value = collection.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

    for key in ("collection_slug", "collectionSlug"):
        value = nft.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _extract_opensea_traits(nft: Dict[str, Any]) -> List[Dict[str, Any]]:
    traits = nft.get("traits")
    if not isinstance(traits, list):
        return []

    out: List[Dict[str, Any]] = []
    for trait in traits:
        if not isinstance(trait, dict):
            continue
        trait_type = (
            trait.get("trait_type")
            or trait.get("traitType")
            or trait.get("type")
        )
        value = trait.get("value")
        if trait_type is not None and value is not None:
            out.append({"trait_type": str(trait_type), "value": value})
    return out


def _pick_opensea_image_url(nft: Dict[str, Any]) -> Optional[str]:
    for key in (
        "display_image_url",
        "image_url",
        "original_image_url",
        "cached_image_url",
    ):
        value = nft.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _pick_opensea_metadata_url(nft: Dict[str, Any]) -> Optional[str]:
    for key in ("metadata_url", "token_metadata", "token_uri"):
        value = nft.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


async def fetch_opensea_nft(
    contract: str,
    token_id: int,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    url = f"https://api.opensea.io/api/v2/chain/{CHAIN}/contract/{contract}/nfts/{token_id}"
    data, err = await _get_json(
        url,
        params=None,
        headers=_opensea_headers(),
        timeout=25.0,
    )
    if err:
        return None, err

    nft = _extract_opensea_nft(data or {})
    if not isinstance(nft, dict):
        return None, "Unexpected OpenSea response."
    return nft, None


async def fetch_token_metadata_url(
    metadata_url: str,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    url = _normalize_media_url(metadata_url)
    if not url:
        return None, "NFT metadata URL is unavailable."
    return await _get_json(
        url,
        params=None,
        headers={"user-agent": "Mozilla/5.0 (compatible; NeanderLookupBot/1.0)"},
        timeout=25.0,
    )


async def fetch_opensea_metadata(
    contract: str,
    token_id: int,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    url = f"https://api.opensea.io/api/v2/metadata/{CHAIN}/{contract}/{token_id}"
    return await _get_json(
        url,
        params=None,
        headers=_opensea_headers(),
        timeout=25.0,
    )


async def fetch_opensea_total_supply(
    contract: str,
    token_id: int,
) -> Tuple[Optional[int], Optional[str]]:
    url = f"https://api.opensea.io/api/v2/chain/{CHAIN}/contract/{contract}/nfts/{token_id}/collection"
    data, err = await _get_json(
        url,
        params=None,
        headers=_opensea_headers(),
        timeout=25.0,
    )
    if err:
        return None, err
    return _safe_int((data or {}).get("total_supply")), None


async def fetch_opensea_rank_and_collection(
    contract: str,
    token_id: int,
) -> Tuple[Optional[int], Optional[str], Optional[str]]:
    nft, err = await fetch_opensea_nft(contract, token_id)
    if err or not isinstance(nft, dict):
        return None, None, err or "Unexpected OpenSea response."

    rank: Optional[int] = None
    rarity = nft.get("rarity")
    if isinstance(rarity, dict):
        rank = _safe_int(rarity.get("rank"))

    if rank is None:
        rank = _safe_int(nft.get("rarity_rank") or nft.get("rarityRank"))

    return rank, _extract_opensea_collection_slug(nft), None


def _count_from_opensea_value(value: Any) -> Optional[int]:
    direct = _safe_int(value)
    if direct is not None:
        return direct
    if isinstance(value, dict):
        for key in ("count", "total", "quantity", "value_count"):
            parsed = _safe_int(value.get(key))
            if parsed is not None:
                return parsed
    return None


def _build_trait_count_map_from_opensea(
    traits_resp: Dict[str, Any],
) -> Dict[Tuple[str, str], int]:
    out: Dict[Tuple[str, str], int] = {}

    # Current OpenSea V2 traits response exposes categorical value counts under
    # counts[trait_type][trait_value]. Keep the parser permissive because the
    # API has used more than one wrapper shape over time.
    counts = traits_resp.get("counts")
    if isinstance(counts, dict):
        for trait_type, values in counts.items():
            if not isinstance(values, dict):
                continue
            for trait_value, raw_count in values.items():
                count = _count_from_opensea_value(raw_count)
                if count is None:
                    continue
                out[(_norm(trait_type), _norm(trait_value))] = count

    # Alternate/wrapped shapes: categories may contain a values/counts object.
    categories = traits_resp.get("categories")
    if isinstance(categories, dict):
        for trait_type, category in categories.items():
            if not isinstance(category, dict):
                continue
            values = category.get("values") or category.get("counts")
            if not isinstance(values, dict):
                continue
            for trait_value, raw_count in values.items():
                count = _count_from_opensea_value(raw_count)
                if count is None:
                    continue
                out[(_norm(trait_type), _norm(trait_value))] = count

    # Also accept list-style trait payloads if OpenSea returns them.
    traits = traits_resp.get("traits")
    if isinstance(traits, list):
        for trait in traits:
            if not isinstance(trait, dict):
                continue
            trait_type = (
                trait.get("trait_type")
                or trait.get("traitType")
                or trait.get("type")
                or trait.get("name")
            )
            values = trait.get("values") or trait.get("counts")
            if trait_type is None or not isinstance(values, (list, dict)):
                continue

            if isinstance(values, dict):
                iterable = values.items()
            else:
                iterable = []
                for item in values:
                    if not isinstance(item, dict):
                        continue
                    trait_value = item.get("value") or item.get("name")
                    if trait_value is not None:
                        iterable.append((trait_value, item))

            for trait_value, raw_count in iterable:
                count = _count_from_opensea_value(raw_count)
                if count is None:
                    continue
                out[(_norm(trait_type), _norm(trait_value))] = count

    return out


_OPENSEA_TRAIT_CACHE: Dict[
    str,
    Tuple[float, Dict[Tuple[str, str], int]],
] = {}


async def fetch_opensea_trait_counts(
    collection_slug: str,
) -> Tuple[Dict[Tuple[str, str], int], Optional[str]]:
    slug = (collection_slug or "").strip()
    if not slug:
        return {}, "OpenSea collection slug is unavailable."

    now = time.monotonic()
    cached = _OPENSEA_TRAIT_CACHE.get(slug)
    if cached is not None:
        cached_at, cached_map = cached
        if now - cached_at < max(0, OPENSEA_TRAIT_CACHE_SECONDS):
            return cached_map, None

    url = f"https://api.opensea.io/api/v2/traits/{slug}"
    data, err = await _get_json(
        url,
        params=None,
        headers=_opensea_headers(),
        timeout=25.0,
    )
    if err:
        return {}, err

    count_map = _build_trait_count_map_from_opensea(data or {})
    if not count_map:
        return {}, "OpenSea trait response contained no categorical value counts."

    _OPENSEA_TRAIT_CACHE[slug] = (now, count_map)
    return count_map, None


# -----------------------
# MESSAGE BUILD
# -----------------------
async def build_nft_message(contract: str, token_id: int) -> Tuple[Optional[str], Optional[bytes], Optional[str]]:
    meta: Optional[Dict[str, Any]] = None
    traits: List[Dict[str, Any]] = []
    image_url: Optional[str] = None
    img_bytes: Optional[bytes] = None
    last_error: Optional[str] = None
    opensea_nft: Optional[Dict[str, Any]] = None

    attempts = max(1, METADATA_FETCH_RETRIES)

    # OpenSea is the primary NFT metadata source. If its item response has not
    # indexed the attributes yet, follow its metadata URL and read the token
    # JSON directly from IPFS.
    for attempt in range(attempts):
        opensea_nft, err = await fetch_opensea_nft(contract, token_id)
        if err or not isinstance(opensea_nft, dict):
            last_error = err or "OpenSea metadata not available."
            print(
                f"OpenSea metadata fetch failed for tokenId={token_id} "
                f"attempt={attempt + 1}/{attempts}: {last_error}",
                flush=True,
            )
        else:
            traits = _extract_opensea_traits(opensea_nft)
            image_url = _pick_opensea_image_url(opensea_nft)

            if not traits or not image_url:
                detailed_meta, detailed_err = await fetch_opensea_metadata(
                    contract,
                    token_id,
                )
                if isinstance(detailed_meta, dict):
                    detailed_traits = detailed_meta.get("traits")
                    if not traits and isinstance(detailed_traits, list):
                        traits = [a for a in detailed_traits if isinstance(a, dict)]
                    detailed_image = detailed_meta.get("image")
                    if not image_url and isinstance(detailed_image, str):
                        image_url = detailed_image.strip()
                elif detailed_err:
                    last_error = detailed_err

            metadata_url = _pick_opensea_metadata_url(opensea_nft)
            if (not traits or not image_url) and metadata_url:
                direct_meta, direct_err = await fetch_token_metadata_url(metadata_url)
                if isinstance(direct_meta, dict):
                    attrs = direct_meta.get("attributes")
                    if not traits and isinstance(attrs, list):
                        traits = [a for a in attrs if isinstance(a, dict)]
                    direct_image = direct_meta.get("image") or direct_meta.get("image_url")
                    if not image_url and isinstance(direct_image, str):
                        image_url = direct_image.strip()
                elif direct_err:
                    last_error = direct_err

            if traits and image_url:
                img_bytes = await _download_image_bytes(image_url)
                if img_bytes:
                    meta = opensea_nft
                    break
                last_error = f"OpenSea image download failed for tokenId={token_id}"
            else:
                last_error = f"Incomplete OpenSea metadata: traits={len(traits)} image={bool(image_url)}"
                print(
                    f"Incomplete OpenSea metadata for tokenId={token_id} "
                    f"attempt={attempt + 1}/{attempts}: "
                    f"traits={len(traits)} image={bool(image_url)}",
                    flush=True,
                )

        if attempt + 1 < attempts:
            await asyncio.sleep(max(0.0, METADATA_RETRY_SECONDS))

    # Optional legacy fallback. Alchemy can help when OpenSea is briefly behind,
    # but it can no longer prevent the OpenSea/direct-IPFS path from running.
    if (not traits or not image_url or not img_bytes) and ALCHEMY_API_KEY:
        for attempt in range(attempts):
            alchemy_meta, err = await fetch_nft_metadata_alchemy(
                contract,
                token_id,
                refresh_cache=attempt > 0,
            )
            if isinstance(alchemy_meta, dict) and not err:
                traits = _extract_traits(alchemy_meta)
                image_url = _pick_image_url(alchemy_meta)
                if traits and image_url:
                    img_bytes = await _download_image_bytes(image_url)
                    if img_bytes:
                        meta = alchemy_meta
                        break
            if attempt + 1 < attempts:
                await asyncio.sleep(max(0.0, METADATA_RETRY_SECONDS))

    if not isinstance(meta, dict) or not traits or not image_url or not img_bytes:
        return (
            None,
            None,
            "NFT metadata is temporarily unavailable. Please try this command again in a few seconds."
        )

    minted_so_far, _ = await fetch_opensea_total_supply(contract, token_id)
    if minted_so_far is None and ALCHEMY_API_KEY:
        minted_so_far, _ = await fetch_total_supply_alchemy(contract)

    if isinstance(opensea_nft, dict):
        rarity = opensea_nft.get("rarity")
        os_rank = _safe_int(rarity.get("rank")) if isinstance(rarity, dict) else None
        if os_rank is None:
            os_rank = _safe_int(opensea_nft.get("rarity_rank") or opensea_nft.get("rarityRank"))
        os_collection_slug = _extract_opensea_collection_slug(opensea_nft)
        os_rank_err = None
    else:
        os_rank, os_collection_slug, os_rank_err = await fetch_opensea_rank_and_collection(
            contract,
            token_id,
        )
        if os_rank_err:
            os_rank = None

    # Prefer OpenSea's collection-level trait counts. This keeps the displayed
    # trait rarity aligned with the marketplace source used for the rank.
    trait_count_map: Dict[Tuple[str, str], int] = {}
    os_traits_err: Optional[str] = None
    if os_collection_slug:
        trait_count_map, os_traits_err = await fetch_opensea_trait_counts(
            os_collection_slug
        )
    else:
        os_traits_err = "OpenSea NFT response did not include a collection slug."

    # Temporary fallback while Alchemy computeRarity is still available.
    trait_pct_map: Dict[Tuple[str, str], float] = {}
    if not trait_count_map and ALCHEMY_API_KEY:
        rarity_resp, r_err = await fetch_compute_rarity_alchemy(contract, token_id)
        if not r_err and isinstance(rarity_resp, dict):
            trait_pct_map = _build_trait_pct_map_from_alchemy(rarity_resp)

        if os_traits_err:
            print(
                f"OpenSea trait counts unavailable for tokenId={token_id}: "
                f"{os_traits_err}",
                flush=True,
            )

    coll = _collection_label(contract)
    nft_id = _display_nft_id(contract, token_id)

    header1 = f"<b>{coll} NFT ID #{nft_id}</b>"
    header2 = f"Token ID #{token_id}"
    if minted_so_far and minted_so_far > 0:
        header2 += f" of {minted_so_far}"

    trait_lines: List[str] = []
    for a in traits[:MAX_TRAITS]:
        tt = a.get("trait_type") or a.get("type") or a.get("traitType") or "Trait"
        vv = a.get("value")
        if not isinstance(tt, str) or vv is None:
            continue
        vv_s = str(vv)
        trait_key = (_norm(tt), _norm(vv_s))
        count = trait_count_map.get(trait_key)
        if count is not None:
            trait_lines.append(
                _format_trait_count_line(tt, vv_s, count, minted_so_far)
            )
        else:
            pct = trait_pct_map.get(trait_key)
            trait_lines.append(_format_trait_line(tt, vv_s, pct, minted_so_far))

    if not trait_lines:
        return (
            None,
            None,
            "NFT metadata is temporarily unavailable. Please try this command again in a few seconds."
        )

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
        f"{'\n'.join(trait_lines)}\n\n"
        f"<a href=\"{os_url}\">View on OpenSea</a>"
    )

    return caption, img_bytes, None


# -----------------------
# DISCORD
# -----------------------
def _discord_text_from_telegram_html(text: str) -> str:
    value = text or ""

    value = re.sub(
        r'<a\s+href="([^"]+)">([^<]+)</a>',
        lambda m: f"[{m.group(2)}]({m.group(1)})",
        value,
        flags=re.IGNORECASE,
    )

    value = re.sub(
        r"<b>(.*?)</b>",
        r"**\1**",
        value,
        flags=re.IGNORECASE | re.DOTALL,
    )

    value = re.sub(r"<[^>]+>", "", value)
    return value.strip()


discord_intents = discord.Intents.none()
discord_intents.guilds = True
discord_client = discord.Client(intents=discord_intents)
discord_tree = app_commands.CommandTree(discord_client)
_discord_synced = False


async def _discord_lookup(
    interaction: discord.Interaction,
    contract: str,
    token_id: int,
    label: str,
    min_id: int,
) -> None:
    if interaction.channel_id != DISCORD_CHANNEL_ID:
        await interaction.response.send_message(
            "Please use this lookup command in the NeanderBros general-chat channel.",
            ephemeral=True,
        )
        return

    if token_id < min_id:
        await interaction.response.send_message(
            f"{label} Token ID must be >= {min_id}.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(thinking=True)

    caption, img_bytes, err = await build_nft_message(contract, token_id)

    if err:
        await interaction.followup.send(err, ephemeral=True)
        return

    embed = discord.Embed(
        title=f"{_collection_label(contract)} Lookup",
        description=_discord_text_from_telegram_html(caption or ""),
    )

    if img_bytes:
        bio = io.BytesIO(img_bytes)
        bio.seek(0)
        file = discord.File(bio, filename="nft.png")
        embed.set_image(url="attachment://nft.png")
        await interaction.followup.send(embed=embed, file=file)
    else:
        await interaction.followup.send(embed=embed)


async def discord_bro_cmd(
    interaction: discord.Interaction,
    number: int,
) -> None:
    # Commands always use the canonical on-chain token ID.
    token_id = number
    await _discord_lookup(
        interaction,
        BROS,
        token_id,
        "NeanderBro",
        BROS_MIN_TOKEN_ID,
    )


async def discord_gal_cmd(
    interaction: discord.Interaction,
    number: int,
) -> None:
    # Gals display the raw token ID, so no offset is applied.
    token_id = number - DISPLAY_ID_OFFSETS.get(GALS, 0)
    await _discord_lookup(
        interaction,
        GALS,
        token_id,
        "NeanderGal",
        GALS_MIN_TOKEN_ID,
    )


if DISCORD_ENABLED:
    guild_object = discord.Object(id=DISCORD_GUILD_ID)

    discord_bro_cmd = app_commands.describe(
        number="NeanderBro on-chain Token ID, e.g. 1385"
    )(discord_bro_cmd)
    discord_gal_cmd = app_commands.describe(
        number="NeanderGal on-chain Token ID, e.g. 91"
    )(discord_gal_cmd)

    discord_tree.command(
        name="bro",
        description="Look up a NeanderBro NFT",
        guild=guild_object,
    )(discord_bro_cmd)

    discord_tree.command(
        name="gal",
        description="Look up a NeanderGal NFT",
        guild=guild_object,
    )(discord_gal_cmd)


@discord_client.event
async def on_ready() -> None:
    global _discord_synced

    print(
        f"Discord connected as {discord_client.user} "
        f"(guild={DISCORD_GUILD_ID}, channel={DISCORD_CHANNEL_ID})",
        flush=True,
    )

    if not DISCORD_ENABLED or _discord_synced:
        return

    try:
        guild_object = discord.Object(id=DISCORD_GUILD_ID)
        synced = await discord_tree.sync(guild=guild_object)
        _discord_synced = True
        print(
            "Discord guild commands synced: "
            + (", ".join("/" + cmd.name for cmd in synced) or "none"),
            flush=True,
        )
    except Exception as exc:
        print(f"Discord command sync failed: {exc}", flush=True)


# -----------------------
# HANDLERS
# -----------------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Ready. Use /bro <Token ID> or /gal <Token ID>.")


async def _handle_lookup(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    contract: str,
    label: str,
    min_id: int,
    display_offset: int = 0,
) -> None:
    entered_number = _parse_token_id(context.args)
    if entered_number is None:
        await update.message.reply_text(f"Usage: /{label} <Token ID>  (example: /{label} 33)")
        return
    token_id = entered_number - display_offset
    if token_id < min_id:
        minimum_token_id = min_id + display_offset
        await update.message.reply_text(f"{label.upper()} Token ID must be >= {minimum_token_id}.")
        return

    await update.message.chat.send_action(action="typing")

    caption, img_bytes, err = await build_nft_message(contract, token_id)
    if err:
        await update.message.reply_text(err)
        return

    telegram_photo = _prepare_telegram_photo(img_bytes) if img_bytes else None
    if telegram_photo:
        bio = io.BytesIO(telegram_photo)
        bio.name = "nft.png"
        try:
            await update.message.reply_photo(
                photo=InputFile(bio),
                caption=caption,
                parse_mode=ParseMode.HTML,
            )
            return
        except BadRequest as exc:
            print(f"Telegram rejected converted NFT image: {exc}", flush=True)

    # Always return the lookup details even when Telegram rejects a marketplace
    # image. Discord can continue using the original image bytes.
    await update.message.reply_text(
        caption,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=False,
    )


async def bro_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _handle_lookup(
        update,
        context,
        BROS,
        "bro",
        BROS_MIN_TOKEN_ID,
        0,
    )


async def gal_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _handle_lookup(
        update,
        context,
        GALS,
        "gal",
        GALS_MIN_TOKEN_ID,
        0,
    )


async def _run_services() -> None:
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

    await app.initialize()
    await app.start()

    if app.updater is None:
        raise RuntimeError("Telegram updater was not created.")

    await app.updater.start_polling(drop_pending_updates=True)
    print("Telegram lookup polling started.", flush=True)

    try:
        if DISCORD_ENABLED:
            print("Starting Discord lookup bot...", flush=True)
            await discord_client.start(DISCORD_BOT_TOKEN)
        else:
            await asyncio.Event().wait()
    finally:
        if DISCORD_ENABLED and not discord_client.is_closed():
            await discord_client.close()

        if app.updater.running:
            await app.updater.stop()

        await app.stop()
        await app.shutdown()


def main() -> None:
    try:
        asyncio.run(_run_services())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
