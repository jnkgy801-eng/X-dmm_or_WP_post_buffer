"""
自サイト（WordPress）の記事 → X（Buffer経由）予約投稿スクリプト

流れ:
  1. WordPress REST API（失敗時はRSS）から最新記事を取得
  2. 履歴（data/posted_history.json）と安全フィルタで投稿候補を絞り込む
  3. 1日の上限・時間帯・ランダムなゆらぎを考慮して予約時刻を決める
  4. Bufferに「本文（リンクなし）→ リプライ（リンク）」のスレッドとして予約作成

シャドウバン（表示抑制）リスクを下げるための設計:
  - 本文にURLを入れず、リプライに置く（外部リンク付き投稿の表示抑制対策）
  - 文面テンプレートを複数用意し、直近と同じものを避ける（定型スパム判定対策）
  - 予約時刻を時間帯内でランダム化し、1日の投稿数に上限を設ける
  - ハッシュタグは最大2個。露骨な語はタグ・タイトルから除外／伏せ字化
  - 未成年・非同意性行為を連想させる作品は投稿対象から除外
  ※Xの表示抑制ロジックは非公開です。上記は「リスクを下げる」工夫であり、回避を保証するものではありません。
"""

from __future__ import annotations

import datetime as dt
import email.utils
import html
import json
import os
import random
import re
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Optional
from urllib.parse import urlparse

import requests

# ================================================================
# 定数
# ================================================================

BUFFER_API_ENDPOINT = "https://api.buffer.com"
USER_AGENT = "Mozilla/5.0 (compatible; SiteXPoster/1.0)"
MIN_LEAD_MIN = 10  # 予約は「今から最低この分数後」以降にする
MAX_CONSECUTIVE_BUFFER_ERRORS = 3  # 連続失敗でそれ以上の投稿を中止（レート制限・認証エラー対策）

# 投稿対象から除外する正規表現（タイトル＋タグに対して判定）。編集して調整してください。
#  - 未成年を連想させる語 / 非同意の性行為を連想させる語（Xのポリシー・アカウント凍結リスク）
DEFAULT_SKIP_PATTERNS: list[str] = [
    r"ロリ|幼女|小学生|中学生|児童|未成年|ショタ|メスガキ|メス[ガ●○◯〇*＊]キ",
    r"(?<![A-Za-z])(JS|JC)(?![A-Za-z])",
    r"レイプ|レ[●○◯*＊x×〇]プ|強姦|輪姦|睡眠姦|痴漢|無理やり|無理矢理",
]

# 本文に出さず「○○」に置き換える語（検索・表示抑制を避けるため）
DEFAULT_NG_WORDS: list[str] = [
    "ちんぽ", "チンポ", "ちんこ", "チンコ", "ち◯こ", "ち○こ", "おまんこ", "オマンコ", "マンコ",
    "セックス", "SEX", "S〇X", "S○X", "中出し", "オナニー", "パイズリ", "フェラ", "射精", "精子",
    "種付け", "孕ませ", "膣", "肉便器", "小便器", "放尿", "交尾", "ごっくん", "ぶっかけ",
    "性器", "デカチン", "巨根",
]

# サイトのタグ名 → 投稿用ハッシュタグ（ここに無いタグは使わない＝露骨なタグを自動除外）
SAFE_HASHTAG_MAP: dict[str, str] = {
    "巨乳": "巨乳", "おっぱい": "おっぱい", "制服": "制服", "人妻・主婦": "人妻", "熟女": "熟女",
    "ギャル": "ギャル", "ハーレム": "ハーレム", "純愛": "純愛", "ダウナー": "ダウナー",
    "ラブラブ・あまあま": "あまあま", "寝取り・寝取られ・NTR": "NTR", "KU100": "KU100", "ASMR": "ASMR",
}

# 冒頭の一言テンプレート。{tag} はタグ名（タグが取れない記事では tag 不要のものだけ使う）
HOOKS_PLAIN: list[str] = [
    "新着作品を追加しました📥",
    "本日の新着から1本👀",
    "今日のピックアップ✨",
    "新作チェック📝",
    "サイトを更新しました🆕",
]
HOOKS_WITH_TAG: list[str] = [
    "{tag}が好きな人はチェック👀",
    "{tag}系の新着です📥",
    "{tag}好きに向けた新作を紹介しています✨",
]
HOOKS_AUDIO: list[str] = [  # KU100（バイノーラル録音）タグの記事向け
    "KU100収録の新作です🎧 イヤホン推奨",
    "ヘッドホンで聴きたい新作が入りました🎧",
]
REPLY_LEADS: list[str] = [
    "詳細・サンプルはこちら👇",
    "作品ページはこちらから👇",
    "価格や詳細はこちらでチェック👇",
]

# ================================================================
# 設定
# ================================================================


def _env_str(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_bool(name: str, default: bool) -> bool:
    raw = _env_str(name)
    return default if raw == "" else raw.lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    raw = _env_str(name)
    if raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        print(f"⚠️  {name}={raw!r} は整数ではないため既定値 {default} を使います。")
        return default


def _parse_windows(raw: str) -> list[tuple[int, int]]:
    """'[[7,9],[12,13],[19,23]]' → [(7, 9), (12, 13), (19, 23)]。不正なら既定値。"""
    default = [(7, 9), (12, 13), (19, 23)]
    if not raw:
        return default
    try:
        windows = [(int(a), int(b)) for a, b in json.loads(raw)]
        if all(0 <= a < b <= 24 for a, b in windows) and windows:
            return windows
    except (ValueError, TypeError):
        pass
    print("⚠️  POST_WINDOWS の形式が不正なため既定の時間帯を使います。")
    return default


@dataclass(frozen=True)
class Config:
    site_url: str
    buffer_api_key: str
    buffer_channel_id: str
    dry_run: bool
    daily_limit: int
    windows: list[tuple[int, int]]
    min_gap_min: int
    tz_offset_hours: float
    recycle_after_days: int
    max_title_chars: int
    max_masks: int
    base_hashtag: str
    ad_tag: str
    url_in_reply: bool
    skip_url_check: bool
    history_path: Path
    posts_fixture: str

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            site_url=_env_str("SITE_URL", "https://adult-navi19.com").rstrip("/"),
            buffer_api_key=_env_str("BUFFER_API_KEY"),
            buffer_channel_id=_env_str("BUFFER_CHANNEL_ID"),
            dry_run=_env_bool("DRY_RUN", True),  # 安全側：明示的に false にしない限り実投稿しない
            daily_limit=max(0, _env_int("DAILY_POST_LIMIT", 3)),
            windows=_parse_windows(_env_str("POST_WINDOWS")),
            min_gap_min=max(1, _env_int("MIN_GAP_MIN", 45)),
            tz_offset_hours=float(_env_str("TZ_OFFSET_HOURS", "9") or 9),
            recycle_after_days=max(0, _env_int("RECYCLE_AFTER_DAYS", 30)),
            max_title_chars=max(8, _env_int("MAX_TITLE_CHARS", 28)),
            max_masks=max(0, _env_int("MAX_TITLE_MASKS", 2)),
            base_hashtag=_env_str("BASE_HASHTAG", "FANZA同人").lstrip("#"),
            ad_tag=_env_str("AD_TAG", "#PR"),
            url_in_reply=_env_bool("URL_IN_REPLY", True),
            skip_url_check=_env_bool("SKIP_URL_CHECK", False),
            history_path=Path(_env_str("HISTORY_FILE", "data/posted_history.json")),
            posts_fixture=_env_str("POSTS_FIXTURE"),
        )


# ================================================================
# 記事の取得（WordPress REST API → 失敗時 RSS）
# ================================================================


@dataclass
class Post:
    key: str  # 重複判定用キー（URLのパス部分。ID変更に強い）
    title: str
    url: str
    tags: list[str]
    published: dt.datetime  # UTC


def _strip_html(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text)).strip()


def _make_key(url: str) -> str:
    return urlparse(url).path or url


def _fetch_tag_names(session: requests.Session, cfg: Config, tag_ids: set[int]) -> dict[int, str]:
    names: dict[int, str] = {}
    ids = sorted(tag_ids)
    for i in range(0, len(ids), 100):
        chunk = ids[i : i + 100]
        resp = session.get(
            f"{cfg.site_url}/wp-json/wp/v2/tags",
            params={"include": ",".join(map(str, chunk)), "per_page": 100, "_fields": "id,name"},
            timeout=30,
        )
        resp.raise_for_status()
        names.update({t["id"]: html.unescape(t["name"]) for t in resp.json()})
    return names


def fetch_posts_rest(session: requests.Session, cfg: Config, limit: int = 60) -> list[Post]:
    resp = session.get(
        f"{cfg.site_url}/wp-json/wp/v2/posts",
        params={"per_page": limit, "orderby": "date", "order": "desc", "_fields": "id,date_gmt,link,title,tags"},
        timeout=30,
    )
    resp.raise_for_status()
    items: list[dict[str, Any]] = resp.json()
    tag_names = _fetch_tag_names(session, cfg, {t for it in items for t in it.get("tags", [])})
    posts: list[Post] = []
    for it in items:
        published = dt.datetime.fromisoformat(it["date_gmt"]).replace(tzinfo=dt.timezone.utc)
        posts.append(
            Post(
                key=_make_key(it["link"]),
                title=_strip_html(it["title"]["rendered"]),
                url=it["link"],
                tags=[tag_names[t] for t in it.get("tags", []) if t in tag_names],
                published=published,
            )
        )
    return posts


def fetch_posts_feed(session: requests.Session, cfg: Config) -> list[Post]:
    resp = session.get(f"{cfg.site_url}/feed/", timeout=30)
    resp.raise_for_status()
    root = ET.fromstring(resp.content)
    posts: list[Post] = []
    for item in root.iter("item"):
        link = (item.findtext("link") or "").strip()
        if not link:
            continue
        pub_raw = item.findtext("pubDate")
        published = email.utils.parsedate_to_datetime(pub_raw).astimezone(dt.timezone.utc) if pub_raw else dt.datetime.now(dt.timezone.utc)
        posts.append(
            Post(
                key=_make_key(link),
                title=_strip_html(item.findtext("title") or ""),
                url=link,
                tags=[(c.text or "").strip() for c in item.findall("category") if c.text],
                published=published,
            )
        )
    return posts


def load_posts(session: requests.Session, cfg: Config) -> list[Post]:
    """記事を新しい順に返す。テスト用に POSTS_FIXTURE（JSONファイル）も読める。"""
    if cfg.posts_fixture:
        raw = json.loads(Path(cfg.posts_fixture).read_text(encoding="utf-8"))
        return [
            Post(key=_make_key(r["url"]), title=r["title"], url=r["url"], tags=r.get("tags", []),
                 published=dt.datetime.fromisoformat(r["published"]))
            for r in raw
        ]
    try:
        posts = fetch_posts_rest(session, cfg)
        print(f"📥 REST APIから {len(posts)} 件取得")
    except (requests.RequestException, ValueError, KeyError) as e:
        print(f"⚠️  REST API取得に失敗（{e}）。RSSにフォールバックします。")
        posts = fetch_posts_feed(session, cfg)
        print(f"📥 RSSから {len(posts)} 件取得")
    return sorted(posts, key=lambda p: p.published, reverse=True)


# ================================================================
# 履歴（重複防止・1日上限・直近テンプレート）
# ================================================================


class History:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.posted: dict[str, dict[str, Any]] = {}  # key -> {"due": iso(UTC), "at": iso(UTC)}
        self.recent_hooks: list[str] = []

    @classmethod
    def load(cls, path: Path) -> "History":
        h = cls(path)
        if not path.exists():
            return h
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                h.posted = data.get("posted", {})
                h.recent_hooks = data.get("recent_hooks", [])
        except (OSError, ValueError) as e:
            # 履歴が壊れたまま続行すると重複投稿になるため、ここで止める
            raise SystemExit(f"❌ 履歴ファイルを読めません（{path}）: {e}")
        return h

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 2, "posted": self.posted, "recent_hooks": self.recent_hooks[-5:]}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)  # 書き込み途中での破損を防ぐ

    def record(self, post: Post, due_utc: dt.datetime, hook: str) -> None:
        self.posted[post.key] = {
            "due": due_utc.isoformat(),
            "at": dt.datetime.now(dt.timezone.utc).isoformat(),
        }

    def count_due_on(self, day: dt.date, tz: dt.timezone) -> int:
        n = 0
        for rec in self.posted.values():
            try:
                if dt.datetime.fromisoformat(rec["due"]).astimezone(tz).date() == day:
                    n += 1
            except (KeyError, ValueError):
                continue
        return n

    def last_posted(self, key: str) -> Optional[dt.datetime]:
        try:
            return dt.datetime.fromisoformat(self.posted[key]["due"])
        except (KeyError, ValueError):
            return None


# ================================================================
# 安全フィルタ・文面生成
# ================================================================


def is_skipped(post: Post, patterns: list[re.Pattern[str]]) -> bool:
    haystack = post.title + " " + " ".join(post.tags)
    return any(p.search(haystack) for p in patterns)


def shorten_title(title: str, max_chars: int, ng_words: list[str]) -> tuple[str, int]:
    """サブタイトルを落として短縮し、NG語を伏せ字にする。戻り値は (タイトル, 伏せ字にした数)。"""
    head = re.split(r"[〜～【\[（(―—]", title, maxsplit=1)[0].strip()
    base = head if len(head) >= 6 else title.strip()
    masks = 0
    for word in sorted(ng_words, key=len, reverse=True):
        if word in base:
            masks += base.count(word)
            base = base.replace(word, "○○")
    if len(base) > max_chars:
        base = base[: max_chars - 1].rstrip() + "…"
    return base, masks


def build_hashtags(post: Post, cfg: Config, rng: random.Random) -> tuple[str, Optional[str]]:
    """ハッシュタグ行と、冒頭の一言に使うタグ名を返す。ハッシュタグは基本タグ＋最大1個。"""
    safe = [t for t in post.tags if t in SAFE_HASHTAG_MAP]
    chosen = rng.choice(safe) if safe else None
    tags = [f"#{cfg.base_hashtag}"] if cfg.base_hashtag else []
    if chosen:
        tags.append(f"#{SAFE_HASHTAG_MAP[chosen]}")
    return " ".join(tags), (SAFE_HASHTAG_MAP[chosen] if chosen else None)


def choose_hook(post: Post, tag_label: Optional[str], recent: list[str], rng: random.Random) -> str:
    """直近3回と同じ文面を避けて冒頭の一言を選ぶ。"""
    pool = list(HOOKS_PLAIN)
    if tag_label:
        pool += [h.format(tag=tag_label) for h in HOOKS_WITH_TAG]
    if "KU100" in post.tags or "KU100" in post.title:
        pool += HOOKS_AUDIO * 2  # 事実に基づく訴求なので出現率を上げる
    fresh = [h for h in pool if h not in recent[-3:]] or pool
    return rng.choice(fresh)


_X_LOW_WEIGHT = [(0, 4351), (8192, 8205), (8208, 8223), (8242, 8247)]
_URL_RE = re.compile(r"https?://\S+")


def x_text_length(text: str) -> int:
    """Xの重み付き文字数（URLは23固定、半角=1、日本語・絵文字=2）。"""
    urls = _URL_RE.findall(text)
    rest = _URL_RE.sub("", text)
    weight = sum(1 if any(lo <= ord(c) <= hi for lo, hi in _X_LOW_WEIGHT) else 2 for c in rest)
    return weight + 23 * len(urls)


def build_texts(post: Post, cfg: Config, ng_words: list[str], history: History, rng: random.Random) -> Optional[tuple[str, Optional[str], str]]:
    """(本文, リプライ本文 or None, 使用した一言) を返す。NG語が多すぎる記事は None（投稿しない）。"""
    title, masks = shorten_title(post.title, cfg.max_title_chars, ng_words)
    if masks > cfg.max_masks:
        return None
    tag_line, tag_label = build_hashtags(post, cfg, rng)
    hook = choose_hook(post, tag_label, history.recent_hooks, rng)
    lead = rng.choice(REPLY_LEADS)

    def assemble(t: str) -> tuple[str, Optional[str]]:
        if cfg.url_in_reply:
            main = f"{hook}\n\n📽 {t}\n\n{tag_line}".strip()
            return main, f"{lead}\n{post.url}\n{cfg.ad_tag}"
        return f"{hook}\n\n📽 {t}\n\n{post.url}\n\n{tag_line} {cfg.ad_tag}".strip(), None

    for limit in (len(title), 20, 14, 10):  # 長すぎるときはタイトルをさらに縮める
        shown = title if limit >= len(title) else title[: limit - 1] + "…"
        main, reply = assemble(shown)
        if x_text_length(main) <= 280 and (reply is None or x_text_length(reply) <= 280):
            return main, reply, hook
    return None


def url_is_alive(session: requests.Session, url: str) -> bool:
    """投稿前にリンク先が200で、noindexヘッダが付いていないか確認する。"""
    try:
        with session.get(url, timeout=15, stream=True) as r:
            return r.status_code == 200 and "noindex" not in r.headers.get("X-Robots-Tag", "").lower()
    except requests.RequestException:
        return False


# ================================================================
# 予約時刻の決定
# ================================================================


def plan_schedule(day: dt.date, count: int, windows: list[tuple[int, int]], tz: dt.timezone,
                  now_utc: dt.datetime, min_gap_min: int, rng: random.Random) -> list[dt.datetime]:
    """指定日の各時間帯から「1時間につき1つ」ランダムな時刻を候補にし、count個を選ぶ（UTC返却）。
    候補は毎回ランダムなので、毎日同じ時刻に投稿するBotらしさを避けられる。"""
    candidates: list[dt.datetime] = []
    earliest = now_utc + dt.timedelta(minutes=MIN_LEAD_MIN)
    for start_h, end_h in windows:
        for hour in range(start_h, min(end_h, 24)):
            local = dt.datetime.combine(day, dt.time(hour, rng.randint(0, 59), rng.randint(0, 59)), tzinfo=tz)
            utc = local.astimezone(dt.timezone.utc)
            if utc >= earliest:
                candidates.append(utc)
    if count <= 0 or not candidates:
        return []
    count = min(count, len(candidates))
    gap = dt.timedelta(minutes=min_gap_min)
    for _ in range(50):
        pick = sorted(rng.sample(candidates, count))
        if all(b - a >= gap for a, b in zip(pick, pick[1:])):
            return pick
    return sorted(candidates)[:count]  # 間隔条件を満たす組み合わせが無い場合の保険


# ================================================================
# Buffer
# ================================================================

_CREATE_POST_MUTATION = """
mutation CreatePost($input: CreatePostInput!) {
  createPost(input: $input) {
    ... on PostActionSuccess { post { id status dueAt } }
    ... on MutationError { message }
  }
}
"""


def buffer_create_post(session: requests.Session, cfg: Config, main_text: str, reply_text: Optional[str],
                       due_utc: dt.datetime) -> tuple[bool, str]:
    """成功時 (True, 予約日時)、失敗時 (False, エラー文言)。"""
    thread: list[dict[str, str]] = [{"text": main_text}]
    if reply_text:
        thread.append({"text": reply_text})
    input_obj = {
        "text": main_text,
        "channelId": cfg.buffer_channel_id,
        "schedulingType": "automatic",
        "mode": "customScheduled",
        "dueAt": due_utc.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "metadata": {"twitter": {"thread": thread}},
    }
    try:
        resp = session.post(
            BUFFER_API_ENDPOINT,
            json={"query": _CREATE_POST_MUTATION, "variables": {"input": input_obj}},
            headers={"Authorization": f"Bearer {cfg.buffer_api_key}", "Content-Type": "application/json"},
            timeout=30,
        )
        data = resp.json()
    except (requests.RequestException, ValueError) as e:
        return False, f"通信エラー: {e}"
    if data.get("errors"):
        return False, "; ".join(e.get("message", str(e)) for e in data["errors"])
    result = (data.get("data") or {}).get("createPost") or {}
    if result.get("message"):
        return False, result["message"]
    if result.get("post"):
        return True, result["post"].get("dueAt", "")
    return False, "不明な応答（postが含まれません）"


# ================================================================
# メイン
# ================================================================


def iter_candidates(posts: list[Post], history: History, cfg: Config, now_utc: dt.datetime) -> Iterator[Post]:
    """未投稿（新しい順）→ 再投稿可能（最後の投稿が古い順）の順に候補を返す。"""
    yield from (p for p in posts if p.key not in history.posted)
    if cfg.recycle_after_days <= 0:
        return
    threshold = now_utc - dt.timedelta(days=cfg.recycle_after_days)
    recyclable = [p for p in posts if (last := history.last_posted(p.key)) and last <= threshold]
    yield from sorted(recyclable, key=lambda p: history.last_posted(p.key) or now_utc)


def main() -> int:
    cfg = Config.from_env()
    if not cfg.dry_run and not (cfg.buffer_api_key and cfg.buffer_channel_id):
        print("❌ BUFFER_API_KEY / BUFFER_CHANNEL_ID が未設定です（確認だけなら DRY_RUN=true）。")
        return 1

    tz = dt.timezone(dt.timedelta(hours=cfg.tz_offset_hours))
    now_utc = dt.datetime.now(dt.timezone.utc)
    rng = random.Random()
    history = History.load(cfg.history_path)
    skip_patterns = [re.compile(p) for p in DEFAULT_SKIP_PATTERNS]

    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT

    try:
        posts = load_posts(session, cfg)
    except (requests.RequestException, ET.ParseError, OSError, ValueError, KeyError) as e:
        print(f"❌ 記事の取得に失敗しました: {e}")
        return 2

    # 今日の残り枠 → 無ければ翌日の枠に予約する（1日の上限は予約日基準で数える）
    slots: list[dt.datetime] = []
    for offset in (0, 1):
        day = now_utc.astimezone(tz).date() + dt.timedelta(days=offset)
        remaining = cfg.daily_limit - history.count_due_on(day, tz)
        slots = plan_schedule(day, remaining, cfg.windows, tz, now_utc, cfg.min_gap_min, rng)
        if slots:
            break
    if not slots:
        print(f"ℹ️  予約できる枠がありません（1日の上限 {cfg.daily_limit} 件に達しているか、時間帯が過ぎています）。")
        return 0

    mode = "DRY RUN（投稿しません）" if cfg.dry_run else "本番（Bufferに予約作成）"
    print(f"🐦 {mode} / 予約枠 {len(slots)} 件 / 記事 {len(posts)} 件")

    errors = consecutive_errors = done = 0
    summary: list[str] = []
    candidates = iter_candidates(posts, history, cfg, now_utc)
    for due in slots:
        built = None
        for post in candidates:  # 条件に合う記事が見つかるまで次の候補へ
            if is_skipped(post, skip_patterns):
                print(f"  ⏭️  除外（安全フィルタ）: {post.title[:30]}")
                continue
            if not cfg.skip_url_check and not url_is_alive(session, post.url):
                print(f"  ⏭️  除外（リンク確認NG）: {post.url}")
                continue
            texts = build_texts(post, cfg, DEFAULT_NG_WORDS, history, rng)
            if texts is None:
                print(f"  ⏭️  除外（NG語が多い/文字数超過）: {post.title[:30]}")
                continue
            built = (post, *texts)
            break
        if built is None:
            print("ℹ️  投稿できる記事が尽きました。")
            break

        post, main_text, reply_text, hook = built
        due_jst = due.astimezone(tz).strftime("%m/%d %H:%M")
        block = f"[{due_jst} JST]\n{main_text}\n" + (f"  └ リプライ:\n{reply_text}\n" if reply_text else "")
        print("\n" + block)
        summary.append(block)

        history.recent_hooks.append(hook)  # 同じ実行内でも一言が被らないようにする（保存は成功時のみ）
        if cfg.dry_run:
            continue
        ok, info = buffer_create_post(session, cfg, main_text, reply_text, due)
        if ok:
            history.record(post, due, hook)
            history.save()  # 1件ごとに保存し、途中で失敗しても二重投稿を防ぐ
            done += 1
            consecutive_errors = 0
            print(f"  ✅ 予約しました（Buffer: {info}）")
            time.sleep(1.5)
        else:
            errors += 1
            consecutive_errors += 1
            print(f"  ❌ Buffer予約に失敗: {info}")
            if consecutive_errors >= MAX_CONSECUTIVE_BUFFER_ERRORS:
                print("🛑 連続失敗のため中止します（認証・レート制限・キュー停止を確認してください）。")
                break

    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary and summary:
        with open(step_summary, "a", encoding="utf-8") as f:
            f.write(f"### X投稿プレビュー（{mode}）\n\n```\n" + "\n".join(summary) + "\n```\n")

    print(f"\n完了: 予約 {done} 件 / 失敗 {errors} 件")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
