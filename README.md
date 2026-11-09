# jev-browser-py

**Python-native browser agent inspired by [Jev Browser](https://github.com/jkudish/jev-browser), with [Scrapling](https://github.com/D4Vinci/Scrapling) extraction.**

> **現在の状態（2026-11-09）:** 設計・実装指南書のみ。Pythonパッケージ、CLI、ブラウザ実行エンジン、テスト、CIは**未実装**です。READMEの計画は実装・検証済みであることを示しません。

## 目的

自然言語の目的からWebページを観測し、TypeSafe/Jevの型付き判断（Choice / Noul）で**次の操作を一つ選び**、許可された操作のみPlaywrightで実行する独立Pythonライブラリを作ります。ScraplingはHTML解析・構造化抽出に利用し、後からHTTPクロールへ拡張します。

- **Python-first:** Python 3.12+、非同期Playwright、TypeSafe公式Python SDK。Node.js版Jev Browserをランタイム依存にしない。Chromium/Playwrightの内部実装までPythonで置き換える趣旨ではありません。
- **Judgment ≠ Execution:** Jevが提案、Policyが許可、Executorが単一操作、Verifierが実際の達成を判定。高いモデル確信度や「done」は承認・成功の証拠ではありません。
- **Standalone first:** まずこのリポジトリ単体で動作・テスト・リリース可能な状態に仕上げ、その後 [aiagent-md](https://github.com/choko510/aiagent-md) のBrowser Worker / Gateway / Sentinelへ接続します。
- **Evidence first:** ブラウザ操作前後の観測、判断理由に必要なメタデータ、消費トークン、エラー、抽出元の証拠を残します。機密情報は保存・モデル送信しません。

## 開発者・実装担当AIの入口

1. **[IMPLEMENTATION_GUIDE.md](IMPLEMENTATION_GUIDE.md)** を全文読む。設計、外部依存、アーキテクチャ、開発順序、テスト、完成条件、最初のPR指示を収録。
2. **[AGENTS.md](AGENTS.md)** を読む。自動実装時の変更範囲、安全性、品質、PR運用。
3. Guideの **PR-01** から着手し、実装コードとテストを小さな縦切りで追加する。全機能の空ファイルを初日に作らない。

### 予定するパッケージ

\`\`\`text
src/jev_browser_py/
  contracts.py     # 公開DTO
  transport.py     # Jev推論インターフェイス
  judgment.py      # Choice/Noulと回答検証
  snapshot.py      # Playwrightでページを観測
  action_space.py  # 型付きの操作候補
  executor.py      # Policyを通した単一操作
  runtime.py       # boundedなObserve→Judge→Actループ
  extraction.py    # Scrapling Selector
  verifier.py      # 独立した結果検証
  cli.py           # 後段で追加
\`\`\`

※ 上記は将来の想定ファイルであり、現時点で存在することを意味しません。

## 参照実装

- [Jev Browser (TypeScript)](https://github.com/jkudish/jev-browser) — 機能比較の基準
- [Jev Agent Tools](https://github.com/jkudish/jev-agent-tools) — トランスポート・回答検証の参考
- [TypeSafe Python SDK](https://github.com/typesafe-ai/typesafe-sdk-python) — Pythonネイティブ推論
- [Jev Ultrafast](https://github.com/browser-use/jev-ultrafast) — Python先行実装（方式が異なるため無条件に流用しない）
- [Scrapling](https://github.com/D4Vinci/Scrapling) — HTML抽出・クロール
- [aiagent-md](https://github.com/choko510/aiagent-md) — 将来の統合先（現時点では仕様中心）

## セキュリティの考え方

許可のないサイト操作、購入・送信・投稿・削除等の不可逆処理、CAPTCHAの無条件回避、秘密情報のモデル送信を行いません。公開Web由来の文章は指示ではなく**非信頼データ**です。

Jev BrowserやScraplingの上流コードを直接流用する場合はライセンス・著作権表示を維持してください。
