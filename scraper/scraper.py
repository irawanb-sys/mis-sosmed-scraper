"""
Social Media Intelligence Scraper - FIXED v2
Perbaikan:
  1. SECURITY: semua API key/secret dipindah ke environment variable
     (JANGAN hardcode secret di file yang masuk ke GitHub).
  2. TikTok (Apify clockworks/tiktok-scraper): field input yang dipakai
     sebelumnya SALAH -> "maxItems" (tidak ada di schema actor ini,
     yang benar "resultsPerPage") dan "searchSection": "videos" (bukan
     opsi valid, yang benar "/video"). Akibatnya actor sering jalan
     dengan resultsPerPage default = 1, atau gagal validasi input.
  3. Twitter/X: Nitter (RSS) sudah sangat tidak reliable di 2026 -
     mayoritas instance publik mati / RSS dimatikan / X makin agresif
     blokir. Diganti ke Apify actor (apidojo/tweet-scraper) yang lebih
     stabil. Tetap perlu APIFY_API_KEY dan ini actor berbayar
     (~$0.30-3/1000 tweet tergantung actor, cek pricing tab di Apify
     sebelum jalan volume besar).
  4. Helper run_apify_actor() dibuat generik + ada pengecekan status
     run (SUCCEEDED/FAILED/TIMED-OUT) sehingga tidak diam-diam
     mengambil dataset kosong dari run yang gagal.
"""

import os, json, time, requests, xml.etree.ElementTree as ET
from datetime import datetime, date, timedelta

# ── Config dari ENVIRONMENT VARIABLE (jangan hardcode!) ────────
SUPABASE_URL    = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY    = os.environ.get("SUPABASE_KEY", "")   # service_role key -> WAJIB rahasia
YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY", "")
GEMINI_API_KEY  = os.environ.get("GEMINI_API_KEY", "")
APIFY_API_KEY   = os.environ.get("APIFY_API_KEY", "")

missing = [k for k, v in {
    "SUPABASE_URL": SUPABASE_URL, "SUPABASE_KEY": SUPABASE_KEY,
    "YOUTUBE_API_KEY": YOUTUBE_API_KEY, "GEMINI_API_KEY": GEMINI_API_KEY,
    "APIFY_API_KEY": APIFY_API_KEY,
}.items() if not v]
if missing:
    print(f"⚠️  Env var belum di-set: {missing} — set di GitHub Actions Secrets / .env lokal (jangan commit .env)")

TODAY = date.today().isoformat()

HEADERS_SB = {
    "apikey":        SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type":  "application/json",
}

# ── Supabase helpers ───────────────────────────────────────────

def sb_get(table, params={}):
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    r = requests.get(url, headers=HEADERS_SB, params=params)
    r.raise_for_status()
    return r.json()

def sb_insert(table, rows):
    if not rows: return
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    r = requests.post(url, headers={**HEADERS_SB, "Prefer": "return=minimal"}, json=rows)
    r.raise_for_status()
    print(f"  ✅ {len(rows)} rows → {table}")

def sb_upsert(table, rows, on_conflict):
    if not rows: return
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    r = requests.post(url,
        headers={**HEADERS_SB, "Prefer": "resolution=merge-duplicates,return=minimal"},
        params={"on_conflict": on_conflict}, json=rows)
    r.raise_for_status()
    print(f"  ✅ upsert {len(rows)} rows → {table}")

# ── Gemini Sentiment ───────────────────────────────────────────

def analyze_sentiment_batch(texts):
    if not GEMINI_API_KEY or not texts:
        return [{"label": "netral", "score": 0.0}] * len(texts)

    # FIX: gemini-1.5-flash sudah di-shutdown total oleh Google (semua request balik 404).
    # gemini-3.1-flash-lite: murah, cepat, belum ada tanggal retirement yang diumumkan.
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-lite:generateContent?key={GEMINI_API_KEY}"
    results = []

    for i in range(0, len(texts), 10):
        batch = texts[i:i+10]
        numbered = "\n".join(f"{j+1}. {t[:300]}" for j, t in enumerate(batch))
        prompt = (
            f"Analisis sentimen teks berikut terhadap produk makanan/sosis Indonesia.\n"
            f"Balas HANYA JSON array tanpa markdown. Label: positif/netral/negatif. "
            f"Skor float -1.0 sampai 1.0.\n"
            f"Contoh: [{{\"label\":\"positif\",\"score\":0.8}}]\n\n"
            f"Teks:\n{numbered}\n\nOutput {len(batch)} elemen:"
        )
        try:
            resp = requests.post(url, json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0.1, "maxOutputTokens": 500}
            }, timeout=30)
            resp.raise_for_status()
            raw = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
            raw = raw.strip().strip("```json").strip("```").strip()
            results.extend(json.loads(raw))
        except Exception as e:
            print(f"  ⚠️ Gemini: {e}")
            results.extend([{"label": "netral", "score": 0.0}] * len(batch))
        time.sleep(1.5)

    return results

# ── YouTube ────────────────────────────────────────────────────

def scrape_youtube(keyword, keyword_id):
    print(f"  ▶ YouTube: '{keyword}'")
    params = {
        "q": keyword, "part": "snippet", "type": "video",
        "maxResults": 20, "order": "date",
        "publishedAfter": (datetime.now()-timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "key": YOUTUBE_API_KEY
    }
    try:
        r = requests.get("https://www.googleapis.com/youtube/v3/search", params=params, timeout=15)
        r.raise_for_status()
        items = r.json().get("items", [])
    except Exception as e:
        print(f"    ⚠️ YouTube error: {e}"); return []

    mentions = []
    for it in items:
        s   = it.get("snippet", {})
        vid = it.get("id", {}).get("videoId", "")
        mentions.append({
            "keyword_id": keyword_id, "keyword": keyword, "platform": "youtube",
            "content": f"{s.get('title','')} {s.get('description','')}".strip()[:1000],
            "author": s.get("channelTitle"), "url": f"https://youtube.com/watch?v={vid}",
            "published_at": s.get("publishedAt"), "sentiment": "netral", "sentiment_score": 0.0,
            "likes": 0, "comments": 0, "views": 0,
        })

    if items:
        vids = ",".join(it.get("id",{}).get("videoId","") for it in items if it.get("id",{}).get("videoId"))
        try:
            sr = requests.get("https://www.googleapis.com/youtube/v3/videos",
                params={"id": vids, "part": "statistics", "key": YOUTUBE_API_KEY}, timeout=10)
            for si in sr.json().get("items", []):
                st = si.get("statistics", {})
                for m in mentions:
                    if si["id"] in (m.get("url") or ""):
                        m["likes"]    = int(st.get("likeCount", 0))
                        m["comments"] = int(st.get("commentCount", 0))
                        m["views"]    = int(st.get("viewCount", 0))
        except: pass

    print(f"    → {len(mentions)} video"); return mentions

# ── Apify generic runner (dipakai TikTok, Instagram, Twitter) ──

def run_apify_actor(actor_slug, payload, max_wait_seconds=180, poll_interval=5):
    """
    Jalankan Apify actor, polling sampai SUCCEEDED, lalu ambil dataset items.
    Mengembalikan (items, status) — kalau status bukan SUCCEEDED, items = []
    dan alasannya di-print supaya tidak diam-diam dianggap "tidak ada mention".
    """
    try:
        run = requests.post(f"https://api.apify.com/v2/acts/{actor_slug}/runs",
            params={"token": APIFY_API_KEY}, json=payload, timeout=30)
        run.raise_for_status()
        run_id = run.json()["data"]["id"]
    except Exception as e:
        print(f"    ⚠️ Apify[{actor_slug}] gagal start run: {e}")
        return [], "START_ERROR"

    elapsed = 0
    status = "RUNNING"
    while elapsed < max_wait_seconds:
        time.sleep(poll_interval)
        elapsed += poll_interval
        try:
            st = requests.get(f"https://api.apify.com/v2/actor-runs/{run_id}",
                    params={"token": APIFY_API_KEY}, timeout=15).json()["data"]
            status = st["status"]
        except Exception as e:
            print(f"    ⚠️ Apify[{actor_slug}] polling error: {e}")
            continue
        if status in ("SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"):
            break

    if status != "SUCCEEDED":
        print(f"    ⚠️ Apify[{actor_slug}] run berstatus '{status}' (bukan SUCCEEDED) — cek input/payload atau quota Apify")
        return [], status

    dataset_id = st["defaultDatasetId"]
    try:
        items = requests.get(f"https://api.apify.com/v2/datasets/{dataset_id}/items",
            params={"token": APIFY_API_KEY}, timeout=30).json()
    except Exception as e:
        print(f"    ⚠️ Apify[{actor_slug}] gagal ambil dataset: {e}")
        return [], "DATASET_ERROR"

    return items, "SUCCEEDED"

# ── Twitter / X (via Apify, pengganti Nitter yang sudah tidak reliable) ──

def scrape_twitter(keyword, keyword_id):
    print(f"  ✦ Twitter/X (Apify apidojo/tweet-scraper): '{keyword}'")
    if not APIFY_API_KEY:
        print("    → skip (APIFY_API_KEY tidak ada)"); return []

    items, status = run_apify_actor("apidojo~tweet-scraper", {
        "searchTerms": [keyword],
        "sort": "Latest",
        "maxItems": 20,
    }, max_wait_seconds=180)

    if status != "SUCCEEDED":
        return []

    mentions = []
    for it in items:
        author = (it.get("author") or {})
        mentions.append({
            "keyword_id": keyword_id, "keyword": keyword, "platform": "twitter",
            "content": (it.get("text") or "")[:800],
            "author": author.get("userName") or author.get("name"),
            "url": it.get("url") or it.get("twitterUrl"),
            "published_at": it.get("createdAt") or datetime.now().isoformat(),
            "sentiment": "netral", "sentiment_score": 0.0,
            "likes": int(it.get("likeCount", 0) or 0),
            "comments": int(it.get("replyCount", 0) or 0),
            "views": int(it.get("viewCount", 0) or 0),
        })
    print(f"    → {len(mentions)} tweet"); return mentions

# ── TikTok (FIXED: resultsPerPage, bukan maxItems; searchSection "/video") ──

def scrape_tiktok(keyword, keyword_id):
    if not APIFY_API_KEY:
        print(f"  ♪ TikTok: skip (APIFY_API_KEY tidak ada)"); return []
    print(f"  ♪ TikTok (Apify): '{keyword}'")

    items, status = run_apify_actor("clockworks~tiktok-scraper", {
        "searchQueries": [keyword],
        "searchSection": "/video",     # FIX: dulu "videos" -> bukan opsi valid di actor ini
        "resultsPerPage": 15,          # FIX: dulu "maxItems" -> field ini tidak ada, jadi default ke 1
        "shouldDownloadCovers": False,
    }, max_wait_seconds=180)

    if status != "SUCCEEDED":
        return []

    mentions = []
    for it in items:
        if it.get("errorCode"):   # actor ini mengembalikan item error, bukan exception
            continue
        mentions.append({
            "keyword_id": keyword_id, "keyword": keyword, "platform": "tiktok",
            "content": f"{it.get('text','')}".strip()[:800],
            "author": (it.get("authorMeta") or {}).get("name"),
            "url": it.get("webVideoUrl"),
            "published_at": datetime.fromtimestamp(it.get("createTime", 0)).isoformat() if it.get("createTime") else datetime.now().isoformat(),
            "sentiment": "netral", "sentiment_score": 0.0,
            "likes": int(it.get("diggCount", 0) or 0),
            "comments": int(it.get("commentCount", 0) or 0),
            "views": int(it.get("playCount", 0) or 0),
            "shares": int(it.get("shareCount", 0) or 0),
        })
    print(f"    → {len(mentions)} video TikTok"); return mentions

# ── Instagram ───────────────────────────────────────────────────

def scrape_instagram(keyword, keyword_id):
    if not APIFY_API_KEY:
        print(f"  ◉ Instagram: skip (APIFY_API_KEY tidak ada)"); return []
    print(f"  ◉ Instagram (Apify): '#{keyword}'")

    items, status = run_apify_actor("apify~instagram-hashtag-scraper", {
        "hashtags": [keyword.replace(" ", "")],
        "resultsLimit": 15,
        "shouldDownloadVideos": False,
    }, max_wait_seconds=180)

    if status != "SUCCEEDED":
        return []

    mentions = []
    for it in items:
        mentions.append({
            "keyword_id": keyword_id, "keyword": keyword, "platform": "instagram",
            "content": (it.get("caption") or "")[:800],
            "author": it.get("ownerUsername"), "url": it.get("url"),
            "published_at": it.get("timestamp") or datetime.now().isoformat(),
            "sentiment": "netral", "sentiment_score": 0.0,
            "likes": int(it.get("likesCount", 0) or 0),
            "comments": int(it.get("commentsCount", 0) or 0),
            "views": int(it.get("videoViewCount", 0) or 0),
        })
    print(f"    → {len(mentions)} post Instagram"); return mentions

# ── Web / Google News RSS (tidak diubah — ini tidak pakai Apify) ──

def scrape_web(keyword, keyword_id):
    from urllib.parse import quote
    print(f"  🌐 Google News: '{keyword}'")
    q   = quote(f'"{keyword}"')
    url = f"https://news.google.com/rss/search?q={q}&hl=id&gl=ID&ceid=ID:id"
    try:
        r    = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        root = ET.fromstring(r.content)
        mentions = []
        for item in root.findall(".//item")[:15]:
            title   = item.findtext("title") or ""
            desc    = item.findtext("description") or ""
            link    = item.findtext("link") or ""
            pubdate = item.findtext("pubDate") or ""
            source  = item.findtext("source") or ""
            try:
                from email.utils import parsedate_to_datetime
                pub_iso = parsedate_to_datetime(pubdate).isoformat()
            except: pub_iso = datetime.now().isoformat()
            mentions.append({
                "keyword_id": keyword_id, "keyword": keyword, "platform": "web",
                "content": f"{title}. {desc}".strip()[:800], "author": source,
                "url": link, "published_at": pub_iso,
                "sentiment": "netral", "sentiment_score": 0.0,
                "likes": 0, "comments": 0, "views": 0,
            })
        print(f"    → {len(mentions)} artikel"); return mentions
    except Exception as e:
        print(f"    ⚠️ Web: {e}"); return []

# ── Update Trend Daily ─────────────────────────────────────────

def update_trend(keyword, keyword_id, mentions):
    today_m = [m for m in mentions if (m.get("published_at") or "")[:10] == TODAY]
    rows = []
    for plat in ["youtube", "tiktok", "twitter", "instagram", "web"]:
        pm = [m for m in today_m if m["platform"] == plat]
        if not pm: continue
        rows.append({
            "keyword_id": keyword_id, "keyword": keyword,
            "trend_date": TODAY, "platform": plat,
            "total_mention": len(pm),
            "positif": sum(1 for m in pm if m["sentiment"] == "positif"),
            "netral":  sum(1 for m in pm if m["sentiment"] == "netral"),
            "negatif": sum(1 for m in pm if m["sentiment"] == "negatif"),
            "avg_sentiment": sum(m["sentiment_score"] for m in pm) / len(pm),
            "total_likes": sum(m["likes"] for m in pm),
            "total_views": sum(m["views"] for m in pm),
        })
    if today_m:
        rows.append({
            "keyword_id": keyword_id, "keyword": keyword,
            "trend_date": TODAY, "platform": "all",
            "total_mention": len(today_m),
            "positif": sum(1 for m in today_m if m["sentiment"] == "positif"),
            "netral":  sum(1 for m in today_m if m["sentiment"] == "netral"),
            "negatif": sum(1 for m in today_m if m["sentiment"] == "negatif"),
            "avg_sentiment": sum(m["sentiment_score"] for m in today_m) / len(today_m),
            "total_likes": sum(m["likes"] for m in today_m),
            "total_views": sum(m["views"] for m in today_m),
        })
    if rows:
        sb_upsert("sm_trend_daily", rows, "keyword_id,trend_date,platform")

# ── Main ───────────────────────────────────────────────────────

def main():
    print(f"🚀 Scraping — {TODAY}")
    keywords = sb_get("sm_keywords", {"is_active": "eq.true"})
    if not keywords:
        print("Tidak ada keyword aktif."); return
    print(f"📋 {len(keywords)} keyword: {[k['keyword'] for k in keywords]}\n")

    for kw in keywords:
        keyword, keyword_id = kw["keyword"], kw["id"]
        print(f"🔍 '{keyword}'")

        all_m  = []
        all_m += scrape_youtube(keyword, keyword_id)
        all_m += scrape_twitter(keyword, keyword_id)
        all_m += scrape_tiktok(keyword, keyword_id)
        all_m += scrape_instagram(keyword, keyword_id)
        all_m += scrape_web(keyword, keyword_id)

        if not all_m:
            print(f"  → Tidak ada mention\n"); continue

        print(f"\n  🤖 Analisis sentimen {len(all_m)} mention...")
        sentiments = analyze_sentiment_batch([m.get("content", "") for m in all_m])
        for i, m in enumerate(all_m):
            if i < len(sentiments):
                m["sentiment"]       = sentiments[i].get("label", "netral")
                m["sentiment_score"] = float(sentiments[i].get("score", 0.0))

        sb_insert("sm_mentions", all_m)
        update_trend(keyword, keyword_id, all_m)
        print(f"  ✅ '{keyword}' — {len(all_m)} mention\n")
        time.sleep(2)

    print(f"🎉 Selesai — {TODAY}")

if __name__ == "__main__":
    main()
