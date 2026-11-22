# NTRおすすめ速報（adult-navi21.site）運用ツール

FANZAのNTRタグ作品を集計してサイト（WordPress）に反映し、サイトの検索流入ページをXで告知するための一式です。
目的は **NTRが好きな人に「自分の好み」から作品を探してもらい、FANZAでの購入につなげること**です。

```
GitHub Actions（毎日 5:00 JST）  navi-sync.yml
  └ navi_ntr.py ── FANZA API（NTRジャンル・新着順）
        ├ 作品  → POST /wp-json/navi/v1/upsert          → 新作 / 高評価 / セール / シチュエーション別ページ
        └ 女優  → POST /wp-json/navi/v1/actress-upsert  → 女優ランキング / 女優ページ

GitHub Actions（毎日 6:30 JST）  site_x_post.yml
  └ site_x_poster.py ── GET /wp-json/navi/v1/feed（検索に載せてよいページだけ）→ Buffer → X
```

---

## 1. サイトの構成（プラグイン `navi-happyend-hub` v0.5）

| ページ | URL | 内容 | 購入導線 |
|---|---|---|---|
| ランキング（トップ） | `/` | 女優ランキング3種 | 各行に「代表作を見る」ボタン |
| 新作 | `/new/` | 直近45日に発売されたNTR作品 | 価格・サンプル・購入ボタン |
| 高評価 | `/best/` | レビュー平均×件数のランキング | 同上 |
| セール | `/sale/` | セール中・値下がり（割引率順、過去最安バッジ） | 同上 |
| シチュエーション | `/situation/` と `/situation/<key>/` | 人妻・不倫 / 彼女・恋人 / 留守・不在中 / 目の前・すぐ隣 / 映像・記録もの / 職場・上司 / 隣人・ご近所 / 堕ちていく過程 / VR | 同上 |
| 女優 | `/actress/aXXXX/` | 数字から作った説明文、得意なシチュエーション、まず観るならこの1本 | 作品ごとに購入ボタン |

- `/new/` `/best/` `/sale/` `/situation/` は、プラグインを更新して管理画面を開くと**自動で作成**されます。
- `/situation/<key>/` は、そのシチュエーションの作品が**5本以上たまった時点で自動作成**されます。
- シチュエーション分類は、FANZAのタグとタイトルの語句一致による**目安**です（`navi_config.json` の `situations`）。作品内容を断定しません。

### index（検索に載せる）方針

薄いページを増やさないため、次のページだけを `index` にし、それ以外は `noindex, follow` です。

| ページ | index条件 |
|---|---|
| 集計ページ（新作・高評価・シチュエーション別） | 作品が5本以上 |
| セール | 3本以上 |
| 女優ページ | 単体作品が10本以上かつ掲載作品が6本以上、または「確認済み＋短評30文字以上」 |
| 作品の個別ページ | 「確認済み＋短評30文字以上」のみ |

女優ページの基準は `wp-config.php` の `NAVI_ACTRESS_INDEX_MIN_SOLO`（0なら手動確認分のみ）で変えられます。

### クリック計測

プラグインがアフィリエイトリンクのクリックを `affiliate_click` として送ります（GA4の`gtag`またはGTMの`dataLayer`が入っている場合のみ。無ければ何もしません）。
`slot` に設置場所が入るので、**どの導線が売れているか**が分かります。

`rank-solo / rank-wife / rank-aggr`（トップのランキング）／`actress-pick`（女優ページの1本）／`actress-works`／`new`／`best`／`sale`／`deals`／`hub-<key>`／`work`

---

## 2. セットアップ

### 2-1. WordPress

1. `plugins.zip` の `navi-happyend-hub` を `wp-content/plugins/` に**上書きアップロード**（フォルダ名は変えない。有効化したままでOK）
2. `wp-config.php` に追記
   ```php
   define('NAVI_API_TOKEN', '24文字以上のランダム文字列');   // 必須（GitHubの NAVI_API_TOKEN と同じ値）
   ```
3. 管理画面を開く（固定ページ `/new/` 等が自動作成される）
4. メニューに「新作」「高評価」「セール」「シチュエーション」を追加
5. トップページの本文に、必要なら次を追加（ランキングの下など）
   ```
   [navi_new days="45" limit="6"]
   [navi_situations]
   ```
6. 旧プラグイン `actress-profile` は **停止 → 削除**（機能は navi-happyend-hub に統合済み。旧プラグインで入力した体重などは自動で引き継がれます）

> `contact-form-7` と `conoha-wing-plugin` / `cache-clear-conoha` はサーバー側で管理されているものです。上書き・削除しないでください。

### 2-2. GitHub

**Settings → Secrets and variables → Actions → Secrets**

| Secret | 用途 |
|---|---|
| `DMM_API_ID` / `DMM_AFFILIATE_ID` | FANZA API |
| `NAVI_API_TOKEN` | サイトへの登録用（wp-configと同じ値） |
| `BUFFER_API_KEY` / `BUFFER_CHANNEL_ID` | X予約投稿（`buffer_channel_setup.py` でチャンネルIDを確認） |

Variables の `SITE_URL` は使わなくなりました（古い値が残っていたら削除してください）。

### 2-3. 初回実行

1. Actions → **navi-sync** → Run workflow → `dry_run=true`（件数とシチュエーション別の内訳をログで確認）
2. `dry_run=false` で実行（作品・女優がサイトに入る）
3. サイトで `/new/` `/best/` `/sale/` を確認
4. Actions → **サイト記事をXへ予約投稿（Buffer）** を `dry_run=true` で実行し、投稿文を確認 → 問題なければ本番

---

## 3. 設定

### `navi_config.json`（FANZA側）

| キー | 内容 |
|---|---|
| `pages` / `months` | 取得ページ数（1ページ100件）/ 集計期間（月） |
| `new_days` / `max_works` | 新作とみなす日数 / サイトに送る作品の上限 |
| `min_solo_for_actress` / `max_actresses` | 女優ページの対象にする単体作品数 / 人数の上限 |
| `exclude_keywords` | タグ・タイトルにあれば作品ごと除外（未成年・学校設定・家族設定など） |
| `situations` | シチュエーション → タグ名・タイトル語句。語句を足すと分類が増える |

> サイトの「集計方法」に「学校設定・家族設定は除く」とあるため、`exclude_keywords` に義父・兄嫁・叔母・生徒・保健室などの語を入れています。除外したくない語は外してください。

### X投稿（`site_x_post.yml` / Variables）

| 名前 | 既定 | 説明 |
|---|---|---|
| `DAILY_POST_LIMIT` | 3 | 1日の投稿上限。新規アカウントは2〜3から |
| `POST_WINDOWS` | `[[7,9],[12,13],[19,23]]` | 投稿する時間帯（JST） |
| `RECYCLE_AFTER_DAYS` | 30 | 同じページを再投稿するまでの日数（0で無効） |
| `DRY_RUN` | 未設定（本番） | `true` で定期実行も確認のみ |

投稿の形は「本文（リンクなし）＋リプライ（リンク・`#PR`）」の2ツイートです。本文は事実だけ（例: `NTR作品30本(単体24本)`）で、体験談は作りません。
安全フィルタ・ハッシュタグ・文面は `site_x_poster.py` 冒頭（`DEFAULT_SKIP_PATTERNS` / `SAFE_HASHTAG_MAP` / `HOOKS_*`）で調整します。

---

## 4. ファイル構成

```
.
├── navi_ntr.py                 # FANZA → サイト（作品・女優）
├── navi_config.json            # 取得・除外・シチュエーション分類の設定
├── site_x_poster.py            # サイトのフィード → Buffer → X
├── buffer_channel_setup.py     # BufferのチャンネルID確認用
├── requirements.txt
├── data/posted_history.json    # X投稿履歴（自動更新。消すと再投稿される）
└── .github/workflows/
    ├── navi-sync.yml           # 毎日5:00 JST
    └── site_x_post.yml         # 毎日6:30 JST
```

ローカル確認:
```powershell
$env:DMM_API_ID = "..."; $env:DMM_AFFILIATE_ID = "..."
python navi_ntr.py --dry-run          # 送らずに集計だけ
python navi_ntr.py --list-genres      # ジャンル(タグ)ID一覧
$env:DRY_RUN = "true"; python site_x_poster.py
```

---

## 5. トラブルシュート

| 症状 | 対処 |
|---|---|
| 登録APIが503 | `wp-config.php` に `NAVI_API_TOKEN` が無い（24文字以上） |
| 登録APIが403 | GitHubの `NAVI_API_TOKEN` とwp-configの値が違う |
| `/situation/<key>/` が作られない | そのシチュエーションの作品が5本未満。`navi_config.json` の語句を増やす |
| Xの候補が「0件」 | index条件を満たすページがまだ無い。`/wp-json/navi/v1/feed` をブラウザで開いて確認 |
| `Channel not found` | `BUFFER_CHANNEL_ID` が違う。`buffer_channel_setup.py` で取り直す |
| 女優ページが `noindex` のまま | 単体作品10本未満（`NAVI_ACTRESS_INDEX_MIN_SOLO`）。管理画面で短評を書き「確認済み」にすればindex対象 |

## 6. 注意事項

- **APIキー・トークンは公開しない**。写してしまったら発行し直す。
- ページの「広告を含みます」表記と、投稿の `#PR` は外さない（FANZAアフィリエイト規約）。
- Xのアダルト規約、Bufferの規約は各自で確認する。Xの表示抑制は非公開の仕組みで、本ツールの工夫は効果を保証しない。
- 価格・セールは取得時点の情報。購入前にFANZAの作品ページで確認する旨をページに表示している。
