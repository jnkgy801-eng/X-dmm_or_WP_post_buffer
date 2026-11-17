#!/usr/bin/env python3
"""FANZA(DMM) APIのジャンル(タグ)から作品を取得し、WordPressプラグイン(navi-happyend-hub)へ登録する。

Geminiは使わない。結末(寝取り返し/ざまぁ/ハッピーエンド)は、タグとタイトルのキーワード一致で
「候補」を付けるだけ。確信度は最大80に固定しており、プラグインの自動公開しきい値(既定85)に届かない。
つまり登録された作品は必ず「下書き」になり、公開前に管理画面で人が確認する。

使い方:
  環境変数: DMM_API_ID / DMM_AFFILIATE_ID / NAVI_SITE_URL / NAVI_API_TOKEN
  python navi_sync.py --list-floors              # フロア一覧(service/floorのコード確認)
  python navi_sync.py --list-genres <floor_id>   # そのフロアのジャンル(タグ)一覧
  python navi_sync.py --dry-run                  # 取得だけして送らない(最初はこれで確認)
  python navi_sync.py --dump-raw                 # APIが返す作品1件の生データを表示(取れる項目の確認)
  python navi_sync.py                            # 取得してWordPressへ登録
"""
import argparse
import json
import os
import re
import sys
import time
from datetime import date

import requests

API = "https://api.dmm.com/affiliate/v3"
UA = "navi-sync/0.1"
MAX_CONF = 80  # プラグインの自動公開しきい値(既定85)未満に固定する


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
        self._floors = None

    def get(self, path, **params):
        time.sleep(1.0)  # APIに優しく
        r = self.session.get("%s/%s" % (API, path), params={**self.base, **params}, timeout=30)
        r.raise_for_status()
        res = r.json().get("result", {})
        if str(res.get("status", "200")) != "200":
            raise RuntimeError("DMM API error (%s): %s" % (path, res.get("message")))
        return res

    def floors(self):
        if self._floors is None:
            self._floors = self.get("FloorList")
        return self._floors


def print_floors(api):
    for s in api.floors().get("site", []):
        for sv in s.get("service", []):
            for fl in sv.get("floor", []):
                print("site=%s service=%s floor=%s id=%s  (%s / %s)" % (
                    s.get("code"), sv.get("code"), fl.get("code"), fl.get("id"), sv.get("name"), fl.get("name")))


def find_floor_id(api, site, service, floor):
    for s in api.floors().get("site", []):
        if s.get("code") != site:
            continue
        for sv in s.get("service", []):
            if sv.get("code") != service:
                continue
            for fl in sv.get("floor", []):
                if fl.get("code") == floor:
                    return str(fl["id"])
    raise RuntimeError("フロアが見つかりません: %s/%s/%s  (--list-floors で確認してください)" % (site, service, floor))


def list_genres(api, floor_id):
    res = api.get("GenreSearch", floor_id=floor_id, hits=500)
    return [(str(g["genre_id"]), str(g["name"])) for g in res.get("genre", [])]


def fetch_items(api, t, genre_id):
    items = []
    for p in range(int(t.get("pages", 1))):
        res = api.get(
            "ItemList",
            site=t.get("site", "FANZA"), service=t["service"], floor=t["floor"],
            article="genre", article_id=genre_id,
            hits=100, offset=1 + p * 100, sort=t.get("sort", "date"),
        )
        got = res.get("items", [])
        items += got
        if len(got) < 100:
            break
    return items


def to_int(v):
    m = re.search(r"\d[\d,]*", str(v if v is not None else ""))
    return int(m.group(0).replace(",", "")) if m else 0


def sale_end_of(it):
    today = date.today().isoformat()
    for c in it.get("campaign") or []:
        end = str(c.get("date_end") or "")[:10]
        if re.match(r"^\d{4}-\d{2}-\d{2}$", end) and end >= today:
            return end
    return ""


def classify(genres, title, rules):
    """タグ/タイトルのキーワード一致で結末の「候補」を返す。確実な判定ではない。"""
    best = ("other", 0, "結末の手がかりなし")
    for ending, kws in rules.items():
        in_g = [k for k in kws if any(k in g for g in genres)]
        in_t = [k for k in kws if k in title]
        if not (in_g or in_t):
            continue
        conf = 75 if in_g else 55
        if in_g and in_t:
            conf = MAX_CONF
        if conf > best[1]:
            best = (ending, min(conf, MAX_CONF), "%s候補(タグ%s/題名%s)" % (ending, in_g, in_t))
    return best


def is_excluded(it, exclude):
    """除外キーワードがタグまたはタイトルに含まれる作品か"""
    title = str(it.get("title") or "")
    genres = [str(g.get("name", "")) for g in ((it.get("iteminfo") or {}).get("genre") or [])]
    return any(k in g for g in genres for k in exclude) or any(k in title for k in exclude)


def normalize(it, rules, target_names, exclude, trait_whitelist=()):
    cid = str(it.get("content_id") or it.get("product_id") or "")
    title = str(it.get("title") or "").strip()
    aff = it.get("affiliateURL") or it.get("URL") or ""
    if not (cid and title and aff):
        return None
    genres = [str(g.get("name", "")) for g in ((it.get("iteminfo") or {}).get("genre") or [])]
    if is_excluded(it, exclude):
        return None
    prices = it.get("prices") or {}
    img = it.get("imageURL") or {}
    review = it.get("review") or {}
    rcount = to_int(review.get("count"))
    ending, conf, why = classify(genres, title, rules)
    traits = [g for g in genres if g in trait_whitelist][:6]  # 許可リストにある属性だけ付ける
    note = "タグ: %s / レビュー %s(%d件) / %s" % (", ".join(genres[:10]), review.get("average") or "-", rcount, why)
    return {
        "review_count": rcount,
        "ending": ending,
        "payload": {
            "cid": cid,
            "title": title,
            "image": (img.get("large") or img.get("list") or img.get("small") or "").replace("http://", "https://"),
            "aff_url": aff,
            "price": to_int(prices.get("price")),
            "list_price": to_int(prices.get("list_price")),
            "sale_end": sale_end_of(it),
            "ending": ending,
            "grim": 0,
            "revenge": False,
            "confidence": conf,
            "traits": traits,
            "auto_note": note,
        },
    }


def post(site, token, items):
    for i in range(0, len(items), 50):
        r = requests.post(
            "%s/wp-json/navi/v1/upsert" % site.rstrip("/"),
            json={"items": items[i:i + 50]},
            headers={"X-Navi-Token": token, "User-Agent": UA},
            timeout=60,
        )
        print(r.status_code, r.text[:300])
        r.raise_for_status()


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="navi_config.json")
    ap.add_argument("--list-floors", action="store_true")
    ap.add_argument("--list-genres", metavar="FLOOR_ID")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--dump-raw", action="store_true", help="APIが返す作品1件の生データを表示して終了(取れる項目の確認用)")
    args = ap.parse_args(argv)

    api = Api(env("DMM_API_ID"), env("DMM_AFFILIATE_ID"))
    if args.list_floors:
        print_floors(api)
        return
    if args.list_genres:
        for gid, name in list_genres(api, args.list_genres):
            print("%s\t%s" % (gid, name))
        return

    with open(args.config, encoding="utf-8") as f:
        cfg = json.load(f)
    rules = cfg.get("ending_keywords", {})
    exclude = cfg.get("exclude_genre_keywords", [])
    min_reviews = int(cfg.get("min_reviews", 0))
    keep_other = bool(cfg.get("import_unclassified", True))
    whitelist = set(cfg.get("trait_whitelist", []))

    found = {}
    for t in cfg["targets"]:
        floor_id = find_floor_id(api, t.get("site", "FANZA"), t["service"], t["floor"])
        kws = t["genre_keywords"]
        chosen = [(gid, n) for gid, n in list_genres(api, floor_id) if any(k in n for k in kws)]
        if not chosen:
            print("警告: %s/%s にキーワード %s に合うジャンルがありません (--list-genres %s で確認)" % (
                t["service"], t["floor"], kws, floor_id))
            continue
        print("対象ジャンル:", chosen)
        names = [n for _, n in chosen]
        for gid, _ in chosen:
            for it in fetch_items(api, t, gid):
                if args.dump_raw:
                    if is_excluded(it, exclude):
                        continue
                    print(json.dumps(it, ensure_ascii=False, indent=2))
                    return
                n = normalize(it, rules, names, exclude, whitelist)
                if not n or n["review_count"] < min_reviews:
                    continue
                if n["ending"] == "other" and not keep_other:
                    continue
                found[n["payload"]["cid"]] = n

    ranked = sorted(found.values(), key=lambda n: n["review_count"], reverse=True)
    ranked = ranked[: int(cfg.get("max_items", 150))]
    items = [n["payload"] for n in ranked]
    cand = sum(1 for n in ranked if n["ending"] != "other")
    print("取得 %d件(結末候補あり %d件)" % (len(items), cand))

    if args.dry_run:
        print(json.dumps(items[:3], ensure_ascii=False, indent=2))
        return
    if items:
        post(env("NAVI_SITE_URL"), env("NAVI_API_TOKEN"), items)


if __name__ == "__main__":
    main()
