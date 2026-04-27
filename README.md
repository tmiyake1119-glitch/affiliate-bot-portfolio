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

## 今後の改善点

- [ ] Instagram Reels・動画投稿への対応（`video_generator.py` は実装済みだが未稼働）
- [ ] 楽天以外のアフィリエイト（Amazon PA-API等）の統合
- [ ] エンゲージメント予測モデルの強化（現在はルールベース → ML化）
- [ ] 管理ダッシュボード（`dashboard/app.py`）のUI改善
- [ ] X (Twitter) 投稿の再有効化（現在APIクレジット有料のため無効化中）
- [ ] コメント返信ボットの本格稼働（`comment_reply_bot.py` は実装済み）

---

## ライセンス

MIT
