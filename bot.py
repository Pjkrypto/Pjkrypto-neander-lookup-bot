import os
import re
import io
import asyncio
from typing import Any, Dict, List, Optional, Tuple

import httpx
import discord
from discord import app_commands
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

# Alchemy metadata retry protection.
# Alchemy can occasionally return HTTP 200 with incomplete NFT metadata.
METADATA_FETCH_RETRIES = int(os.getenv("METADATA_FETCH_RETRIES", "3"))
METADATA_RETRY_SECONDS = float(os.getenv("METADATA_RETRY_SECONDS", "2"))

# Alchemy
ALCHEMY_API_KEY = os.getenv("ALCHEMY_API_KEY", "").strip()
ALCHEMY_NETWORK = os.getenv("ALCHEMY_NETWORK", "polygon-mainnet").strip()
ALCHEMY_BASE_URL = os.getenv("ALCHEMY_BASE_URL", "").strip().rstrip("/")

# OpenSea
OPENSEA_API_KEY = os.getenv("OPENSEA_API_KEY", "").strip()

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
if not ALCHEMY_API_KEY:
    raise SystemExit("Missing ALCHEMY_API_KEY")
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
        return f"{trait_type}: {value} — {count} ({pct_str})"
    return f"{trait_type}: {value} — ({pct_str})"


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
    meta: Optional[Dict[str, Any]] = None
    traits: List[Dict[str, Any]] = []
    image_url: Optional[str] = None
    img_bytes: Optional[bytes] = None
    last_error: Optional[str] = None

    attempts = max(1, METADATA_FETCH_RETRIES)

    # Do not accept an HTTP-200 metadata response unless the NFT metadata is
    # actually complete enough to build the Telegram card. On retries, force
    # Alchemy to refresh its metadata cache.
    for attempt in range(attempts):
        refresh_cache = attempt > 0

        meta, err = await fetch_nft_metadata_alchemy(
            contract,
            token_id,
            refresh_cache=refresh_cache,
        )

        if err or not isinstance(meta, dict):
            last_error = err or "Metadata not available."
            print(
                f"Metadata fetch failed for tokenId={token_id} "
                f"attempt={attempt + 1}/{attempts}: {last_error}",
                flush=True,
            )
        else:
            traits = _extract_traits(meta)
            image_url = _pick_image_url(meta)

            raw = meta.get("raw")
            raw_error = raw.get("error") if isinstance(raw, dict) else None

            if not traits or not image_url:
                last_error = (
                    f"Incomplete metadata: traits={len(traits)} "
                    f"image={bool(image_url)} raw_error={raw_error!r}"
                )
                print(
                    f"Incomplete Alchemy metadata for tokenId={token_id} "
                    f"attempt={attempt + 1}/{attempts}: "
                    f"traits={len(traits)} image={bool(image_url)} "
                    f"raw_error={raw_error!r}",
                    flush=True,
                )
            else:
                img_bytes = await _download_image_bytes(image_url)
                if img_bytes:
                    break

                last_error = f"Image download failed for tokenId={token_id}"
                print(
                    f"{last_error} attempt={attempt + 1}/{attempts}",
                    flush=True,
                )

        if attempt + 1 < attempts:
            await asyncio.sleep(max(0.0, METADATA_RETRY_SECONDS))

    if not isinstance(meta, dict) or not traits or not image_url or not img_bytes:
        return (
            None,
            None,
            "NFT metadata is temporarily unavailable. Please try this command again in a few seconds."
        )

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

    trait_lines: List[str] = []
    for a in traits[:MAX_TRAITS]:
        tt = a.get("trait_type") or a.get("type") or a.get("traitType") or "Trait"
        vv = a.get("value")
        if not isinstance(tt, str) or vv is None:
            continue
        vv_s = str(vv)
        pct = trait_pct_map.get((_norm(tt), _norm(vv_s)))
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
        minimum_display = min_id + DISPLAY_ID_OFFSETS.get(contract.lower(), 0)
        await interaction.response.send_message(
            f"{label} NFT number must be >= {minimum_display}.",
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
    # Discord users enter the displayed Bro NFT number.
    # Bros display tokenId+1, so /bro 1386 looks up raw token 1385.
    token_id = number - DISPLAY_ID_OFFSETS.get(BROS, 0)
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
        number="NeanderBro NFT number, e.g. 1386"
    )(discord_bro_cmd)
    discord_gal_cmd = app_commands.describe(
        number="NeanderGal NFT number, e.g. 91"
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
