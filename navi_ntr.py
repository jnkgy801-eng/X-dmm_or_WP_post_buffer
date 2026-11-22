#!/usr/bin/env python3
"""FANZA動画のNTRタグ作品を取得し、サイト(WordPress / プラグイン navi-happyend-hub)へ登録する。

1回の取得(NTRジャンルの新着順)から、次の2つを作って送る。
  - 作品  : 新作・高評価・セール・シチュエーション別ページの元データ (POST /navi/v1/upsert)
  - 女優  : NTR作品数・単体作品数・レビュー・得意なシチュエーション (POST /navi/v1/actress-upsert)

シチュエーション分類は、FANZAのタグ名とタイトルの語句一致による目安(navi_config.json の situations)。
作品内容を断定するものではない。除外キーワード(exclude_keywords)に当たる作品は、作品・女優の両方から外す。

使い方(環境変数 DMM_API_ID / DMM_AFFILIATE_ID が必要。送信には NAVI_SITE_URL / NAVI_API_TOKEN も必要):
  python navi_ntr.py --dry-run              # 取得と集計だけ。送らない(最初はこれで確認)
  python navi_ntr.py                        # 取得して送る
  python navi_ntr.py --list-genres          # videoaフロアのジャンル(タグ)一覧
  python navi_ntr.py --csv actress.csv      # 女優別の集計をCSVにも出す(Excelで開ける)
"""
import argparse
import csv
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import date, timedelta

import requests

API = "https://api.dmm.com/affiliate/v3"
UA = "navi-ntr/1.0"
AGGRESSIVE_IDS = {1031, 4120}  # 痴女, 逆ナン
WIFE_IDS = {1039, 1020, 1069}  # 人妻・主婦, 若妻・幼妻, 不倫
SOLO_ID = 4025                 # 単体作品


def env(name, required=True):
    v = os.environ.get(name, "").strip()
    if required and not v:
        sys.exit("環境変数 %s が未設定です" % name)
    return v


class Api:
    def __init__(self, api_id, affiliate_id):
        self.base = {"api_id": api_id, "affiliate_id": affiliate_id, "output": "json"}
        self.session = requests.Session()
        self.session.headers["User-Agent"] = UA

    def get(self, path, **params):
        time.sleep(1.0)  # APIに優しく
        r = self.session.get("%s/%s" % (API, path), params={**self.base, **params}, timeout=30)
        r.raise_for_status()
        res = r.json().get("result", {})
        if str(res.get("status", "200")) != "200":
            raise RuntimeError("DMM API error (%s): %s" % (path, res.get("message")))
        return res


def to_int(v):
    m = re.search(r"\d[\d,]*", str(v if v is not None else ""))
    return int(m.group(0).replace(",", "")) if m else 0


def genre_names(it):
    return [str(g.get("name", "")) for g in ((it.get("iteminfo") or {}).get("genre") or [])]


def is_excluded(it, keywords):
    """除外キーワードが、タグまたはタイトルに含まれる作品か"""
    title = str(it.get("title") or "")
    return any(k in g for g in genre_names(it) for k in keywords) or any(k in title for k in keywords)


def situations_of(genres, title, rules):
    out = []
    for key, r in rules.items():
        if any(g in genres for g in r.get("genres", [])) or any(k in title for k in r.get("title", [])):
            out.append(key)
    return out


def sale_end_of(it):
    today = date.today().isoformat()
    for c in it.get("campaign") or []:
        end = str(c.get("date_end") or "")[:10]
        if re.match(r"^\d{4}-\d{2}-\d{2}$", end) and end >= today:
            return end
    return ""


def price_range(it):
    """配信タイプ別の価格から、最安〜最高を返す(取れなければ price を使う)"""
    prices = it.get("prices") or {}
    ds = (prices.get("deliveries") or {}).get("delivery") or []
    if isinstance(ds, dict):
        ds = [ds]
    vals = [to_int(d.get("price")) for d in ds if isinstance(d, dict)]
    vals = [v for v in vals if v > 0]
    if not vals:
        v = to_int(prices.get("price"))
        vals = [v] if v > 0 else []
    return (min(vals), max(vals)) if vals else (0, 0)


def sample_info(it):
    """サンプル画像(最大4枚)とサンプル動画ページのURL"""
    imgs = (((it.get("sampleImageURL") or {}).get("sample_s") or {}).get("image")) or []
    if isinstance(imgs, str):
        imgs = [imgs]
    samples = [u.replace("http://", "https://") for u in imgs[:4] if isinstance(u, str)]
    sm = it.get("sampleMovieURL") or {}
    movie = sm.get("size_644_414") or sm.get("size_560_360") or sm.get("size_476_306") or ""
    return samples, str(movie).replace("http://", "https://")


def fetch_items(api, cfg, cutoff, pages):
    for p in range(pages):
        res = api.get(
            "ItemList", site=cfg.get("site", "FANZA"), service=cfg["service"], floor=cfg["floor"],
            article="genre", article_id=str(cfg["ntr_genre_id"]), hits=100, offset=1 + p * 100, sort="date",
        )
        got = res.get("items", [])
        yield from got
        if len(got) < 100:
            return
        last = str(got[-1].get("date", ""))[:10]
        if last and last < cutoff:
            return


def build(api, cfg, pages):
    """取得した作品を、作品リストと女優別の集計にまとめる"""
    today = date.today().isoformat()
    cutoff = (date.today() - timedelta(days=30 * cfg["months"])).isoformat()
    year_ago = (date.today() - timedelta(days=365)).isoformat()
    exclude = cfg.get("exclude_keywords", [])
    rules = cfg.get("situations", {})

    works, stats, seen = [], defaultdict(lambda: {
        "name": "", "ruby": "", "works": 0, "recent": 0, "solo": 0, "aggr": 0, "wife": 0, "rev_n": 0,
        "rev_sum": 0.0, "top": (-1, ""), "latest": "", "sits": Counter(), "wl": []}), set()
    skipped = Counter()
    for it in fetch_items(api, cfg, cutoff, pages):
        cid = str(it.get("content_id") or "")
        if not cid or cid in seen:
            continue
        seen.add(cid)
        d = str(it.get("date", ""))[:10]
        if not d or d > today or d < cutoff:  # 予約中(未来日)と古い作品は対象外
            skipped["date"] += 1
            continue
        if is_excluded(it, exclude):
            skipped["excluded"] += 1
            continue
        info = it.get("iteminfo") or {}
        actresses = info.get("actress") or []
        aff = it.get("affiliateURL") or it.get("URL") or ""
        title = str(it.get("title") or "").strip()
        if not (actresses and aff and title):
            skipped["incomplete"] += 1
            continue
        gids = set()
        for g in info.get("genre") or []:
            try:
                gids.add(int(g.get("id")))
            except (TypeError, ValueError):
                pass
        review = it.get("review") or {}
        rc = to_int(review.get("count"))
        try:
            avg = float(review.get("average") or 0)
        except ValueError:
            avg = 0.0
        img = it.get("imageURL") or {}
        prices = it.get("prices") or {}
        lo, hi = price_range(it)
        samples, movie = sample_info(it)
        sits = situations_of(genre_names(it), title, rules)
        solo = SOLO_ID in gids
        work = {
            "cid": cid, "title": title,
            "image": (img.get("large") or img.get("list") or img.get("small") or "").replace("http://", "https://"),
            "aff_url": aff, "release": d, "rc": rc, "avg": avg, "minutes": to_int(it.get("volume")),
            "maker": str(((info.get("maker") or [{}])[0] or {}).get("name") or ""),
            "solo": solo, "situations": sits, "samples": samples, "movie": movie,
            "price": to_int(prices.get("price")) or lo, "list_price": to_int(prices.get("list_price")),
            "sale_end": sale_end_of(it), "actress": [str(a.get("name") or "") for a in actresses[:3] if a.get("name")],
        }
        works.append(work)
        for a in actresses:
            aid = str(a.get("id") or "")
            if not aid:
                continue
            s = stats[aid]
            s["name"], s["ruby"] = str(a.get("name") or ""), str(a.get("ruby") or "")
            s["works"] += 1
            s["recent"] += 1 if d >= year_ago else 0
            s["solo"] += 1 if solo else 0
            s["aggr"] += 1 if gids & AGGRESSIVE_IDS else 0
            s["wife"] += 1 if gids & WIFE_IDS else 0
            s["rev_n"] += rc
            s["rev_sum"] += avg * rc
            s["latest"] = max(s["latest"], d)
            s["sits"].update(sits)
            if rc > s["top"][0]:
                s["top"] = (rc, title)
            s["wl"].append({**work, "date": d, "rc": rc, "avg": avg, "price_min": lo, "price_max": hi})
    return works, stats, skipped


def actress_payload(stats, cfg):
    """サイトへ送る女優データ(単体作品の多い順に上位 max_actresses 人)"""
    out = []
    for aid, s in stats.items():
        if s["solo"] < cfg.get("min_solo_for_actress", 5):
            continue
        # 代表作の候補: 単体・レビュー数の多い順。ただし新作も2本は必ず含める
        wl = sorted(s["wl"], key=lambda w: (w["solo"], w["rc"], w["date"]), reverse=True)
        top = wl[:10]
        for w in sorted(s["wl"], key=lambda w: w["date"], reverse=True):
            if len(top) >= 12:
                break
            if w not in top:
                top.append(w)
        n = s["works"]
        out.append({
            "aid": aid, "name": s["name"], "ruby": s["ruby"], "ntr_works": n, "recent": s["recent"], "solo": s["solo"],
            "aggr_pct": round(s["aggr"] / n * 100), "wife_pct": round(s["wife"] / n * 100),
            "review_avg": round(s["rev_sum"] / s["rev_n"], 2) if s["rev_n"] else 0,
            "review_n": s["rev_n"], "top_title": s["top"][1], "latest": s["latest"],
            "sits": [k for k, c in s["sits"].most_common(3) if c >= 2],
            "works": [{
                "cid": w["cid"], "title": w["title"], "image": w["image"], "aff_url": w["aff_url"], "date": w["date"],
                "solo": w["solo"], "rc": w["rc"], "avg": w["avg"], "minutes": w["minutes"], "price_min": w["price_min"],
                "price_max": w["price_max"], "maker": w["maker"], "samples": w["samples"], "movie": w["movie"],
            } for w in top],
        })
    out.sort(key=lambda x: (x["solo"], x["ntr_works"]), reverse=True)
    return out[: cfg.get("max_actresses", 150)]


def pick_works(works, cfg):
    """サイトへ送る作品: 直近new_days日の新作はすべて + レビュー件数の多い順で max_works 件まで"""
    new_from = (date.today() - timedelta(days=cfg.get("new_days", 45))).isoformat()
    chosen = {w["cid"]: w for w in works if w["release"] >= new_from}
    for w in sorted(works, key=lambda w: w["rc"], reverse=True):
        if len(chosen) >= cfg.get("max_works", 300):
            break
        chosen.setdefault(w["cid"], w)
    return list(chosen.values())


def actress_profile(api, aid):
    """女優の写真URLとプロフィール(身長・カップ・3サイズ)。取れなくても全体は止めない"""
    try:
        res = api.get("ActressSearch", actress_id=aid, hits=1)
    except Exception as e:  # noqa: BLE001
        print("profile skipped (%s): %s" % (aid, e))
        return {}
    for a in res.get("actress") or []:
        img = a.get("imageURL") or {}
        out = {}
        photo = (img.get("large") or img.get("small") or "").replace("http://", "https://")
        if photo:
            out["image"] = photo
        for k in ("height", "bust", "waist", "hip"):
            v = to_int(a.get(k))
            if v > 0:
                out[k] = v
        cup = str(a.get("cup") or "").strip()
        if cup:
            out["cup"] = cup
        return out
    return {}


def post(site, token, path, key, items, size, extra=None):
    for i in range(0, len(items), size):
        r = requests.post(
            "%s/wp-json/navi/v1/%s" % (site.rstrip("/"), path),
            json={**(extra or {}), key: items[i:i + size]},
            headers={"X-Navi-Token": token, "User-Agent": UA}, timeout=120,
        )
        print(path, r.status_code, r.text[:200])
        r.raise_for_status()


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="navi_config.json")
    ap.add_argument("--pages", type=int, help="取得ページ数(1ページ100件)。省略時は設定ファイルの値")
    ap.add_argument("--dry-run", action="store_true", help="取得と集計だけ行い、サイトには送らない")
    ap.add_argument("--no-photos", action="store_true", help="女優の写真・プロフィールを取得しない(速くなる)")
    ap.add_argument("--csv", help="女優別の集計をCSVに出力する")
    ap.add_argument("--list-genres", action="store_true", help="フロアのジャンル(タグ)一覧を表示して終了")
    args = ap.parse_args(argv)

    with open(args.config, encoding="utf-8") as f:
        cfg = json.load(f)
    api = Api(env("DMM_API_ID"), env("DMM_AFFILIATE_ID"))

    if args.list_genres:
        floors = api.get("FloorList")
        for s in floors.get("site", []):
            for sv in s.get("service", []):
                for fl in sv.get("floor", []):
                    if (s.get("code"), sv.get("code"), fl.get("code")) == (cfg.get("site", "FANZA"), cfg["service"], cfg["floor"]):
                        for g in api.get("GenreSearch", floor_id=fl["id"], hits=500).get("genre", []):
                            print("%s\t%s" % (g["genre_id"], g["name"]))
                        return
        sys.exit("フロアが見つかりません")

    works, stats, skipped = build(api, cfg, args.pages or cfg.get("pages", 30))
    sends = pick_works(works, cfg)
    actresses = actress_payload(stats, cfg)
    sit_count = Counter(k for w in sends for k in w["situations"])
    print("対象作品 %d件(除外 %d / 期間外・不備 %d) → 送る作品 %d件 / 送る女優 %d人" % (
        len(works), skipped["excluded"], skipped["date"] + skipped["incomplete"], len(sends), len(actresses)))
    print("シチュエーション別:", dict(sit_count))

    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["女優ID", "女優名", "NTR作品数", "直近1年", "単体作品数", "誘う側タグ率(%)", "人妻不倫タグ率(%)",
                        "レビュー平均", "レビュー総数", "最新作", "代表作(レビュー最多)"])
            for a in actresses:
                w.writerow([a["aid"], a["name"], a["ntr_works"], a["recent"], a["solo"], a["aggr_pct"], a["wife_pct"],
                            a["review_avg"], a["review_n"], a["latest"], a["top_title"]])

    if args.dry_run:
        print(json.dumps(sends[:2], ensure_ascii=False, indent=2))
        return

    site, token = env("NAVI_SITE_URL"), env("NAVI_API_TOKEN")
    post(site, token, "upsert", "items", sends, 50)
    if not args.no_photos:
        for a in actresses:
            a.update(actress_profile(api, a["aid"]))
    post(site, token, "actress-upsert", "items", actresses, 20, {"period_months": cfg["months"]})


if __name__ == "__main__":
    main()
