"""Discord webhook alerts. No-ops silently when the webhook isn't configured."""
from __future__ import annotations

import requests

import config


def send_discord(message: str, channel: str = "action") -> bool:
    """Post to one of the two Discord channels: "action" (terse, act-now)
    or "detail" (the narrator's full briefs). Both fall back to the single
    DISCORD_WEBHOOK_URL when the split isn't configured."""
    url = (config.DISCORD_DETAIL_WEBHOOK_URL if channel == "detail"
           else config.DISCORD_ACTION_WEBHOOK_URL)
    if not url:
        print(f"[alert skipped - no webhook/{channel}] {message[:80]}")
        return False
    try:
        resp = requests.post(url, json={"content": message[:1900]}, timeout=8)
        return resp.status_code in (200, 204)
    except requests.RequestException as exc:
        print(f"[alert failed] {exc}")
        return False


def alert_new_take(match_name: str, market_title: str, edge: float, ev: float) -> None:
    send_discord(
        f"🟢 **NEW VALUE BET** — {match_name}\n"
        f"{market_title}\nEdge: {edge:+.1%} | EV per $1: {ev:+.2f}"
    )


def alert_ripe(match_name: str, market_title: str, timing: dict) -> None:
    reasons = "\n".join(f"• {r}" for r in timing["reasons"][:4])
    send_discord(
        f"⏰ **RIPE — BET WINDOW OPEN** ({timing['score']:.0f}/100) — {match_name}\n"
        f"**{market_title}** @ {timing['current_odds']:.2f} "
        f"(edge {timing['current_edge']:+.1%})\n{reasons}"
    )


def alert_final_lock(match_name: str, best_bet: dict | None) -> None:
    if best_bet:
        send_discord(
            f"🔴 **FINAL DECISION LOCKED** (T-10min) — {match_name}\n"
            f"Best bet: {best_bet['market_title']}\n"
            f"Model: {best_bet['model_probability']:.0%} | "
            f"Odds: {best_bet['decimal_odds']} | "
            f"EV: {best_bet['expected_value']:+.2f} → **{best_bet['recommendation']}**"
        )
    else:
        send_discord(f"🔴 **FINAL DECISION LOCKED** — {match_name}: no bets clear the bar. SKIP all.")


def send_ntfy(message: str, title: str = "WC26", priority: str = "high") -> bool:
    """Instant phone push via ntfy.sh — no account, no open page, no RC.
    Silently no-ops when the topic is unset."""
    if not config.NTFY_TOPIC:
        return False
    try:
        resp = requests.post(
            f"https://ntfy.sh/{config.NTFY_TOPIC}",
            data=message[:1900].encode("utf-8"),
            headers={"Title": title, "Priority": priority, "Tags": "soccer"},
            timeout=8)
        return resp.status_code == 200
    except requests.RequestException as exc:
        print(f"[ntfy failed] {exc}")
        return False


def send_alert(message: str, title: str = "WC26",
               kind: str = "action") -> dict[str, bool]:
    """Fan-out by kind. "action": the act-now channel + phone push, and a
    copy to detail so that channel reads as a complete log. "detail": the
    narrator's channel only — the phone stays quiet.

    Returns the per-transport acceptance map, e.g.
    {"discord_action": True, "discord_detail": False, "ntfy": False}.
    False covers BOTH delivery failure and non-configuration — an
    unconfigured transport never counts as delivered, so a caller that
    needs durability (the hunter) can see that nothing actually left the
    process instead of treating a silent no-op as success."""
    if kind == "detail":
        return {"discord_detail": send_discord(message, channel="detail")}
    results = {"discord_action": send_discord(message, channel="action")}
    if config.DISCORD_DETAIL_WEBHOOK_URL != config.DISCORD_ACTION_WEBHOOK_URL:
        results["discord_detail"] = send_discord(message, channel="detail")
    results["ntfy"] = send_ntfy(message, title=title)
    return results
