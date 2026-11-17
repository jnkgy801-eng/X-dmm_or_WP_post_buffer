#!/usr/bin/env python3
"""FANZA動画のNTRタグ作品を、出演女優ごとに集計してCSVに出力する。WordPressには何も送らない。

集計するもの(女優ごと):
  - NTRタグ作品の本数 / 直近1年の本数 / 単体作品の本数
  - 「誘う側」寄りタグ(痴女・逆ナン)が併記された作品の割合
  - 「人妻・不倫」寄りタグ(人妻・主婦、若妻・幼妻、不倫)が併記された作品の割合
  - レビュー平均・レビュー総数・レビュー件数が最多の作品(代表作)
タグの併記率はあくまで傾向の目安で、「寝取る側/寝取られる側」を断定するものではない。

使い方(環境変数 DMM_API_ID / DMM_AFFILIATE_ID が必要):
  python navi_actress.py --config navi_config_video.json
  python navi_actress.py --pages 5 --months 12 --min-works 3 --out actress_stats.csv
出力のCSVはExcelでそのまま開ける(UTF-8 BOM付き)。

WordPressの女優ページに反映する場合(環境変数 NAVI_SITE_URL / NAVI_API_TOKEN も必要):
  python navi_actress.py --config navi_config_video.json --pages 30 --push
  (単体作品が --push-min-solo 本以上の女優を、上位から --max-push 人まで送る)
"""
import argparse
import csv
import json
from collections import defaultdict
from datetime import date, timedelta

import navi_sync as ns

NTR_GENRE_ID = 4111          # 寝取り・寝取られ・NTR
AGGRESSIVE_IDS = {1031, 4120}  # 痴女, 逆ナン
WIFE_IDS = {1039, 1020, 1069}  # 人妻・主婦, 若妻・幼妻, 不倫
SOLO_ID = 4025                 # 単体作品


def fetch_pages(api, t, pages, cutoff):
    for p in range(pages):
        res = api.get(
            "ItemList",
            site=t.get("site", "FANZA"), service=t["service"], floor=t["floor"],
            article="genre", article_id=str(NTR_GENRE_ID),
            hits=100, offset=1 + p * 100, sort="date",
        )
        got = res.get("items", [])
        for it in got:
            yield it
        if len(got) < 100:
            return
        last = str(got[-1].get("date", ""))[:10]
        if last and last < cutoff:
            return


def new_stat():
    return {"name": "", "ruby": "", "works": 0, "recent": 0, "solo": 0, "aggr": 0, "wife": 0,
            "rev_n": 0, "rev_sum": 0.0, "top": (-1, ""), "wl": []}


def actress_image(api, aid):
    """ActressSearch APIで女優の写真URLを取る(なければ空文字)"""
    try:
        res = api.get("ActressSearch", actress_id=aid, hits=1)
    except Exception as e:  # 写真が取れなくても全体は止めない
        print("photo skipped (%s): %s" % (aid, e))
        return ""
    for a in res.get("actress") or []:
        img = a.get("imageURL") or {}
        return (img.get("large") or img.get("small") or "").replace("http://", "https://")
    return ""


def push_actresses(site, token, period, items):
    for i in range(0, len(items), 20):
        r = ns.requests.post(
            "%s/wp-json/navi/v1/actress-upsert" % site.rstrip("/"),
            json={"period_months": period, "items": items[i:i + 20]},
            headers={"X-Navi-Token": token, "User-Agent": ns.UA},
            timeout=120,
        )
        print(r.status_code, r.text[:300])
        r.raise_for_status()


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="navi_config_video.json")
    ap.add_argument("--pages", type=int, default=10, help="取得ページ数(1ページ100件)")
    ap.add_argument("--months", type=int, default=24, help="何か月前までの作品を集計するか")
    ap.add_argument("--min-works", type=int, default=5, help="CSVに出す最低NTR作品数")
    ap.add_argument("--out", default="actress_stats.csv")
    ap.add_argument("--push", action="store_true", help="集計結果をWordPressの女優ページに送る")
    ap.add_argument("--push-min-solo", type=int, default=5, help="送る女優の最低単体作品数")
    ap.add_argument("--max-push", type=int, default=150, help="送る女優の最大人数")
    ap.add_argument("--no-photos", action="store_true", help="女優の写真を取得しない(速くなる)")
    args = ap.parse_args(argv)

    api = ns.Api(ns.env("DMM_API_ID"), ns.env("DMM_AFFILIATE_ID"))
    with open(args.config, encoding="utf-8") as f:
        cfg = json.load(f)
    t = cfg["targets"][0]
    exclude = cfg.get("exclude_genre_keywords", [])

    today = date.today().isoformat()
    cutoff = (date.today() - timedelta(days=30 * args.months)).isoformat()
    year_ago = (date.today() - timedelta(days=365)).isoformat()

    stats = defaultdict(new_stat)
    seen = set()
    used = 0
    for it in fetch_pages(api, t, args.pages, cutoff):
        cid = str(it.get("content_id") or "")
        if not cid or cid in seen:
            continue
        seen.add(cid)
        d = str(it.get("date", ""))[:10]
        if not d or d > today or d < cutoff:  # 予約中(未来日)と古い作品は集計しない
            continue
        if ns.is_excluded(it, exclude):
            continue
        info = it.get("iteminfo") or {}
        actresses = info.get("actress") or []
        if not actresses:
            continue
        gids = set()
        for g in info.get("genre") or []:
            try:
                gids.add(int(g.get("id")))
            except (TypeError, ValueError):
                pass
        review = it.get("review") or {}
        rc = ns.to_int(review.get("count"))
        try:
            avg = float(review.get("average") or 0)
        except ValueError:
            avg = 0.0
        title = str(it.get("title") or "")
        img = it.get("imageURL") or {}
        work = {
            "cid": cid,
            "title": title,
            "image": (img.get("large") or img.get("list") or img.get("small") or "").replace("http://", "https://"),
            "aff_url": it.get("affiliateURL") or it.get("URL") or "",
            "date": d,
            "solo": SOLO_ID in gids,
            "rc": rc,
            "avg": avg,
        }
        used += 1
        for a in actresses:
            aid = str(a.get("id") or "")
            if not aid:
                continue
            s = stats[aid]
            s["name"] = str(a.get("name") or "")
            s["ruby"] = str(a.get("ruby") or "")
            s["wl"].append(work)
            s["works"] += 1
            s["recent"] += 1 if d >= year_ago else 0
            s["solo"] += 1 if SOLO_ID in gids else 0
            s["aggr"] += 1 if gids & AGGRESSIVE_IDS else 0
            s["wife"] += 1 if gids & WIFE_IDS else 0
            s["rev_n"] += rc
            s["rev_sum"] += avg * rc
            if rc > s["top"][0]:
                s["top"] = (rc, title)

    rows = []
    for aid, s in stats.items():
        if s["works"] < args.min_works:
            continue
        rows.append([
            aid, s["name"], s["works"], s["recent"], s["solo"],
            round(s["aggr"] / s["works"] * 100), round(s["wife"] / s["works"] * 100),
            round(s["rev_sum"] / s["rev_n"], 2) if s["rev_n"] else "", s["rev_n"], s["top"][1],
        ])
    rows.sort(key=lambda r: (r[2], r[3]), reverse=True)

    payload = []
    for r in rows:
        aid = r[0]
        s = stats[aid]
        if s["solo"] < args.push_min_solo:
            continue
        wl = sorted(s["wl"], key=lambda w: (w["solo"], w["rc"], w["date"]), reverse=True)[:12]
        payload.append({
            "aid": aid, "name": s["name"], "ruby": s["ruby"],
            "ntr_works": s["works"], "recent": s["recent"], "solo": s["solo"],
            "aggr_pct": round(s["aggr"] / s["works"] * 100), "wife_pct": round(s["wife"] / s["works"] * 100),
            "review_avg": round(s["rev_sum"] / s["rev_n"], 2) if s["rev_n"] else 0,
            "review_n": s["rev_n"], "top_title": s["top"][1], "works": wl,
        })
    payload.sort(key=lambda x: (x["solo"], x["ntr_works"]), reverse=True)
    payload = payload[: args.max_push]

    with open(args.out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["女優ID", "女優名", "NTR作品数", "直近1年", "単体作品数",
                    "誘う側タグ率(%)", "人妻不倫タグ率(%)", "レビュー平均", "レビュー総数", "代表作(レビュー最多)"])
        w.writerows(rows)

    print("works used: %d / actresses: %d / rows written: %d -> %s" % (used, len(stats), len(rows), args.out))
    if args.push:
        if not args.no_photos:
            for x in payload:
                x["image"] = actress_image(api, x["aid"])
        push_actresses(ns.env("NAVI_SITE_URL"), ns.env("NAVI_API_TOKEN"), args.months, payload)
        print("pushed: %d actresses" % len(payload))


if __name__ == "__main__":
    main()
