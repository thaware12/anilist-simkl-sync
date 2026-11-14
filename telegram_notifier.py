import os
import json
import requests
from datetime import datetime

# Environment Variables
ANILIST_USERNAME = os.environ.get("ANILIST_USERNAME")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC")
STATE_FILE = "dub_state_v2.json"

ANILIST_URL = "https://graphql.anilist.co"
DUB_FEED_URL = "https://raw.githubusercontent.com/RockinChaos/AniSchedule/master/raw/dub-episode-feed.json"
TELEGRAM_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

ANILIST_QUERY = """
query ($userName: String) {
  MediaListCollection(userName: $userName, type: ANIME, status_in: [CURRENT, PLANNING]) {
    lists {
      entries {
        progress
        media {
          id
          idMal
          episodes
          siteUrl
          status
          nextAiringEpisode { episode }
          title { romaji english }
        }
      }
    }
  }
}
"""

def ep_fmt(ep):
    try: return f"E{int(ep):02d}"
    except: return str(ep)

def format_timestamp(raw_time_str):
    if not raw_time_str: return "Unknown Date", "Unknown Time"
    try:
        dt = datetime.fromisoformat(raw_time_str.replace("Z", "+00:00"))
        return dt.strftime("%d %b"), dt.strftime("%H:%M")
    except: return "Unknown Date", "Unknown Time"

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f: return json.load(f)
        except: return {}
    return {}

def flush_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=4)

def send_telegram_message(anime_title, ep_num, total_eps, watched_eps, sub_eps, aired_at, ani_url):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: return
    date_str, time_str = format_timestamp(aired_at)
    
    try: next_ep_str = "Completed" if total_eps != "?" and int(watched_eps) + 1 > int(total_eps) else ep_fmt(int(watched_eps) + 1)
    except: next_ep_str = "?"

    message = (
        f"🎬 **{anime_title}**\n"
        f"╭─ ♫ {ep_fmt(ep_num)} · English Dub\n"
        f"╰─ ✔ {ep_fmt(watched_eps)} / 字 {ep_fmt(sub_eps)} / {ep_fmt(total_eps)}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"📅 {date_str} · 🕒 {time_str} UTC\n"
        f"[🔗 Continue → {next_ep_str}]({ani_url})"
    )
    
    try:
        tele_resp = requests.post(TELEGRAM_URL, json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown", "disable_web_page_preview": True}, timeout=10)
        if tele_resp.status_code != 200:
            print(f"❌ Telegram Error: Failed to send message (HTTP {tele_resp.status_code})")
    except Exception as e:
        print(f"❌ Network Error connecting to Telegram: {e}")

def send_ntfy_notification(anime_title, ep_num, total_eps, watched_eps, sub_eps, aired_at, ani_url):
    if not NTFY_TOPIC: return
    date_str, time_str = format_timestamp(aired_at)

    try: next_ep_str = "Completed" if total_eps != "?" and int(watched_eps) + 1 > int(total_eps) else ep_fmt(int(watched_eps) + 1)
    except: next_ep_str = "?"

    ntfy_title = f"🎬 {anime_title}"

    ntfy_body = (
        f"╭─ ♫ {ep_fmt(ep_num)} · English Dub\n"
        f"╰─ ✔ {ep_fmt(watched_eps)} / 字 {ep_fmt(sub_eps)} / {ep_fmt(total_eps)}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"📅 {date_str} · 🕒 {time_str} UTC\n"
        f"🔗 Continue → {next_ep_str}"
    )

    payload = {
        "topic": NTFY_TOPIC,
        "title": ntfy_title,
        "message": ntfy_body,
        "priority": 3,
        "click": ani_url,
        "markdown": True
    }

    try:
        ntfy_resp = requests.post("https://ntfy.sh/", json=payload, timeout=10)
        if ntfy_resp.status_code != 200:
            print(f"❌ ntfy Server Error: Failed to send push notification (HTTP {ntfy_resp.status_code}).")
    except Exception as e:
        print(f"❌ Network Error connecting to ntfy: {e}")

def main():
    initial_run = not os.path.exists(STATE_FILE)
    notified_state = load_state()

    headers = {"Content-Type": "application/json", "Accept": "application/json", "User-Agent": "AniList-Notifier/1.0"}

    # --- ANILIST API CHECK ---
    try:
        ani_resp = requests.post(ANILIST_URL, json={"query": ANILIST_QUERY, "variables": {"userName": ANILIST_USERNAME}}, headers=headers, timeout=15)
        
        # Catch specific Cloudflare or Server errors
        if ani_resp.status_code == 403:
            print("❌ AniList Firewall Block: GitHub Actions IP is temporarily blocked by Cloudflare (403 Forbidden).")
            return
        elif ani_resp.status_code >= 500:
            print(f"❌ AniList Server Down: The AniList API is currently offline (HTTP {ani_resp.status_code}).")
            return
            
        ani_data = ani_resp.json()
    except Exception as e:
        print(f"❌ Network Error connecting to AniList: {e}")
        return

    # Check for AniList internal GraphQL errors (like the "temporarily disabled" message)
    if "errors" in ani_data:
        error_msg = ani_data["errors"][0].get("message", "Unknown GraphQL error")
        print(f"❌ AniList API Issue: {error_msg}")
        return

    data_block = ani_data.get("data")
    if not data_block:
        print("❌ AniList Data Error: Received empty data from AniList.")
        return

    # Highly optimized tracking dictionary
    tracking_data = {}
    for lst in data_block.get("MediaListCollection", {}).get("lists", []):
        for entry in lst.get("entries", []):
            media = entry.get("media", {})
            ani_id, mal_id = str(media.get("id", "")), str(media.get("idMal", ""))
            
            title = media.get("title", {}).get("english") or media.get("title", {}).get("romaji")
            total_eps = media.get("episodes") or "?"
            
            status = media.get("status")
            next_air = media.get("nextAiringEpisode")
            sub_eps = (next_air.get("episode") - 1) if next_air and next_air.get("episode") else (total_eps if status in ["FINISHED", "CANCELLED"] else "?")

            info = {"title": title, "total": total_eps, "watched": entry.get("progress", 0), "sub_eps": sub_eps, "url": media.get("siteUrl", "")}
            if ani_id: tracking_data[f"ani_{ani_id}"] = info
            if mal_id: tracking_data[f"mal_{mal_id}"] = info

    if not tracking_data: return

    # --- ANISCHEDULE FEED CHECK ---
    try:
        dub_resp = requests.get(DUB_FEED_URL, headers=headers, timeout=15)
        if dub_resp.status_code != 200:
            print(f"❌ AniSchedule Error: Feed is down or unavailable (HTTP {dub_resp.status_code}).")
            return
        dub_feed = dub_resp.json()
    except Exception as e:
        print(f"❌ Network Error connecting to AniSchedule Repo: {e}")
        return

    state_changed = False

    for episode_data in dub_feed:
        feed_ani_id = str(episode_data.get("id", ""))
        feed_mal_id = str(episode_data.get("idMal", ""))
        ep_num = episode_data.get("episode", {}).get("aired", 0)
        
        # Match using the optimized dictionary
        matched_info = None
        if feed_ani_id and f"ani_{feed_ani_id}" in tracking_data:
            matched_info, matched_id = tracking_data[f"ani_{feed_ani_id}"], feed_ani_id
        elif feed_mal_id and f"mal_{feed_mal_id}" in tracking_data:
            matched_info, matched_id = tracking_data[f"mal_{feed_mal_id}"], feed_mal_id

        if matched_info and ep_num > 0:
            last_notified = notified_state.get(matched_id, 0)
            
            if initial_run:
                notified_state[matched_id] = max(last_notified, ep_num)
                state_changed = True
            elif ep_num > last_notified:
                send_ntfy_notification(matched_info["title"], ep_num, matched_info["total"], matched_info["watched"], matched_info["sub_eps"], episode_data.get("episode", {}).get("airedAt"), matched_info["url"])
                send_telegram_message(matched_info["title"], ep_num, matched_info["total"], matched_info["watched"], matched_info["sub_eps"], episode_data.get("episode", {}).get("airedAt"), matched_info["url"])
                
                print(f"✅ Alert sent for: {matched_info['title']} (Ep {ep_num})")
                notified_state[matched_id] = ep_num
                state_changed = True

    if state_changed or initial_run:
        flush_state(notified_state)

if __name__ == "__main__":
    main()
