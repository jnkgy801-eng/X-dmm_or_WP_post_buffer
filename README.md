# サイト記事 → X 予約投稿（Buffer）

adult-navi19.com の新着記事を、Buffer経由でXに予約投稿します（GitHub Actionsで毎日自動実行）。
参考: X-dmm-videoa-genre-limited（Buffer投稿部分を流用）

## セットアップ
1. 新しいGitHubリポジトリにこの一式を置く（`.github/workflows/site_x_post.yml` の位置を変えない）
2. **Settings → Secrets and variables → Actions → Secrets** に登録
   - `BUFFER_API_KEY` / `BUFFER_CHANNEL_ID`（`python buffer_channel_setup.py` で確認）
3. **Actions → サイト記事をXへ予約投稿 → Run workflow** を `dry_run=true` で実行し、投稿文をジョブサマリーで確認
4. 問題なければ `dry_run=false` で1回実行。以降は毎日 06:30 JST に自動実行

## Variables（任意・Settings → Variables）
| 名前 | 既定 | 説明 |
|---|---|---|
| `DAILY_POST_LIMIT` | 3 | 1日の投稿上限。新規アカウントは2〜3から |
| `POST_WINDOWS` | `[[7,9],[12,13],[19,23]]` | 投稿する時間帯（JST）。枠内でランダム時刻 |
| `RECYCLE_AFTER_DAYS` | 30 | 未投稿が尽きたとき、最終投稿からこの日数以上たった記事を別文面で再投稿（0で無効） |
| `DRY_RUN` | 未設定=本番 | `true` にすると定期実行も確認のみになる（一時停止用） |
| `SITE_URL` | https://adult-navi19.com | |

## 安全フィルタの調整（site_x_poster.py 冒頭）
- `DEFAULT_SKIP_PATTERNS` … 投稿しない作品（未成年を連想する語・非同意性行為を連想する語）
- `DEFAULT_NG_WORDS` … タイトルで「○○」に伏せる語。伏せ字が3個以上の作品は投稿しない
- `SAFE_HASHTAG_MAP` … ハッシュタグに使ってよいタグ（ここに無いタグは使われない）

## 運用上の注意
- Xアカウントの設定で「メディアにセンシティブな内容を含める」をONにする
- 投稿先はアダルト投稿を許可する運用のアカウントにする（Xの規約内で）
- Buffer側のアダルトコンテンツに関する規約は、各自で確認すること
- サイトにも「広告を含みます」等の表記を入れる（投稿の #PR と合わせて）
- 履歴は `data/posted_history.json`（自動コミット）。消すと同じ記事が再投稿される
