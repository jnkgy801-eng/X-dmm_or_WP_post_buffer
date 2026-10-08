# サイト記事 → X 自動予約投稿（Buffer + GitHub Actions）

自サイト（WordPress）の新着記事を、Buffer経由でXに自動で予約投稿するツールです。
目的は **サイトへの集客** で、投稿先はアフィリエイトURLではなく **サイトの記事URL** です。
参考: `X-dmm-videoa-genre-limited`（Buffer投稿部分を流用し、データ元をDMM APIから自サイトに変更）

---

## 1. 全体の流れ

```
GitHub Actions（毎日 06:30 JST）
  │
  ├─ 1. 記事取得     WordPress REST API（失敗時はRSS）から最新60件
  ├─ 2. 枠の決定     その日の残り投稿数 × 時間帯内のランダム時刻
  ├─ 3. 候補の選別   未投稿の新しい順 → 安全フィルタ → リンク生存確認
  ├─ 4. 文面生成     本文（リンクなし）＋ リプライ（リンク・#PR）
  ├─ 5. 予約作成     Buffer GraphQL API（スレッド2件）
  └─ 6. 履歴保存     data/posted_history.json をコミット（重複防止）
```

---

## 2. 仕様

### 2-1. 投稿の形

```
【本文】
本日の新着から1本👀

📽 作品タイトル（短縮・伏せ字処理済み）

#FANZA同人 #巨乳

  └ 【リプライ】
詳細・サンプルはこちら👇
https://（記事URL）
#PR
```

- 1作品につき、本文＋リプライの2ツイートのスレッドです。
- 画像・動画は添付しません。リンクカード（OGP）に任せます。

### 2-2. シャドウバン（表示抑制）対策

Xの表示抑制ロジックは非公開です。以下は「リスクを下げる工夫」であり、回避の保証ではありません。

| 対策 | 内容 |
|---|---|
| リンクの置き方 | 本文にURLを入れず、リプライに置く（`URL_IN_REPLY=false` で本文に戻せる） |
| 文面のゆらぎ | 冒頭の一言と誘導文を複数用意。直近3回と同じ一言は避ける |
| 時刻のゆらぎ | 時間帯（既定: 7-9時 / 12-13時 / 19-23時 JST）の中で毎回ランダムな時刻（分・秒まで）に予約 |
| 投稿量の制限 | 1日の上限（既定3件）。予約日基準で数えるため、再実行しても超えない |
| 投稿間隔 | 最低45分（`MIN_GAP_MIN`） |
| ハッシュタグ | 最大2個（基本タグ＋記事タグ1個）。露骨な語のタグは許可リスト外のため使われない |
| タイトル加工 | サブタイトルを落として短縮。露骨な語は「○○」に伏せる。伏せ字が3個以上なら投稿しない |
| 対象作品の除外 | 未成年を連想する語・非同意の性行為を連想する語を含む作品は投稿しない |
| リンク確認 | 投稿前にリンク先が200で、noindexでないことを確認 |
| 体験談の不使用 | 「一気見した」等の捏造体験談は使わず、事実（新着・タグ・KU100収録）のみ |

### 2-3. 候補の選び方

1. 未投稿の記事を新しい順に
2. 未投稿が尽きたら、最終投稿から `RECYCLE_AFTER_DAYS`（既定30日）以上たった記事を別文面で再投稿（0で無効）
3. 安全フィルタ・リンク確認・文字数チェックを通らない記事は飛ばして次の候補へ

### 2-4. 履歴（`data/posted_history.json`）

- Bufferへの予約に **成功した記事だけ** を、1件ごとに記録します。
- 記事のURLパスをキーにするため、記事IDが変わっても重複しません。
- 直近の一言も保存し、次回の実行でも被らないようにします。
- ファイルを削除すると、同じ記事が再投稿されます。

### 2-5. エラー時の挙動

| 状況 | 挙動 |
|---|---|
| REST APIが取得できない | RSS（`/feed/`）にフォールバック |
| 記事の取得に両方失敗 | 終了コード2で停止 |
| Buffer予約が3回連続で失敗 | それ以上の投稿を中止し、終了コード1（Actionsが失敗表示になる） |
| 履歴ファイルが壊れている | 重複投稿を避けるため停止 |

---

## 3. セットアップ手順

### 3-1. Bufferの準備

1. [Buffer](https://buffer.com/) に登録し、投稿先のXアカウントを「チャンネル」として接続
2. https://publish.buffer.com/settings/api で **Personal API Key** を発行

### 3-2. チャンネルIDの確認（VS Code / Windows PowerShell）

`buffer_channel_setup.py` があるフォルダをVS Codeで開き、統合ターミナルで実行します。

```powershell
python -m pip install requests
$env:BUFFER_API_KEY = "発行したキー"
python buffer_channel_setup.py
```

- キーは必ず半角の `"` で囲みます（囲まないと「用語として認識されません」になる）。
- 出力の `[twitter]` の行の下にある `channelId = ...` が `BUFFER_CHANNEL_ID` です。
- macOS / Linux / Git Bash では `export BUFFER_API_KEY=キー` を使います。
- コマンドプロンプトでは `set BUFFER_API_KEY=キー`（引用符なし）を使います。

### 3-3. GitHubの設定

1. 新しいリポジトリにこの一式を置く（`.github/workflows/site_x_post.yml` の位置は変えない）
2. **Settings → Secrets and variables → Actions → Secrets** に登録

| Secret | 値 |
|---|---|
| `BUFFER_API_KEY` | 3-1で発行したキー |
| `BUFFER_CHANNEL_ID` | 3-2で確認した `channelId` |

### 3-4. 初回実行

1. **Actions → 「サイト記事をXへ予約投稿（Buffer）」→ Run workflow**
2. `dry_run=true`（既定）で実行し、ジョブサマリーで投稿文を確認
3. 問題なければ `dry_run=false` で1回実行（Bufferのキューに予約が入るのを確認）
4. 以降は毎日 06:30 JST に自動実行

---

## 4. 設定（Variables・任意）

**Settings → Secrets and variables → Actions → Variables** に登録します。

| 名前 | 既定 | 説明 |
|---|---|---|
| `DAILY_POST_LIMIT` | 3 | 1日の投稿上限。新規アカウントは2〜3から |
| `POST_WINDOWS` | `[[7,9],[12,13],[19,23]]` | 投稿する時間帯（JST・24時間表記） |
| `RECYCLE_AFTER_DAYS` | 30 | 再投稿までの日数（0で無効） |
| `DRY_RUN` | 未設定（本番） | `true` で定期実行も確認のみになる（一時停止用） |
| `SITE_URL` | `https://adult-navi19.com` | 対象サイト |

その他、`site_x_poster.py` が読む環境変数: `MIN_GAP_MIN`（45）/ `MAX_TITLE_CHARS`（28）/ `MAX_TITLE_MASKS`（2）/ `BASE_HASHTAG`（FANZA同人）/ `AD_TAG`（#PR）/ `URL_IN_REPLY`（true）/ `TZ_OFFSET_HOURS`（9）

### 安全フィルタの調整（`site_x_poster.py` 冒頭）

- `DEFAULT_SKIP_PATTERNS` … 投稿しない作品の正規表現（近親ものなど除外したい題材はここに追加）
- `DEFAULT_NG_WORDS` … タイトルで「○○」に伏せる語
- `SAFE_HASHTAG_MAP` … ハッシュタグに使ってよいサイトのタグ名 → ハッシュタグ
- `HOOKS_*` / `REPLY_LEADS` … 文面テンプレート（増やすほど定型感が薄れる）

---

## 5. ファイル構成

```
.
├── site_x_poster.py                    # 本体
├── buffer_channel_setup.py             # チャンネルID確認用
├── requirements.txt
├── README.md
├── data/
│   └── posted_history.json             # 投稿履歴（自動生成・自動コミット）
└── .github/workflows/
    └── site_x_post.yml                 # 毎日の自動実行
```

### ローカルでの確認（投稿しない）

```powershell
$env:DRY_RUN = "true"
python site_x_poster.py
```

---

## 6. トラブルシュート

| 症状 | 原因と対処 |
|---|---|
| `Channel not found` | `BUFFER_CHANNEL_ID` が存在しない。`[twitter]` の `channelId` を取り直し、Secretsを上書き。ID確認に使ったキーとSecretsのキーが同じかも確認 |
| `export` が認識されない | PowerShellでは `$env:BUFFER_API_KEY = "キー"` を使う |
| `'XYpD...' は用語として認識されません` | キーを `"` で囲んでいない |
| `python` / `pip` が認識されない | `py` または `python -m pip` を試す。未インストールならPythonを入れ、「Add python.exe to PATH」にチェック |
| REST APIで403 | セキュリティプラグイン等がブロックしている可能性。RSSに自動フォールバックするが、タグ情報は減る |
| `予約できる枠がありません` | 当日の上限到達、または時間帯が過ぎている（翌日の枠に自動で回る） |
| 投稿が出ない | Buffer側でキューが一時停止していないか、チャンネルの接続が切れていないか確認 |

---

## 7. 注意事項

- **APIキーは絶対に公開しない**。スクリーンショットやチャットに写さない。写った場合は発行し直す（https://publish.buffer.com/settings/api）。
- Xアカウントの設定で「メディアにセンシティブな内容を含める」をONにする。
- Xのアダルトコンテンツに関する規約の範囲で運用する。
- **Bufferのアダルトコンテンツ関連の規約は、各自で確認する**（違反時はBuffer側のアカウント停止リスクがある）。
- サイトにも「広告を含みます」等の表記を入れる（投稿の `#PR` と合わせて）。
- Xの表示抑制は非公開の仕組みで、本ツールの対策は効果を保証しない。インプレッションが極端に下がったら、投稿数を減らして様子を見る。
- 新規アカウントはいきなり投稿量を増やさず、`DAILY_POST_LIMIT` 2〜3から始める。
- 投稿以外（同ジャンルのアカウントとの交流、プロフィール整備）は自動化せず、手動で行う。

---

## 8. 今後の改善案

- GA4で記事ごと・流入元ごとのクリックを計測し、売れるジャンルの投稿を増やす
- 特集・ランキング記事（ジャンル別まとめ）を専用テンプレートで投稿
- Xのインプレッション推移を見て、時間帯（`POST_WINDOWS`）を調整
