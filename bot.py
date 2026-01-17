import os
import re
import asyncio
from typing import Any, Dict, Optional, Tuple

import httpx
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
OPENSEA_API_KEY = os.getenv("OPENSEA_API_KEY", "").strip()

CHAIN = os.getenv("CHAIN", "polygon").strip()
BROS = os.getenv("NEANDERBROS_CONTRACT", "").strip().lower()
GALS = os.getenv("NEANDERGALS_CONTRACT", "").strip().lower()

MAX_TRAITS = int(os.getenv("MAX_TRAITS", "12"))

if not TELEGRAM_BOT_TOKEN:
    raise SystemExit("Missing TELEGRAM_BOT_TOKEN")
if not OPENSEA_API_KEY:
    raise SystemExit("Missing OPENSEA_API_KEY")
if not BROS:
    raise SystemExit("Missing NEANDERBROS_CONTRACT")
if not GALS:
    raise SystemExit("Missing NEANDERGALS_CONTRACT")


def _parse_token_id(args: list[str]) -> Optional[str]:
    if not args:
        return None
    # allow "1234" or "#1234"
    token = args[0].strip()
    token = token.lstrip("#")
    if not re.fullmatch(r"\d+", token):
        return None
    return token


async def fetch_opensea_nft(contract: str, token_id: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """
    Returns (data, error_message)
    """
    url = f"https://api.opensea.io/api/v2/chain/{CHAIN}/contract/{contract}/nfts/{token_id}"
    headers = {
        "accept": "application/json",
        "x-api-key": OPENSEA_API_KEY,
    }

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(url, headers=headers)
    except Exception as e:
        return None, f"Network error calling OpenSea: {e}"

    if r.status_code == 429:
        return None, "OpenSea throttled the request (429). Please try again in ~10–20 seconds."
    if r.status_code >= 400:
        # Keep error compact; avoid dumping huge bodies
        return None, f"OpenSea error {r.status_code}: {r.text[:200]}"

    try:
        return r.json(), None
    except Exception:
        return None, "Could not parse OpenSea response as JSON."


def extract_fields(data: Dict[str, Any], contract: str, token_id: str) -> Dict[str, Any]:
    # OpenSea v2 typically returns an object with fields such as:
    # - "nft": { "name", "image_url", "display_image_url", "traits", ... }
    nft = data.get("nft") if isinstance(data, dict) else None
    if not isinstance(nft, dict):
        nft = data  # sometimes APIs return the object directly

    name = nft.get("name") if isinstance(nft, dict) else None
    if not name:
        name = f"Token #{token_id}"

    # Prefer display_image_url when present
    image_url = None
    for k in ("display_image_url", "image_url", "image"):
        v = nft.get(k) if isinstance(nft, dict) else None
        if isinstance(v, str) and v.strip():
            image_url = v.strip()
            break

    # Traits may appear as list of dicts
    traits = nft.get("traits") if isinstance(nft, dict) else None
    trait_lines = []
    if isinstance(traits, list):
        for t in traits[:MAX_TRAITS]:
            if not isinstance(t, dict):
                continue
            trait_type = t.get("trait_type") or t.get("type") or "Trait"
            value = t.get("value")
            if value is None:
                continue
            trait_lines.append(f"- {trait_type}: {value}")

    # OpenSea item link (construct if not provided)
    opensea_url = nft.get("opensea_url") if isinstance(nft, dict) else None
    if not isinstance(opensea_url, str) or not opensea_url.strip():
        # Polygon item URLs commonly work as:
        # https://opensea.io/assets/<chain>/<contract>/<token_id>
        opensea_url = f"https://opensea.io/assets/{CHAIN}/{contract}/{token_id}"

    return {
        "name": name,
        "image_url": image_url,
        "trait_lines": trait_lines,
        "opensea_url": opensea_url,
    }


async def handle_lookup(update: Update, context: ContextTypes.DEFAULT_TYPE, contract: str, label: str) -> None:
    token_id = _parse_token_id(context.args)
    if not token_id:
        await update.message.reply_text(f"Usage: /{label} <tokenId>  (example: /{label} 33)")
        return

    # Immediate acknowledgement helps with perceived latency
    await update.message.chat.send_action(action="typing")

    data, err = await fetch_opensea_nft(contract, token_id)
    if err:
        await update.message.reply_text(err)
        return

    fields = extract_fields(data, contract, token_id)
    title = fields["name"]
    traits = "\n".join(fields["trait_lines"]) if fields["trait_lines"] else "(No traits returned by OpenSea.)"
    opensea_url = fields["opensea_url"]

    caption = f"<b>{title}</b>\n\n{traits}\n\n<a href=\"{opensea_url}\">View on OpenSea</a>"

    image_url = fields["image_url"]
    if image_url:
        try:
            await update.message.reply_photo(photo=image_url, caption=caption, parse_mode=ParseMode.HTML)
            return
        except Exception:
            # If Telegram rejects the image URL, fall back to text
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
