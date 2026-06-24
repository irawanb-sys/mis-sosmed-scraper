"""
Social Media Intelligence Scraper
===================================
Platforms : YouTube (API) | Twitter (Nitter RSS) | TikTok (Apify)
            Instagram (Apify) | Web (Google News RSS)
Sentiment : Gemini 1.5 Flash
Storage   : Supabase mis-survey-v2 (xsfjocfihkdrdxatugzv)
Schedule  : 2x/hari via GitHub Actions
"""

import os, json, time, requests, xml.etree.ElementTree as ET
from datetime import datetime, date, timedelta
from urllib.parse import quote

# ── Config ─────────────────────────────────────────────────────
SUPABASE_URL    = os.environ.get("SUPABASE_URL",    "https://xsfjocfihkdrdxatugzv.supabase.co")
SUPABASE_KEY    = os.environ.get("SUPABASE_KEY",    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InhzZmpvY2ZpaGtkcmR4YXR1Z3p2Iiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc4MTAyNDAxMSwiZXhwIjoyMDk2NjAwMDExfQ.5CYU1UcywW4KXSvPUh183LogP2VShXM2ZxbIMjRgfbo")
YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY", "AIzaSyB8EI2111XEWPQjsi3SU3vHdZ5NLGbJUoo")
GEMINI_API_KEY  = os.environ.get("GEMINI_API_KEY",  "AQ.Ab8RN6KWrY_rcT-xrUnI7ANuDch4wdAiLrzS1BaieinmYNLT5Q")
APIFY_API_KEY   = os.environ.get("APIFY_API_KEY",   "")

TODAY = date.today().isoformat()

HEADERS_SB = {
    "apikey":        SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type":  "application/json",
}

# ── Supabase helpers ───────────────────────────────────────────

def sb_get(table, params={}):
    r = requests.get(f"{SUPABASE_URL}/rest/v1/{table}", headers=HEADERS_SB, params=params)
    r.raise_for_status()
    return r.json()

def sb_insert(table, rows):
    if not rows: return
    r = requests.post(f"{SUPABASE_URL}/rest/v1/{table}",
        headers={**HEADERS_SB, "Prefer": "return=minimal"}, json=rows)
    r.raise_for_status()
    print(f"  ✅ {len(rows)} rows → {table}")

def sb_upsert(table, rows, on_conflict):
    if not rows: return
    r = requests.post(f"{SUPABASE_URL}/rest/v1/{table}",
        headers={**HEADERS_SB, "Prefer": "resolution=merge-duplicates,return=minimal"},
        params={"on_conflict": on_conflict}, json=rows)
    r.raise_for_status()
    print(f"  ✅ upsert {len(rows)} rows → {table}")

# ── Gemini Sentiment ───────────────────────────────────────────

def analyze_sentiment_batch(texts):
    if not GEMINI_API_KEY or not texts:
        return [{"label": "netral", "score": 0.0}] * len(texts)

    url = ("https://generativelanguage.googleapis.com/v1beta/models/"
           f"gemini-1.5-flash:generateContent?key={GEMINI_API_KEY}")
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
        print(f"    ⚠️ {e}"); return []

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
                        m["likes"]    = int(st.get("likeCount",    0))
                        m["comments"] = int(st.get("commentCount", 0))
                        m["views"]    = int(st.get("viewCount",    0))
        except: pass

    print(f"    → {len(mentions)} video"); return mentions

# ── Twitter / Nitter RSS ───────────────────────────────────────

def scrape_twitter(keyword, keyword_id):
    print(f"  ✦ Twitter (Nitter): '{keyword}'")
    instances = ["https://nitter.privacydev.net", "https://nitter.poast.org", "https://nitter.net"]
    for base in instances:
        try:
            q = keyword.replace(" ", "+")
            r = requests.get(f"{base}/search/rss?q={q}&f=tweets",
                    timeout=10, headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code != 200: continue
            root = ET.fromstring(r.content)
            mentions = []
            for item in root.findall(".//item")[:20]:
                title   = item.findtext("title") or ""
                desc    = item.findtext("description") or ""
                link    = item.findtext("link") or ""
                pubdate = item.findtext("pubDate") or ""
                author  = item.findtext("dc:creator",
                            namespaces={"dc":"http://purl.org/dc/elements/1.1/"}) or ""
                try:
                    from email.utils import parsedate_to_datetime
                    pub_iso = parsedate_to_datetime(pubdate).isoformat()
                except: pub_iso = datetime.now().isoformat()
                mentions.append({
                    "keyword_id": keyword_id, "keyword": keyword, "platform": "twitter",
                    "content": f"{title} {desc}".strip()[:800], "author": author,
                    "url": link.replace(base, "https://twitter.com"),
                    "published_at": pub_iso, "sentiment": "netral", "sentiment_score": 0.0,
                    "likes": 0, "comments": 0, "views": 0,
                })
            print(f"    → {len(mentions)} tweet (via {base})"); return mentions
        except Exception as e:
            print(f"    ⚠️ {base}: {e}"); continue
    print("    → semua Nitter instance gagal"); return []

# ── TikTok / Apify ────────────────────────────────────────────

def scrape_tiktok(keyword, keyword_id):
    if not APIFY_API_KEY:
        print(f"  ♪ TikTok: skip (APIFY_API_KEY tidak ada)"); return []
    print(f"  ♪ TikTok (Apify): '{keyword}'")
    try:
        run = requests.post("https://api.apify.com/v2/acts/clockworks~tiktok-scraper/runs",
            params={"token": APIFY_API_KEY},
            json={"searchQueries":[keyword],"searchSection":"videos","maxItems":15,
                  "shouldDownloadCovers":False}, timeout=30)
        run.raise_for_status()
        run_id = run.json()["data"]["id"]
        for _ in range(12):
            time.sleep(5)
            st = requests.get(f"https://api.apify.com/v2/actor-runs/{run_id}",
                    params={"token": APIFY_API_KEY}).json()["data"]
            if st["status"] == "SUCCEEDED": break
        dataset_id = st["defaultDatasetId"]
        items = requests.get(f"https://api.apify.com/v2/datasets/{dataset_id}/items",
            params={"token": APIFY_API_KEY, "limit": 15}).json()
        mentions = []
        for it in items:
            mentions.append({
                "keyword_id": keyword_id, "keyword": keyword, "platform": "tiktok",
                "content": f"{it.get('text','')}".strip()[:800],
                "author": it.get("authorMeta",{}).get("name"),
                "url": it.get("webVideoUrl"),
                "published_at": datetime.fromtimestamp(it.get("createTime",0)).isoformat(),
                "sentiment": "netral", "sentiment_score": 0.0,
                "likes": int(it.get("diggCount",0)), "comments": int(it.get("commentCount",0)),
                "views": int(it.get("playCount",0)), "shares": int(it.get("shareCount",0)),
            })
        print(f"    → {len(mentions)} video TikTok"); return mentions
    except Exception as e:
        print(f"    ⚠️ TikTok: {e}"); return []

# ── Instagram / Apify ─────────────────────────────────────────

def scrape_instagram(keyword, keyword_id):
    if not APIFY_API_KEY:
        print(f"  ◉ Instagram: skip (APIFY_API_KEY tidak ada)"); return []
    print(f"  ◉ Instagram (Apify): '#{keyword}'")
    try:
        run = requests.post("https://api.apify.com/v2/acts/apify~instagram-hashtag-scraper/runs",
            params={"token": APIFY_API_KEY},
            json={"hashtags":[keyword.replace(" ","")],"resultsLimit":15,
                  "shouldDownloadVideos":False}, timeout=30)
        run.raise_for_status()
        run_id = run.json()["data"]["id"]
        for _ in range(12):
            time.sleep(5)
            st = requests.get(f"https://api.apify.com/v2/actor-runs/{run_id}",
                    params={"token": APIFY_API_KEY}).json()["data"]
            if st["status"] == "SUCCEEDED": break
        dataset_id = st["defaultDatasetId"]
        items = requests.get(f"https://api.apify.com/v2/datasets/{dataset_id}/items",
            params={"token": APIFY_API_KEY, "limit": 15}).json()
        mentions = []
        for it in items:
            mentions.append({
                "keyword_id": keyword_id, "keyword": keyword, "platform": "instagram",
                "content": (it.get("caption") or "")[:800],
                "author": it.get("ownerUsername"), "url": it.get("url"),
                "published_at": it.get("timestamp"),
                "sentiment": "netral", "sentiment_score": 0.0,
                "likes": int(it.get("likesCount",0)),
                "comments": int(it.get("commentsCount",0)),
                "views": int(it.get("videoViewCount",0)),
            })
        print(f"    → {len(mentions)} post Instagram"); return mentions
    except Exception as e:
        print(f"    ⚠️ Instagram: {e}"); return []

# ── Web / Google News RSS ──────────────────────────────────────

def scrape_web(keyword, keyword_id):
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
    for plat in ["youtube","tiktok","twitter","instagram","web"]:
        pm = [m for m in today_m if m["platform"] == plat]
        if not pm: continue
        rows.append({
            "keyword_id": keyword_id, "keyword": keyword,
            "trend_date": TODAY, "platform": plat,
            "total_mention": len(pm),
            "positif": sum(1 for m in pm if m["sentiment"]=="positif"),
            "netral":  sum(1 for m in pm if m["sentiment"]=="netral"),
            "negatif": sum(1 for m in pm if m["sentiment"]=="negatif"),
            "avg_sentiment": sum(m["sentiment_score"] for m in pm)/len(pm),
            "total_likes": sum(m["likes"] for m in pm),
            "total_views": sum(m["views"] for m in pm),
        })
    if today_m:
        rows.append({
            "keyword_id": keyword_id, "keyword": keyword,
            "trend_date": TODAY, "platform": "all",
            "total_mention": len(today_m),
            "positif": sum(1 for m in today_m if m["sentiment"]=="positif"),
            "netral":  sum(1 for m in today_m if m["sentiment"]=="netral"),
            "negatif": sum(1 for m in today_m if m["sentiment"]=="negatif"),
            "avg_sentiment": sum(m["sentiment_score"] for m in today_m)/len(today_m),
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
        sentiments = analyze_sentiment_batch([m.get("content","") for m in all_m])
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
