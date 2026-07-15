"""Telegram alerts. Photo alert when an image URL survived normalization
(PRD A3 fallback: text-only otherwise)."""

from __future__ import annotations

import html

import httpx

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"


def format_alert(
    product: dict,
    kind: str,
    added_to_cart: bool,
    app: str,
    link: str,
    address_labels: list[str] | None = None,
    cart_label: str | None = None,
) -> str:
    lines = [f"🏎️ <b>{kind}</b> — Hot Wheels ({html.escape(app)})",
             html.escape(product["title"])]
    if product.get("price") is not None:
        lines.append(f"₹{product['price']}")
    if address_labels:
        joined = ", ".join(html.escape(a) for a in address_labels)
        lines.append(f"📍 In stock at: {joined}")
    if added_to_cart:
        suffix = f" ({html.escape(cart_label)})" if cart_label else ""
        lines.append(f"🛒 Added to cart{suffix}")
    lines.append(f'🔗 <a href="{link}">Open in {html.escape(app)}</a>')
    return "\n".join(lines)


async def send_telegram(bot_token: str, chat_id: str, text: str, image: str | None = None) -> None:
    async with httpx.AsyncClient(timeout=20) as http:
        if image:
            response = await http.post(
                TELEGRAM_API.format(token=bot_token, method="sendPhoto"),
                json={"chat_id": chat_id, "photo": image, "caption": text, "parse_mode": "HTML"},
            )
            if response.status_code == 200:
                return
            # Bad/blocked image URL — degrade to text rather than lose the alert.
        response = await http.post(
            TELEGRAM_API.format(token=bot_token, method="sendMessage"),
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": False},
        )
        response.raise_for_status()


async def send_product_alert(
    telegram: tuple[str, str],
    product: dict,
    kind: str,
    added_to_cart: bool,
    app: str,
    link: str,
    address_labels: list[str] | None = None,
    cart_label: str | None = None,
) -> None:
    bot_token, chat_id = telegram
    await send_telegram(
        bot_token, chat_id,
        format_alert(product, kind, added_to_cart, app, link, address_labels, cart_label),
        image=product.get("image"),
    )


async def send_plain(telegram: tuple[str, str], text: str) -> None:
    bot_token, chat_id = telegram
    await send_telegram(bot_token, chat_id, text)
