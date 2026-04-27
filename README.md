# AffiliateBot

楽天アフィリエイトの商品情報をInstagram・Threads・Blueskyへ自動投稿するPythonボット。  
ConoHa VPS上でcron（1日4回）により稼働しており、3アカウントをマルチ管理する。

---

## 解決する課題

- アフィリエイト投稿は毎日継続することで効果が出るが、手動では継続困難
- 複数SNS・複数アカウントへの同時投稿は工数が倍増する
- どの商品・どの訴求文が反響を得るか、データなしでは改善できない

→ 全工程（商品選定 → 投稿文生成 → 投稿 → 分析 → 改善）を自動化し、  
　データドリブンで継続的に最適化できる仕組みを構築した。

---

## 技術スタック

| カテゴリ | 技術 |
|---|---|
| 言語 | Python 3.11 |
| DB | SQLite（`database.py` で直接管理） |
| SNS API | Instagram Graph API / Threads API / Bluesky AT Protocol |
| 商品情報 | 楽天ウェブサービス（ランキング API） |
| 画像生成 | Pillow（テキスト合成） + Cloudinary（CDN配信） |
| LLM | Claude API（Haiku）・OpenAI API（投稿文生成） |
| 通知 | Discord Webhook / Discord Bot |
| インフラ | ConoHa VPS / cron（1日4回実行） |

---

## アーキテクチャ

```
┌─────────────────────────────────────────────────────────────────┐
│  cron (1日4回: 9/13/17/21 JST)                                  │
│       ↓                                                          │
│  main.py                                                         │
│  ├── Step 1  楽天API → トレンド商品取得 (trend_agent)            │
│  ├── Step 2  商品選定・スコアリング (product_selector)            │
│  │             └── CTRウェイト × 季節ブースト × A/Bグループ割当 │
│  ├── Step 3  商品フォーマット整形 (formatter)                    │
│  ├── Step 4  LLMで投稿文生成 → posts_queue に保存 (post_generator)│
│  │                                                               │
│  ├── Step 6  各SNSへ実投稿（アカウントループ）                   │
│  │   ├── BANリスク評価 (ban_risk_monitor)                        │
│  │   ├── 日次上限チェック (post_counter)                         │
│  │   ├── 画像生成 (image_generator) → Cloudinaryアップロード     │
│  │   ├── Threads投稿 (threads_poster)                            │
│  │   ├── Instagram投稿 (instagram_poster)                        │
│  │   └── Bluesky投稿 (bluesky_poster)                            │
│  │                                                               │
│  ├── Step 7  エンゲージメント分析 (analytics_agent)              │
│  │             └── A/Bテスト評価 → ab_config.json 更新           │
│  ├── Step 8  週次レポート・投稿文自動改善 [月曜]                  │
│  ├── Step 9  週間ダイジェスト投稿 [日曜]                         │
│  └── Step 15 DBバックアップ (db_backup)                          │
│                                                                   │
│  ┌──────────────┐   ┌──────────────┐   ┌──────────────────────┐ │
│  │ SQLite DB    │   │ Discord通知  │   │ learning_agent       │ │
│  │ post_logs    │   │ 投稿完了     │   │ (週次cron 月曜3時)   │ │
│  │ ab_results   │   │ エラー       │   │ knowledge/ファイル   │ │
│  │ follower_h.. │   │ 週次レポート │   │ を自動更新           │ │
│  └──────────────┘   └──────────────┘   └──────────────────────┘ │
└─────────────────────────────────────────────────────────────────┘

マルチアカウント構成:
  accounts.json
  ├── mono_zukan_jp   全ジャンル  A/Bテスト有効(Instagram)
  ├── osakezukan_jp   酒類フィルタ
  └── hobbyzukan_jp   ホビーフィルタ
```

---

## 工夫した点

### A/Bテスト自己強化ループ
投稿文のパターン（訴求軸・CTA・フック）をAグループ・Bグループに分け、  
Instagram Insightsのエンゲージメント率（likes×3 + comments×5 + reach）で評価。  
週次で`auto_adjust_ab_ratio()`が比率を自動調整し、収束していく。

```
投稿 → Insights取得 → エンゲージメント計算 → 勝者グループへ比率シフト
```

### スコアリングによる商品選定（product_selector.py）
楽天ランキング商品に対して複合スコアを算出して選定。  
過去のCTR実績（`learning_data`テーブル）がフィードバックループで反映される。

```
score = rank_score
      × commission_multiplier  # ジャンル別手数料補正
      × ctr_weight             # 過去実績CTRウェイト（アカウント別）
      × seasonal_boost         # 季節性ブースト（春節・バレンタイン等）
      × price_filter           # 価格帯フィルタ（500〜5000円）
      - duplicate_penalty      # 最近投稿済み商品の重複ペナルティ
```

### learning_agent による知識ベース自動更新
週次で過去の投稿データをClaude API（Haiku）で分析し、  
`knowledge/` ディレクトリの以下ファイルを自動更新する。

- `genre_trends.md` : ジャンル別成功傾向  
- `hooks.md` : 勝ちフック・CTAパターン  
- `anti_patterns.md` : 避けるべき負けパターン

次週の`post_generator`はこのknowledgeを参照して投稿文を生成する。

### AutoRecovery（auto_recovery_agent.py）
投稿失敗率・エンゲージメント異常を検知し、  
BAN回避のためにプラットフォームごとの投稿を自動停止・自動再開する仕組み。

- `pause.txt` 作成で全停止（緊急停止）
- `suspended_platforms` によるプラットフォーム別停止  
- `post_interval_multiplier` で投稿間隔を延長

### 価格フィルター（環境変数で即変更可能）
```python
PRICE_FILTER_MIN = int(os.getenv("PRICE_FILTER_MIN", "500"))
PRICE_FILTER_MAX = int(os.getenv("PRICE_FILTER_MAX", "5000"))
```
VPSの`.env`を1行書き換えるだけで再デプロイ不要。

---

## ディレクトリ構成

```
affiliate_bot/
├── main.py                  # エントリーポイント（全ステップを順次実行）
├── requirements.txt
├── accounts.example.json    # マルチアカウント設定サンプル
├── .env.example             # 環境変数サンプル
│
├── agents/                  # 各機能モジュール
│   ├── trend_agent.py       # 楽天API商品取得
│   ├── product_selector.py  # スコアリング・商品選定
│   ├── post_generator.py    # LLMによる投稿文生成
│   ├── instagram_poster.py  # Instagram投稿
│   ├── threads_poster.py    # Threads投稿
│   ├── bluesky_poster.py    # Bluesky投稿
│   ├── analytics_agent.py   # エンゲージメント分析・A/Bテスト
│   ├── ab_tester.py         # A/Bテスト管理・比率自動調整
│   ├── learning_agent.py    # 週次知識ベース更新
│   ├── auto_recovery_agent.py # 自動回復・BAN回避
│   ├── database.py          # SQLite管理
│   └── ...（他40+モジュール）
│
├── dashboard/               # Flask管理画面
│   └── app.py
│
└── docs/                    # 設計ドキュメント
    ├── architecture.md
    ├── current_status.md
    ├── decisions.md
    └── runbooks.md
```

---

## セットアップ

### 前提条件
- Python 3.11+
- VPS or ローカル環境（Linuxを推奨）

### インストール

```bash
git clone https://github.com/yourname/affiliate_bot.git
cd affiliate_bot
pip install -r requirements.txt
```

### 環境設定

```bash
cp .env.example .env
# .env を編集して各APIキーを設定

cp accounts.example.json accounts.json
# accounts.json を編集してアカウント情報を設定
```

### 動作確認（テストモード）

```bash
TEST_MODE=true python main.py
# 実際に投稿せず、生成内容をコンソール表示
```

### 本番実行

```bash
python main.py
```

### cron設定（VPS）

```cron
# 1日4回実行（9/13/17/21 JST = 0/4/8/12 UTC）
0  0 * * * cd /home/affiliate_bot && python3 main.py >> logs/main.log 2>&1
0  4 * * * cd /home/affiliate_bot && python3 main.py >> logs/main.log 2>&1
0  8 * * * cd /home/affiliate_bot && python3 main.py >> logs/main.log 2>&1
0 12 * * * cd /home/affiliate_bot && python3 main.py >> logs/main.log 2>&1

# 週次学習（月曜 3:00 JST = 日曜 18:00 UTC）
0 18 * * 0 cd /home/affiliate_bot && python3 agents/learning_agent.py >> logs/learning_agent.log 2>&1
```

### 緊急停止

```bash
touch pause.txt   # 投稿を一時停止
rm pause.txt      # 投稿を再開
```

---

## Background / 開発背景

**EN**

Affiliate marketing only pays off with daily, consistent posting — but doing it manually across three accounts and three platforms quickly becomes unsustainable. I built this bot to eliminate that operational burden, but the harder problem turned out to be *quality*: posting every day means nothing if the products and copy don't resonate.

The core challenge I set out to solve was the feedback loop. Traditional affiliate automation stops at "post something." I wanted a system that observes what actually works — which genres drive clicks, which hooks stop the scroll — and feeds that signal back into the next cycle. The result is less a scheduler and more a self-improving pipeline: each posting cycle generates data, the data refines the scoring weights, and the copy improves week over week through `learning_agent`.

A secondary motivation was resilience. A bot that posts carelessly risks account bans, token expiry, or silent API failures with no way to recover. `auto_recovery_agent`, `ban_risk_monitor`, and `token_renewal` were built from real incidents — each one a safeguard I added after something broke in production.

**JA**

アフィリエイトは毎日継続することで初めて効果が出るが、3アカウント×3プラットフォームへの手動投稿は持続不可能だと判断し、全工程の自動化に着手した。しかし単に「投稿する」だけでは不十分で、より難しい課題は*品質のフィードバックループ*をどう設計するかだった。

どのジャンルがCTRを押し上げるか、どのフックがエンゲージメントを生むか——そのシグナルを次のサイクルに自動で反映する仕組みとして、スコアリング・A/Bテスト・`learning_agent`を組み合わせた。毎回の実行がデータを生み、そのデータが翌週の投稿文を改善する「自己強化ループ」が設計の中心にある。

もう一つの動機はリジリエンスだ。不用意な投稿はアカウントBANを招き、トークン期限切れは無音の障害につながる。`auto_recovery_agent`・`ban_risk_monitor`・`token_renewal`はすべて本番での実際の障害をきっかけに追加した安全装置である。

---

## Design Decisions / 設計思想

### Why A/B testing? / A/Bテストを導入した理由

**EN**

Intuition about "good copy" is unreliable. Two posts selling the same product can produce radically different engagement depending on framing — scarcity vs. value, question-led vs. statement-led. Rather than guess, I built a controlled experiment into the pipeline itself.

The key design choice was using **Instagram Insights as the feedback signal** (not Threads), because Instagram exposes `like_count`, `comments_count`, and `reach` via API. This allows computing a weighted engagement score (`likes × 3 + comments × 5 + reach`) rather than relying on a single vanity metric. The winning group's share grows gradually via `auto_adjust_ab_ratio()` — a soft convergence rather than a hard cutover — which avoids overreacting to short-term noise.

**JA**

「良いコピー」の直感は当てにならない。同じ商品でも「希少性訴求」と「価値訴求」ではエンゲージメントが大きく変わりうる。推測に頼るのではなく、パイプライン自体に制御された実験機構を組み込んだ。

重要な設計判断は**Threadsではなく Instagram Insights をフィードバック信号に使う**ことだ。Instagram は `like_count`・`comments_count`・`reach` をAPIで取得できるため、単一指標ではなく重み付きスコア（`likes×3 + comments×5 + reach`）で評価できる。勝者グループのシェアは `auto_adjust_ab_ratio()` で緩やかにシフトする設計にした。短期ノイズへの過剰反応を避けるためのソフト収束である。

---

### Why multi-account? / マルチアカウント設計にした理由

**EN**

A single general-interest account faces a fundamental targeting problem: alcohol enthusiasts and hobby collectors have different expectations, and a mixed feed satisfies neither. Three focused accounts (`mono_zukan_jp`, `osakezukan_jp`, `hobbyzukan_jp`) allow each audience to receive only relevant content.

The deeper design challenge was making each account *learn independently*. This is why `product_selector.py` holds separate CTR weight dictionaries per account (`_CTR_WEIGHT_OSAKE`, `_CTR_WEIGHT_HOBBY`, etc.) and `generate_posts()` accepts `account_name` to load account-specific knowledge. Accounts share the same Rakuten API fetch (one call, three filters) to avoid redundant API usage, but diverge at scoring, copy generation, and analytics — three independent optimization trajectories within one codebase.

**JA**

汎用アカウント1つでは根本的なターゲティング問題が生じる。お酒ファンとホビーコレクターは期待が異なり、混在したフィードはどちらも満足させない。3つの特化アカウントに分割することで、各フォロワーに関連コンテンツだけを届けられる。

設計上の本質的な課題は「各アカウントが独立して学習する」仕組みだった。`product_selector.py` がアカウントごとに別々のCTRウェイト辞書（`_CTR_WEIGHT_OSAKE`・`_CTR_WEIGHT_HOBBY`等）を持ち、`generate_posts()` が `account_name` を受け取ってアカウント専用のknowledgeを参照する構造がこれを実現している。楽天APIフェッチは1回共有してAPI呼び出しを節約しつつ、スコアリング・投稿文生成・分析は3本の独立した最適化軌跡として分岐する。

---

### Why price filtering and genre balancing? / 価格フィルター・ジャンル均等化を入れた理由

**EN**

**Price filter (¥500–¥5,000 default):** This range targets impulse-purchase decisions. Below ¥500, affiliate commissions rarely justify API and LLM costs. Above ¥5,000, purchase friction increases and conversion rates drop for cold social media traffic. The range is exposed as environment variables (`PRICE_FILTER_MIN` / `PRICE_FILTER_MAX`) so it can be tuned on the VPS without a redeploy.

The price anomaly classifier (`_classify_price`) adds a layer on top: items at a 30-day minimum or 15%+ below average are `FORCED` to the top of the queue regardless of genre weights, because a genuine deal outperforms any framing. Items priced above 130% of the 30-day average are excluded as likely reseller markup — protecting follower trust.

**Genre balancing (diversity bonus):** Left unchecked, a pure CTR-maximizing scorer would narrow the feed to whichever genre is currently trending, causing follower fatigue and suppressing discovery. The diversity bonus (`+0%` to `+50%` based on recent posting frequency) ensures that genres absent from the last 20 posts receive a scoring boost, keeping the feed varied without manual intervention.

**JA**

**価格フィルター（デフォルト500〜5000円）：** この価格帯は衝動買いの意思決定を狙っている。500円未満はアフィリエイト手数料がAPI・LLMコストに見合わない。5000円超はSNSのコールドトラフィックに対してコンバージョン障壁が高くなる。この範囲を環境変数（`PRICE_FILTER_MIN`/`PRICE_FILTER_MAX`）で制御する設計にしたのは、再デプロイなしでVPS上で即調整できるようにするためだ。

その上位レイヤーとして価格異常分類器（`_classify_price`）を設けた。30日間最安値更新または平均比15%超割引の商品は `FORCED`（強制最優先）として、ジャンルウェイトを無視してキューの先頭に配置する。本物のお得感はどんなコピーより強いからだ。逆に30日平均の130%超の商品は転売品として除外し、フォロワーの信頼を守る。

**ジャンル均等化ボーナス（diversity bonus）：** 純粋なCTR最大化ロジックに任せると、一時的にトレンドのジャンルに投稿が集中し、フォロワーの飽きを引き起こす。直近20件の投稿実績が少ないジャンルにスコアボーナス（最大+50%）を付与することで、手動介入なしにフィードの多様性を維持する設計にした。

---

- [ ] Instagram Reels・動画投稿への対応（`video_generator.py` は実装済みだが未稼働）
- [ ] 楽天以外のアフィリエイト（Amazon PA-API等）の統合
- [ ] エンゲージメント予測モデルの強化（現在はルールベース → ML化）
- [ ] 管理ダッシュボード（`dashboard/app.py`）のUI改善
- [ ] X (Twitter) 投稿の再有効化（現在APIクレジット有料のため無効化中）
- [ ] コメント返信ボットの本格稼働（`comment_reply_bot.py` は実装済み）

---

## ライセンス

MIT
