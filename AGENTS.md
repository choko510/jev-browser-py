# AGENTS.md — 実装担当AI向けの作業規則

このリポジトリは、Jev Browserの判断ループをPythonで再実装し、Scraplingの抽出機能を統合するための独立ライブラリです。**最終的なaiagent-md統合は後工程**です。現在の状態はREADMEとIMPLEMENTATION_GUIDE.mdで確認してください。

## 着手前に必ず行うこと

1. README.mdとIMPLEMENTATION_GUIDE.mdを全文読み、担当するPRの責務・依存・合格条件を確認する。
2. 上流のJev Browser、Jev Agent Tools、TypeSafe Python SDK、Scraplingの該当APIを現行ソースで検証する。固定コミットの内容と新しいupstreamとの差を確認する。
3. 既存ブランチ/PR/CIを見て重複作業を避ける。未承認の仕様変更や別PRの作業を混ぜない。
4. 判断結果は操作許可ではない、という設計を維持する。無関係な機能・ドキュメント・テストの量産は避ける。

## 実装ルール

- Python >=3.12、async-first。PlaywrightのPage/ContextはBrowserOwnerまたは呼出し元だけが管理する。既存Pageを注入した場合は、勝手にcloseしない。
- Jev推論は型付きChoice/Noulを使用し、ActionSpaceの許可IDを厳格検証する。未知Action、無効な確率、未定義の回答、古いSnapshotは**fail-closed**。
- 「判断（Proposal）」と「実行（SingleAction）」を分離する。実行前にPolicy許可、要素一意性、状態、世代、deadlineを再確認する。外部のHarnessへ統合するときはGateway/Sentinelを迂回しない。
- 入力、クリック、フォーム送信は副作用を起こし得る。購入・予約確定・投稿・削除・対外メッセージ送信などは個別の明示承認がなければ行わない。
- モデル出力を任意JavaScript、Shell、URL、座標、任意Selectorとして無検証実行しない。機密画面・Cookie・パスワード・TokenはLLM入力やTraceへ漏らさない。
- URL、redirect、iframe、subresource、popup、WebSocket等のnetwork/SSRF対策はブラウザ内から迂回できない設計が前提。スタンドアロンで強制できない場合は安全でない公開アクセスを有効化せず、制限を明記する。
- Scraplingの導入初期は、既に取得したHTMLをSelectorに渡すだけとする。自動的な二重Fetchや独立Browser起動を避ける。
- 成功判定は独立Verifierとページの事後観測で行う。Jevのdoneや高確率のgoal_doneだけで成功を宣言しない。
- Cancel、timeout、old epoch、stale target、lost ACK、ブラウザクラッシュでは安全停止し、外部副作用が不明な操作を自動再送しない。
- Python/Rust/TypeScriptのどのソースをコピーする場合も適切なライセンスと著作権表示を保持する。独自実装と上流由来コードを区別する。

## PRの品質基準

- 変更範囲を小さく保ち、**同じPR内でコード・fixture・テスト・ドキュメント更新**を完結させる。
- 最初に正常系だけでなく異常系と失敗時の期待動作を定義する。
- CIで走るテストはAPIキー不要・公開サイト非依存とする。課金・実サイト操作を行うスモークは任意かつ明示的に分離する。
- 該当するpytest、lint、型チェックを実際に実行し、実行できなかった場合はその理由を記す。未実行をPASSと書かない。
- レビューでは変更された実コードを確認し、セキュリティ境界、キャンセル競合、例外時のクリーンアップを確認する。
- PRタイトル・本文・コミットメッセージは原則日本語。実装した内容、確認結果、未実装項目、リスクと手動操作の有無を明示する。
- 同じ目的で不要なPRを複数作らない。指示が「既存PRを修正」ならそのPRのブランチで対応する。勝手にマージしない。

## 最初の担当範囲

最初の実装担当はIMPLEMENTATION_GUIDE.mdの**PR-01（Python基盤・推論契約・モック検証）だけ**に着手する。ブラウザ自動操作・Scrapling統合・aiagent-md統合を実装済みと表記しない。

この文書は作業規範であり、実装成功やセキュリティ検証完了を保証するものではありません。
