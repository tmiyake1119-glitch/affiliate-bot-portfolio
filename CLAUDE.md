## Core Rules

実装前に必ずPlan Modeで設計を提示し、承認を得るまで実装を開始してはならない。

## 制約
- 最小変更主義（既存コード再利用・局所修正・小さな差分）
- 不要なリファクタ・将来用の抽象化・大規模再設計は禁止
- 新規ライブラリ追加は承認必須

## トークン節約
- 回答は簡潔に・差分中心
- 不要な前置き・過剰説明・繰り返し禁止
- 1タスクずつ・小さく積み上げる

## NG行動
- 勝手に実装開始
- 聞かれていない拡張提案
- 過剰設計・話を広げる

## Quick Start
- 起動: デスクトップの start-llm.bat
- テスト: python test_post_dry.py

## 主要環境変数
- ANTHROPIC_API_KEY: Claude API
- USE_LLM_CORE: llm_core有効化（デフォルトfalse）
- GOOGLE_API_KEY: Gemini API

## Context
まず docs/current_status.md を確認すること。
必要時のみ他docsを参照すること（architecture.md / runbooks.md / decisions.md）。
