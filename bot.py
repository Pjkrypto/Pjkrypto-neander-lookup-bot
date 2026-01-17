import os
import re
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

# Max traits to display (you want 50 max)
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


async def fetch_opensea_contract(contract: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    # Best-effort: used for total supply (collection size) so we can compute rarity %
    url = f"https://api.opensea.io/api/v2/chain/{CHAIN}/contract/{contract}"
    headers = {"accept": "application/json", "x-api-key": OPENSEA_API_KEY}
    return await _http_get_json(url, headers=headers)


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
    # Show small values with more precision
    if p < 0.01:
        return f"{p*100:.4f}%"
    return f"{p*100:.2f}%"


def _extract_nft_object(data: Dict[str, Any]) -> Dict[str, Any]:
    nft = data.get("nft")
    if isinstance(nft, dict):
        return nft
    return data


def _extract_supply(contract_data: Optional[Dict[str, Any]]) -> Optional[int]:
    if not isinstance(contract_data, dict):
        return None

    # OpenSea contract/collection responses vary; try multiple candidates
    candidates = [
        contract_data.get("total_supply"),
        contract_data.get("supply"),
        contract_data.get("collection", {}).get("total_supply") if isinstance(contract_data.get("collection"), dict) else None,
        contract_data.get("collection", {}).get("stats", {}).get("count") if isinstance(contract_data.get("collection"), dict) else None,
        contract_data.get("stats", {}).get("count") if isinstance(contract_data.get("stats"), dict) else None,
        contract_data.get("nft_collection", {}).get("total_supply") if isinstance(contract_data.get("nft_collection"), dict) else None,
    ]
    for c in candidates:
        iv = _safe_int(c)
        if iv and iv > 0:
            return iv
    return None


def _extract_overall_rarity(nft: Dict[str, Any]) -> Tuple[Optional[int], Optional[float]]:
    rarity = nft.get("rarity")
    if isinstance(rarity, dict):
        return _safe_int(rarity.get("rank")), _safe_float(rarity.get("score"))

    # Soft fallback keys (in case payload shape changes)
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


def extract_fields(
    nft_data: Dict[str, Any],
    contract_data: Optional[Dict[str, Any]],
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

    opensea_url = nft.get("opensea_url")
    if not isinstance(opensea_url, str) or not opensea_url.strip():
        opensea_url = f"https://opensea.io/assets/{CHAIN}/{contract}/{token_id}"

    supply = _extract_supply(contract_data)
    traits_all = _normalize_traits(nft)

    # Cap traits to MAX_TRAITS (50)
    traits = traits_all[:MAX_TRAITS] if MAX_TRAITS > 0 else traits_all

    # OpenSea-style trait rarity lines: "Eyes: Green — 42 (0.76%)"
    trait_lines: List[str] = []
    for t in traits:
        tt = t["trait_type"]
        val = t["value"]
        c = t.get("trait_count")

        if isinstance(c, int) and c > 0 and supply and supply > 0:
            p = c / float(supply)
            trait_lines.append(f"{tt}: {val} — {c} ({_format_pct(p)})")
        elif isinstance(c, int) and c > 0:
            # We have a quantity but not supply -> can't compute %
            trait_lines.append(f"{tt}: {val} — {c} (n/a)")
        else:
            trait_lines.append(f"{tt}: {val} — n/a")

    # Overall rarity block
    os_rank, os_score = _extract_overall_rarity(nft)
    rarity_lines: List[str] = []

    if os_rank is not None or os_score is not None:
        rarity_lines.append("<b>Rarity (OpenSea)</b>")
        if os_rank is not None:
            rarity_lines.append(f"Rank: <b>#{os_rank}</b>")
        if os_score is not None:
            rarity_lines.append(f"Score: <b>{os_score:.2f}</b>")
    else:
        rarity_lines.append("<b>Rarity</b>")
        rarity_lines.append("OpenSea did not provide rank/score for this item.")

    # Minted out of total (best-effort)
    minted_line = None
    if supply and supply > 0:
        minted_line = f"<b>Minted</b>: <b>#{token_id}</b> of <b>{supply}</b>"

    return {
        "name": name,
        "image_url": image_url,
        "opensea_url": opensea_url,
        "trait_lines": trait_lines,
        "rarity_lines": rarity_lines,
        "minted_line": minted_line,
        "traits_total": len(traits_all),
        "traits_shown": len(traits),
        "supply": supply,
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

    contract_data, _ = await fetch_opensea_contract(contract)  # best-effort; do not fail if it errors

    fields = extract_fields(nft_data, contract_data, contract, token_id)

    title = fields["name"]
    opensea_url = fields["opensea_url"]
    minted_line = fields["minted_line"]

    rarity_block = "\n".join(fields["rarity_lines"])
    traits_block = "\n".join(fields["trait_lines"]) if fields["trait_lines"] else "(No traits returned by OpenSea.)"

    # If capped, note it
    traits_note = ""
    if MAX_TRAITS > 0 and fields["traits_total"] > fields["traits_shown"]:
        traits_note = f"\n<i>Showing {fields['traits_shown']} of {fields['traits_total']} traits (MAX_TRAITS={MAX_TRAITS}).</i>"

    header_lines = [f"<b>{title}</b>"]
    if minted_line:
        header_lines.append(minted_line)

    caption = (
        "\n".join(header_lines)
        + "\n\n"
        + rarity_block
        + "\n\n"
        + "<b>Traits</b>\n"
        + traits_block
        + traits_note
        + f"\n\n<a href=\"{opensea_url}\">View on OpenSea</a>"
    )

    # Image at the top (send as photo message first)
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
