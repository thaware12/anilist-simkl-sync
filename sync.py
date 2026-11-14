import os
import time
import json
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

ANILIST_USERNAME = os.environ.get("ANILIST_USERNAME")
SIMKL_CLIENT_ID = os.environ.get("SIMKL_CLIENT_ID")
STATE_FILE = "sync_state_v2.json"
AUTH_FILE = "simkl_auth.json"
SKIPPED_FILE = "skipped_anime.txt"
REPORT_FILE = "simkl_rules_report.txt"

# Safety Net Thresholds
MAX_DELETION_PERCENT = 15.0   # Never delete more than 15% of the list in one run
MAX_RUNTIME_SECONDS = 3600  # Changed from 240 to 3600 (60 minutes)
BATCH_SIZE = 25               # Process at most 25 anime per batch for incremental saving

# Official API Endpoints
ANILIST_URL = "https://graphql.anilist.co"
SIMKL_TOKEN_URL = "https://api.simkl.com/oauth2/token"
SIMKL_ACTIVITIES_URL = "https://api.simkl.com/sync/activities"
SIMKL_ALL_ANIME_URL = "https://api.simkl.com/sync/all-items/anime"
SIMKL_HISTORY_URL = "https://api.simkl.com/sync/history?skip_auto_watching=yes"
SIMKL_LIST_URL = "https://api.simkl.com/sync/add-to-list"
SIMKL_REMOVE_URL = "https://api.simkl.com/sync/history/remove"

STATUS_MAP = {
    "CURRENT": "watching",
    "COMPLETED": "completed",
    "PAUSED": "hold",
    "DROPPED": "dropped",
    "PLANNING": "plantowatch",
    "REPEATING": "watching"
}

# Sync order priority so PLANNING/COMPLETED specials never overwrite active WATCHING parent shows
STATUS_PRIORITY = {
    "PLANNING": 1,
    "COMPLETED": 2,
    "DROPPED": 3,
    "PAUSED": 4,
    "CURRENT": 5,
    "REPEATING": 5
}

PRETTY_SIMKL_STATUS = {
    "watching": "Watching",
    "completed": "Completed",
    "hold": "On Hold (Paused)",
    "dropped": "Dropped",
    "plantowatch": "Plan to Watch"
}

ANILIST_QUERY = """
query ($userName: String) {
  MediaListCollection(userName: $userName, type: ANIME) {
    lists {
      entries {
        progress
        status
        media {
          id
          idMal
          status
          episodes
          title { romaji english }
        }
      }
    }
  }
}
"""

session = requests.Session()
retries = Retry(total=3, backoff_factor=1, status_forcelist=[500, 502, 503, 504])
session.mount('https://', HTTPAdapter(max_retries=retries))

def load_json(filepath):
    if os.path.exists(filepath):
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                data = json.load(f)
                return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, OSError):
            pass
    return {}

def save_json_atomic(filepath, data):
    tmp_path = f"{filepath}.tmp"
    with open(tmp_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=4)
    os.replace(tmp_path, filepath)

def log_skipped_unique(entry_line):
    existing = set()
    if os.path.exists(SKIPPED_FILE):
        with open(SKIPPED_FILE, 'r', encoding='utf-8') as f:
            existing = {line.strip() for line in f if line.strip()}
    if entry_line not in existing:
        with open(SKIPPED_FILE, 'a', encoding='utf-8') as f:
            f.write(f"{entry_line}\n")

def get_headers(auth_data):
    return {
        "Content-Type": "application/json",
        "simkl-api-key": SIMKL_CLIENT_ID,
        "Authorization": f"Bearer {auth_data.get('access_token')}",
        "User-Agent": "anilist-simkl-bridge/10.0"
    }

def refresh_simkl_token(auth_data):
    print("🔄 Access token expired. Refreshing V2 tokens...")
    payload = {
        "grant_type": "refresh_token",
        "client_id": SIMKL_CLIENT_ID,
        "refresh_token": auth_data.get("refresh_token")
    }
    try:
        response = session.post(SIMKL_TOKEN_URL, json=payload, timeout=15)
        if response.status_code == 200:
            new_tokens = response.json()
            auth_data["access_token"] = new_tokens.get("access_token")
            auth_data["refresh_token"] = new_tokens.get("refresh_token")
            save_json_atomic(AUTH_FILE, auth_data)
            print("✅ Saved new 7-day Access Token!")
            return True
        else:
            print(f"❌ Failed to refresh token: {response.text}")
            return False
    except Exception as e:
        print(f"❌ Token Refresh Error: {e}")
        return False

def get_anilist_profile():
    print(f"🔍 Fetching AniList profile for user: {ANILIST_USERNAME}...")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    try:
        response = session.post(
            ANILIST_URL,
            json={"query": ANILIST_QUERY, "variables": {"userName": ANILIST_USERNAME}},
            headers=headers,
            timeout=20
        )
        response.raise_for_status()
        res_json = response.json()

        if "errors" in res_json:
            print(f"🚨 ANILIST GRAPHQL ERROR DETECTED: {res_json['errors']}. Aborting to protect state.")
            return None

        anime_dict = {}
        lists = res_json.get("data", {}).get("MediaListCollection", {}).get("lists", [])
        
        for lst in lists:
            for entry in lst.get("entries", []):
                media = entry.get("media", {})
                a_id = str(media.get("id"))
                mal_id = media.get("idMal")
                air_status = media.get("status")
                title = media.get("title", {}).get("english") or media.get("title", {}).get("romaji") or f"Unknown ID {a_id}"
                
                anime_dict[a_id] = {
                    "progress": entry.get("progress", 0),
                    "status": entry.get("status", "CURRENT"),
                    "air_status": air_status,
                    "title": title,
                    "mal_id": str(mal_id) if mal_id else None,
                    "synced_v8": True,
                    "synced_v10": True
                }
                
        print(f"✅ Found {len(anime_dict)} total anime on AniList.")
        return anime_dict
    except Exception as e:
        print(f"❌ AniList Network Error: {e}")
        return None

def get_simkl_activities(auth_data, retries_left=1):
    try:
        response = session.post(SIMKL_ACTIVITIES_URL, headers=get_headers(auth_data), timeout=15)
        if response.status_code == 401 and retries_left > 0:
            if refresh_simkl_token(auth_data):
                return get_simkl_activities(auth_data, retries_left=0)
        if response.status_code == 200:
            return response.json().get("anime", {}).get("all")
    except Exception as e:
        print(f"⚠️ Activities check warning: {e}")
    return None

def get_live_simkl_anime(auth_data, retries_left=1):
    print("🔍 Checking Simkl /sync/activities and fetching live anime list...")
    _ = get_simkl_activities(auth_data)
    try:
        response = session.get(SIMKL_ALL_ANIME_URL, headers=get_headers(auth_data), timeout=25)
        if response.status_code == 401 and retries_left > 0:
            if refresh_simkl_token(auth_data):
                return get_live_simkl_anime(auth_data, retries_left=0)
        if response.status_code == 200:
            data = response.json()
            items = data.get("anime", []) if isinstance(data, dict) else data
            
            by_anilist = {}
            by_mal = {}
            for item in items:
                show = item.get("show", {})
                ids = show.get("ids", {})
                info = {
                    "status": item.get("status"),
                    "progress": item.get("watched_episodes_count", 0),
                    "total_eps": show.get("total_episodes", 0),
                    "simkl_id": ids.get("simkl")
                }
                if ids.get("anilist"):
                    by_anilist[str(ids.get("anilist"))] = info
                if ids.get("mal"):
                    by_mal[str(ids.get("mal"))] = info
                    
            print(f"✅ Found {len(items)} anime currently live on Simkl.")
            return {"by_anilist": by_anilist, "by_mal": by_mal}
        else:
            print(f"⚠️ Could not fetch live Simkl list (HTTP {response.status_code}).")
            return None
    except Exception as e:
        print(f"⚠️ Error fetching live Simkl list: {e}")
        return None

def push_to_simkl(url, payload, auth_data, retries_left=1):
    items_list = payload.get("anime") or payload.get("shows")
    if not items_list:
        return True
    
    endpoint_name = url.split('?')[0].split('/')[-1]
    print(f"🚀 Pushing {len(items_list)} items to Simkl /{endpoint_name}...")
    
    try:
        response = session.post(url, json=payload, headers=get_headers(auth_data), timeout=30)
        
        if response.status_code == 401 and retries_left > 0:
            print("⚠️ Simkl rejected the token (401). Triggering refresh...")
            if refresh_simkl_token(auth_data):
                return push_to_simkl(url, payload, auth_data, retries_left=0)
                
        if response.status_code == 429:
            print("⏳ Simkl Rate Limit (429) hit. Pausing further batches until the next run.")
            return False
        
        if response.status_code in (200, 201):
            res_data = response.json()
            print(f"✅ Success on /{endpoint_name}! Response: {json.dumps(res_data)[:220]}")
            
            not_found_shows = res_data.get("not_found", {}).get("shows", [])
            for missing in not_found_shows:
                m_title = missing.get("title", "Unknown Title")
                m_ids = missing.get("ids", {})
                log_skipped_unique(f"[Not Found on Simkl] {m_title} | IDs: {json.dumps(m_ids)}")
            return True
        else:
            print(f"❌ Simkl API error on /{endpoint_name}! HTTP {response.status_code} | {response.text}")
            return False
            
    except Exception as e:
        print(f"❌ Simkl Push Error: {e}")
        return False

def build_ids(a_id, data, simkl_info=None):
    ids = {"anilist": str(a_id)}
    if data.get("mal_id"):
        ids["mal"] = str(data["mal_id"])
    if simkl_info and simkl_info.get("simkl_id"):
        ids["simkl"] = int(simkl_info["simkl_id"])
    return ids

def resolve_simkl_target_status(anilist_status, air_status):
    target_stat = STATUS_MAP.get(anilist_status, "watching")
    if air_status == "NOT_YET_RELEASED" and target_stat == "completed":
        return "plantowatch"
    return target_stat

def generate_rules_report(current_state, live_simkl):
    """Generates a human-readable report of all anime affected by Simkl rules and where they sit now."""
    if not live_simkl:
        return

    status_overrides = []
    episode_differences = []
    merged_or_missing = []

    for a_id, data in current_state.items():
        title = data["title"]
        al_stat_raw = data["status"]
        expected_simkl_stat = STATUS_MAP.get(al_stat_raw, "watching")
        expected_pretty = PRETTY_SIMKL_STATUS.get(expected_simkl_stat, expected_simkl_stat)
        al_prog = data["progress"]
        air_status = data.get("air_status", "UNKNOWN")

        simkl_info = live_simkl["by_anilist"].get(a_id)
        if not simkl_info and data.get("mal_id"):
            simkl_info = live_simkl["by_mal"].get(data["mal_id"])

        if not simkl_info:
            merged_or_missing.append(
                f"• {title} (AniList ID: {a_id})\n"
                f"  - AniList State : {expected_pretty} ({al_prog} ep)\n"
                f"  - Simkl Now     : Not a standalone entry (Merged into parent series/season or missing from Simkl DB)"
            )
            continue

        actual_stat = simkl_info["status"]
        actual_pretty = PRETTY_SIMKL_STATUS.get(actual_stat, actual_stat)
        actual_prog = simkl_info["progress"]

        if actual_stat != expected_simkl_stat:
            if expected_simkl_stat == "completed" and actual_stat == "watching":
                reason = (
                    "Simkl lists this entry as still airing or groups it with an upcoming sequel/part "
                    "(e.g., multi-part movie), so Simkl holds it in 'Watching' until all parts finish airing."
                )
            elif expected_simkl_stat == "completed" and actual_stat == "plantowatch":
                reason = (
                    "Anime is unreleased or has 0 aired episodes on Simkl; Simkl forbids marking "
                    "unreleased shows as 'Completed' and places them in 'Plan to Watch'."
                )
            else:
                reason = f"Simkl watchlist rules overrode status (AniList Airing Status: {air_status})."

            status_overrides.append(
                f"• {title} (AniList ID: {a_id})\n"
                f"  - AniList State : {expected_pretty} ({al_prog} ep)\n"
                f"  - Simkl Now     : {actual_pretty} ({actual_prog} ep)\n"
                f"  - Simkl Rule    : {reason}"
            )

        elif actual_prog != al_prog:
            if expected_simkl_stat == "plantowatch" and al_prog > 0 and actual_prog == 0:
                ep_reason = "Simkl forbids watched episode history inside 'Plan to Watch', so progress is held at 0 ep."
            elif actual_stat == "completed":
                ep_reason = "Simkl merges split-cour parts/specials into a single entry with a different total episode count."
            elif air_status == "NOT_YET_RELEASED":
                ep_reason = "Anime is unreleased, so Simkl holds watched progress at 0 ep."
            else:
                ep_reason = "Simkl catalog episode count differs from AniList entry."

            episode_differences.append(
                f"• {title} (AniList ID: {a_id})\n"
                f"  - AniList State : {expected_pretty} ({al_prog} ep)\n"
                f"  - Simkl Now     : {actual_pretty} ({actual_prog} ep)\n"
                f"  - Simkl Rule    : {ep_reason}"
            )

    lines = [
        "======================================================================",
        "               SIMKL RULES & STATUS DIFFERENCES REPORT                ",
        "======================================================================",
        f"Total AniList Anime Checked: {len(current_state)}",
        f"1. Status Overridden by Simkl Rules : {len(status_overrides)}",
        f"2. Episode Count Differences        : {len(episode_differences)}",
        f"3. Merged Specials / Missing IDs    : {len(merged_or_missing)}",
        "======================================================================\n",
        "----------------------------------------------------------------------",
        "1. STATUS OVERRIDDEN BY SIMKL RULES (Where They Are on Simkl Now)",
        "----------------------------------------------------------------------"
    ]
    lines.extend(status_overrides if status_overrides else ["None! All standalone statuses match 100%."])

    lines.extend([
        "\n----------------------------------------------------------------------",
        "2. EPISODE COUNT DIFFERENCES (Split-Cour Merges & Plan-to-Watch Rules)",
        "----------------------------------------------------------------------"
    ])
    lines.extend(episode_differences if episode_differences else ["None! All episode counts match 100%."])

    lines.extend([
        "\n----------------------------------------------------------------------",
        "3. MERGED SPECIALS / STANDALONE ID NOT IN SIMKL DB",
        "----------------------------------------------------------------------"
    ])
    lines.extend(merged_or_missing if merged_or_missing else ["None!"])
    lines.append("")

    save_tmp = f"{REPORT_FILE}.tmp"
    with open(save_tmp, 'w', encoding='utf-8') as f:
        f.write("\n".join(lines))
    os.replace(save_tmp, REPORT_FILE)
    print(f"📊 Updated {REPORT_FILE} ({len(status_overrides)} status overrides, {len(episode_differences)} ep diffs, {len(merged_or_missing)} merged/missing).")

def chunk_list(lst, size):
    for i in range(0, len(lst), size):
        yield lst[i:i + size]

def main():
    start_time = time.time()
    print("--- STARTING V10 SELF-HEALING PRODUCTION MIRROR ---")
    
    working_state = load_json(STATE_FILE)
    auth_data = load_json(AUTH_FILE)
    
    if not auth_data.get("access_token"):
        print("❌ No Simkl Auth Data found in simkl_auth.json")
        return

    current_state = get_anilist_profile()
    
    # SAFETY NET 1: Null / Empty Profile Kill Switch
    if current_state is None:
        print("🛡️ Exiting cleanly without touching Simkl (AniList unreachable).")
        return
    if len(current_state) == 0 and len(working_state) > 0:
        print("🚨 NULL KILL SWITCH ACTIVATED: AniList returned 0 anime while state has data. Aborting!")
        return

    deleted_ids = [old_id for old_id in working_state if old_id not in current_state]
    changed_ids = []
    needs_v10_live_audit = False

    for a_id, data in current_state.items():
        prev = working_state.get(a_id, {})
        if not isinstance(prev, dict) or not prev.get("synced_v8", False):
            changed_ids.append(a_id)
            continue
        if not prev.get("synced_v10", False):
            needs_v10_live_audit = True
        if data["progress"] != prev.get("progress", -1) or data["status"] != prev.get("status", ""):
            changed_ids.append(a_id)

    # SAFETY NET 2: 15% Mass Deletion Guard
    if len(working_state) > 0 and deleted_ids:
        deletion_pct = (len(deleted_ids) / len(working_state)) * 100.0
        if deletion_pct > MAX_DELETION_PERCENT:
            print(
                f"🚨 15% MASS DELETION GUARD ACTIVATED: Attempting to delete {len(deleted_ids)} of "
                f"{len(working_state)} anime ({deletion_pct:.1f}%). Blocking deletions to protect Simkl!"
            )
            deleted_ids = []

    report_missing = not os.path.exists(REPORT_FILE)

    if not deleted_ids and not changed_ids and not needs_v10_live_audit and not report_missing:
        print("😴 No changes on AniList since last sync. Skipping Simkl API calls.")
        print("--- SCRIPT FINISHED ---")
        return

    live_simkl = get_live_simkl_anime(auth_data)

    # V10 SELF-HEALING AUDIT: Find any show where a merged special overwrote episode count (e.g. One Piece 1180 vs 1177)
    # or where a Planning sequel overwrote a completed Part 1 into Plan to Watch (e.g. Demon Slayer Infinity Castle)
    if live_simkl and needs_v10_live_audit:
        print("🩺 Running V10 Self-Healing Audit against live Simkl...")
        for a_id, data in current_state.items():
            if a_id in changed_ids:
                continue
            simkl_info = live_simkl["by_anilist"].get(a_id)
            if not simkl_info and data.get("mal_id"):
                simkl_info = live_simkl["by_mal"].get(data["mal_id"])

            if not simkl_info:
                # Merged special (like One Piece Fan Letter or Demon Slayer Part 2) -> mark v10 synced so we NEVER re-push it!
                working_state[a_id] = current_state[a_id]
                continue

            air_status = data.get("air_status")
            target_prog = 0 if air_status == "NOT_YET_RELEASED" else data["progress"]
            target_stat = resolve_simkl_target_status(data["status"], air_status)
            curr_prog = simkl_info["progress"]
            curr_stat = simkl_info["status"]

            # Case 1: Active/non-completed show has wrong episode count on Simkl (e.g. ONE PIECE 1180 vs 1177)
            ep_drifted = (target_stat not in ("completed", "plantowatch")) and (curr_prog != target_prog)
            # Case 2: Released show with watched episodes was pushed into Plan to Watch by a sequel (e.g. Demon Slayer Part 1)
            status_drifted = (curr_stat == "plantowatch" and target_stat != "plantowatch" and target_prog > 0)

            if ep_drifted or status_drifted:
                print(f"🔧 Auto-Heal queued for '{data['title']}': Simkl has {curr_stat}/{curr_prog} ep, AniList wants {target_stat}/{target_prog} ep")
                changed_ids.append(a_id)
            else:
                working_state[a_id] = current_state[a_id]

        save_json_atomic(STATE_FILE, working_state)

    if not deleted_ids and not changed_ids:
        generate_rules_report(current_state, live_simkl)
        print("😴 Audit complete! No Simkl writes needed.")
        print("--- SCRIPT FINISHED ---")
        return

    # Sort changed_ids so PLANNING (1) and COMPLETED (2) run FIRST, and CURRENT/WATCHING (5) runs LAST!
    changed_ids.sort(key=lambda x: STATUS_PRIORITY.get(current_state[x]["status"], 3))

    # STEP 1: Process Approved Deletions in Batches
    for del_batch in chunk_list(deleted_ids, BATCH_SIZE):
        if time.time() - start_time >= MAX_RUNTIME_SECONDS:
            print("⏳ Time budget (4m) reached during deletions. Saving progress; remaining items will sync next run!")
            print("--- SCRIPT FINISHED (PARTIAL PASS) ---")
            return

        del_payload = []
        for old_id in del_batch:
            old_data = working_state[old_id]
            old_title = old_data.get("title", f"ID {old_id}") if isinstance(old_data, dict) else f"ID {old_id}"
            print(f"🗑️ Deleted on AniList: '{old_title}'. Removing from Simkl.")
            ids = {"anilist": str(old_id)}
            if isinstance(old_data, dict) and old_data.get("mal_id"):
                ids["mal"] = str(old_data["mal_id"])
            del_payload.append({"ids": ids})

        if push_to_simkl(SIMKL_REMOVE_URL, {"anime": del_payload}, auth_data):
            for old_id in del_batch:
                working_state.pop(old_id, None)
            save_json_atomic(STATE_FILE, working_state)
            time.sleep(1.5)
        else:
            print("⚠️ Deletion batch paused. Saved completed progress; remaining items will retry next run.")
            return

    # STEP 2: Process Additions & Updates in Batches
    total_batches = (len(changed_ids) + BATCH_SIZE - 1) // BATCH_SIZE
    for batch_idx, change_batch in enumerate(chunk_list(changed_ids, BATCH_SIZE), start=1):
        elapsed = time.time() - start_time
        if elapsed >= MAX_RUNTIME_SECONDS:
            remaining = len(changed_ids) - (batch_idx - 1) * BATCH_SIZE
            print(f"⏳ Time limit safety net triggered ({elapsed:.0f}s elapsed). Passing cleanly — {remaining} remaining anime will sync on next run!")
            print("--- SCRIPT FINISHED (PARTIAL PASS) ---")
            return

        print(f"📦 Processing batch {batch_idx}/{total_batches} ({len(change_batch)} anime)...")
        remove_payload = []
        history_payload = []
        list_payload = []

        for a_id in change_batch:
            data = current_state[a_id]
            title = data["title"]
            air_status = data.get("air_status")
            target_prog = 0 if air_status == "NOT_YET_RELEASED" else data["progress"]
            target_stat = resolve_simkl_target_status(data["status"], air_status)

            simkl_info = None
            if live_simkl:
                simkl_info = live_simkl["by_anilist"].get(a_id)
                if not simkl_info and data.get("mal_id"):
                    simkl_info = live_simkl["by_mal"].get(data["mal_id"])

            ids_obj = build_ids(a_id, data, simkl_info)

            if simkl_info:
                curr_prog = simkl_info["progress"]
                curr_stat = simkl_info["status"]

                if curr_stat == target_stat and target_prog > curr_prog and target_stat != "plantowatch":
                    print(f"📈 Progress update for '{title}': Ep {curr_prog} -> {target_prog}")
                    eps = [{"number": ep} for ep in range(curr_prog + 1, target_prog + 1)]
                    history_payload.append({"title": title, "ids": ids_obj, "episodes": eps})
                else:
                    print(f"🔄 Rewriting '{title}' (Simkl: {curr_stat}/{curr_prog} ep -> Target: {target_stat}/{target_prog} ep)")
                    remove_payload.append({"title": title, "ids": ids_obj})
                    if target_prog > 0 and target_stat != "plantowatch":
                        eps = [{"number": ep} for ep in range(1, target_prog + 1)]
                        history_payload.append({"title": title, "ids": ids_obj, "episodes": eps})
                    list_payload.append({"title": title, "ids": ids_obj, "to": target_stat})
            else:
                # Skip pushing if we already synced this item before and Simkl merged it into a parent show
                prev = working_state.get(a_id, {})
                if isinstance(prev, dict) and prev.get("synced_v8", False):
                    print(f"⏭️ Skipping '{title}' (Merged into parent series on Simkl; avoiding parent overwrite).")
                    continue

                print(f"➕ Adding '{title}': {target_stat} ({target_prog} ep)")
                if target_prog > 0 and target_stat != "plantowatch":
                    eps = [{"number": ep} for ep in range(1, target_prog + 1)]
                    history_payload.append({"title": title, "ids": ids_obj, "episodes": eps})
                list_payload.append({"title": title, "ids": ids_obj, "to": target_stat})

        # Execute batch in strict order: 1. Remove -> 2. History -> 3. List Status
        batch_ok = True
        if remove_payload:
            if not push_to_simkl(SIMKL_REMOVE_URL, {"anime": remove_payload}, auth_data):
                batch_ok = False
            time.sleep(1.5)

        if batch_ok and history_payload:
            if not push_to_simkl(SIMKL_HISTORY_URL, {"anime": history_payload}, auth_data):
                batch_ok = False
            time.sleep(1.5)

        if batch_ok and list_payload:
            if not push_to_simkl(SIMKL_LIST_URL, {"shows": list_payload}, auth_data):
                batch_ok = False
            time.sleep(1.5)

        if batch_ok:
            for a_id in change_batch:
                working_state[a_id] = current_state[a_id]
            save_json_atomic(STATE_FILE, working_state)
            print(f"💾 Batch {batch_idx}/{total_batches} saved to {STATE_FILE}.")
        else:
            print(f"⚠️ Batch {batch_idx} encountered an API/rate limit. Saved all previous batches cleanly — remaining items will finish next run!")
            print("--- SCRIPT FINISHED (PARTIAL PASS) ---")
            return

    # Refresh live Simkl list after updates and write the updated rules report
    time.sleep(2)
    updated_live_simkl = get_live_simkl_anime(auth_data)
    generate_rules_report(current_state, updated_live_simkl or live_simkl)

    print("🎉 All batches completed and saved to sync_state_v2.json!")
    print("--- SCRIPT FINISHED ---")

if __name__ == "__main__":
    main()
