# Python Jev Browser — 実装指南書 / Implementation Guide

> **状態:** Draft / 実装レビュー中（2026-11-09 JST）。コード・テスト・ベンチマークは追加済みですが、セキュリティ境界とリリースGateの検証が未完了です。実装・テスト・未実施項目は `docs/status.md` を参照してください。未検証のインターフェイスは引き続き提案扱いです。
>
> **開発ゴール:** Jev Browserの「型付き判断で一操作を選ぶ」強みをPythonネイティブで再実装し、PlaywrightとScraplingを組み合わせた、単体で使える安全なブラウザシステムを完成させる。その後に `choko510/aiagent-md` のBrowser Workerへ統合可能な設計にする。

## 0. 最初に読むもの・調査基準

**正本はこのファイル。** 実装担当AIは `README.md`、`AGENTS.md` と併せて全文を読み、担当PRの範囲を把握してください。upstreamは以下のコミットを**比較基準**とします（常にmainがこの版とは限らない）。

| 参照先 | 比較基準 | 役割 |
|---|---|---|
| [jkudish/jev-browser](https://github.com/jkudish/jev-browser) | `913d1a1cbc42f9534e3fbfefae915d1fc941d22d` / v0.8.12 | 元のTypeScript版の挙動 |
| [jkudish/jev-agent-tools](https://github.com/jkudish/jev-agent-tools) | 導入時にHEADとライセンス再確認 | JevのProvider、応答検証の参考 |
| [typesafe-ai/typesafe-sdk-python](https://github.com/typesafe-ai/typesafe-sdk-python) | 調査時のpyproject v0.7.5 | 公式Python推論SDK（Choice/Noul） |
| [D4Vinci/Scrapling](https://github.com/D4Vinci/Scrapling) | `30cadb56a407fd631391441709ab792ebca52b08` / v0.5.1 | HTML抽出、後日クローリング |
| [browser-use/jev-ultrafast](https://github.com/browser-use/jev-ultrafast) | 導入時にHEADとライセンス再確認 | Pythonの先行設計。挙動やBrowser所有権は別 |
| [choko510/aiagent-md](https://github.com/choko510/aiagent-md) | `986b0c9f9a2bd4538d97c2fc6ca82439cf6849ae` | **将来**の統合先（現時点は仕様中心） |

上流で必読の実ファイル：Jev Browserの [`src/navigate.ts`](https://github.com/jkudish/jev-browser/blob/main/src/navigate.ts)、[`src/lib.ts`](https://github.com/jkudish/jev-browser/blob/main/src/lib.ts)、[`src/questions.ts`](https://github.com/jkudish/jev-browser/blob/main/src/questions.ts)、[`src/provider.ts`](https://github.com/jkudish/jev-browser/blob/main/src/provider.ts) および [`test/`](https://github.com/jkudish/jev-browser/tree/main/test)。

**上流をまるごと自動翻訳しない。** Python公開API、ブラウザ所有権、権限判定、データ抽出を切り離し、振る舞いをfixtureで比較してから移植する。上流由来コードをコピーするならMIT/BSD-3-Clause等の著作権表示・ライセンス条件を確認して維持する。

## 1. 完成形と非目標

### v1.0で目指す挙動

1. `run(goal, ...)` に自然言語のタスクを渡す。
2. 現在のページの可視情報と操作可能なDOM/ARIA要素を**上限付き・秘密情報を除外して**観測。
3. TypeSafe/Jevに `action: Choice`、`goal_done: Noul`、`stuck: Noul` を同じ状態で問い合わせる。
4. 提案された操作が現在の候補・ポリシー・期限に一致するか検証。
5. Playwrightで**許可済みの一操作のみ**実行し、直後にページを再観測。
6. 目標の達成は独立Verifierで判定。モデルの `done` 単独では成功にしない。
7. 必要なら同じHTMLをScrapling `Selector` へ渡し、出典付きの構造化情報を返す。
8. タイムアウト、キャンセル、画面変更、Web保護、ログイン要求、モデル障害を失敗種別に分ける。
9. ライブラリAPI・CLIから利用でき、単体でテスト・配布できる。

### v1.0で約束しないこと

- JevモデルそのもののPythonローカル再学習・推論実装
- すべてのサイト、CAPTCHA、MFA、特殊なcanvas UI、画像だけの操作への万能対応
- ブラウザやHTTPを使った認可外のデータ取得・アクセス制限回避
- 購入・送金・投稿・予約確定などをユーザー承認なく自動実行すること
- Python化だけで上流版より速い／安いという未測定の主張
- `aiagent-md` のSentinel/DB/Gateway/VM実装をこのリポジトリ内に複製すること

## 2. 重要なアーキテクチャと権限の境界

```text
Caller / CLI / (future aiagent-md Gateway)
        |
        v
BrowserRuntime [Run budget, cancellation, trace, orchestrates only]
        |
        +-- BrowserOwner / Playwright Page [Context/Tab/lock owner]
        |      |
        |      +-- Snapshotter -> bounded PageSnapshot + ActionSpace
        |
        +-- JevTransport -> TypeSafe Python SDK
        |      +-- Choice(action) / Noul(goal_done) / Noul(stuck)
        |      '-- Typed proposal; never operates the page directly
        |
        +-- OutcomeVerifier [independent, evidence-grounded]
        |
        +-- ActionPolicy [deny by default] -> SingleActionExecutor
        |      '-- Preflight freshness check -> one action -> observation
        |
        +-- ScraplingExtractor -> Selector(existing HTML) -> evidence
        |
        '-- Telemetry / redacted artifacts / terminal status

Later: aiagent-md Tool Gateway / Sentinel / side-effect ledger /
       lease fencing owns all authorization, not this package's model.
```

**不変条件:**
- Jevの推論結果は**Proposal**であって**Permission**ではない。信頼度、選択確率、`goal_done`は副作用の承認を代替しない。
- `BrowserOwner` だけがBrowser/Context/Pageを開閉する。他者が注入したPageは絶対に勝手に閉じない。
- 外部ブラウザアクセスは検証済みPolicyと一操作単位のExecutorを通す。Agentから生のCDP URLやCookieを公開しない。
- 同じPageの同時操作は禁止。スナップショットから実行までの間にDOMが変われば提案を破棄し、新たに観測する。
- 未知の副作用結果 `UNKNOWN`、認可不明、権限競合、機密保護不能の場合は安全停止。明示されない再送をしない。
- v1.0 standaloneのPolicyは将来のHarness本番環境のSentinelと同等ではない。**統合後もGateway/Sentinelが最終権限者**である。

### なぜ最初はPythonなのか

Python SDK・Playwright Python・Scraplingを直接呼べる。Jev推論/回線/Chromium処理が遅延を支配する場面ではRust化だけの改善は限定的。ボトルネック測定後にDOM解析などをRust拡張へ切り出せるよう境界を保つ。

## 3. パッケージ構成の目標

必要になったモジュールだけを各PRで追加する。最初から空のファイル一式を作らない。

```text
jev-browser-py/
  README.md
  AGENTS.md
  IMPLEMENTATION_GUIDE.md
  pyproject.toml                 # PR-01で作成
  uv.lock                        # PR-01で作成
  src/jev_browser_py/
    __init__.py                  # Python公開API
    contracts.py                 # strict DTO / status / errors
    questions.py                 # typed Choice/Noul catalog
    transport.py                 # JevTransport Protocol + TypeSafe adapter
    judgment.py                  # validate replies, proposal only
    action_space.py              # prune/dedupe/cap/ephemeral element IDs
    snapshot.py                  # DOM/ARIA extraction and redaction
    policy.py                    # Action authorization interface
    executor.py                  # Playwright single-action execution
    runtime.py                   # bounded async state machine
    typing.py                    # optional text generator
    verifier.py                  # independent outcome verification
    extraction.py                # Scrapling Selector
    session.py                   # owner, injected Page, lifetime
    telemetry.py                 # redacted usage / trace
    cli.py                       # final stages
  tests/
    fixtures/                    # ローカルHTML/HTTPサーバー
    test_judgment.py
    test_action_space.py
    test_executor.py
    test_runtime.py
    test_extraction.py
    test_security.py
    test_e2e_local.py
  examples/                      # ローカルfixtureの利用例
```

**Python 3.12以上**をプロジェクトの最低バージョンとして検討。公式TypeSafe SDK、Scraplingの要求はより低い場合があるので、実装PRでPythonの互換範囲と依存固定（`uv.lock`）を検証する。標準構成は `playwright`、`typesafe-sdk`、`scrapling`、`pydantic>=2`、開発ツールは `pytest`、`pytest-asyncio`、`ruff`、型チェック。実際のバージョンを調査してから記入する。

## 4. 公開APIと内部契約

以下は**期待契約（提案）**であり、署名やフィールド名は実装時に型・互換性のテストで決定する。

```python
from typing import Protocol

class JevTransport(Protocol):
    async def judge(self, *, state: object, questions: object,
                    deadline: float) -> object: ...

class ActionAuthorizer(Protocol):
    async def authorize(self, *, proposal: object,
                        snapshot: object) -> object: ...

class OutcomeVerifier(Protocol):
    async def verify(self, *, goal: str, before: object,
                     after: object) -> object: ...

# 公開意図:
# async def suggest_next_action(goal: str, snapshot: PageSnapshot,
#                               transport: JevTransport) -> ActionProposal: ...
# async def run(goal: str, *, page: Page | None = None,
#               authorizer: ActionAuthorizer,
#               verifier: OutcomeVerifier, limits: RunLimits) -> RunResult: ...
```

### 最低限の型

- `PageSnapshot`: opaque `snapshot_id`、`generation`、ページURL、title、観測時刻、表示テキストの短い抜粋、frame ref、候補一覧、secret-redaction status。
- `ActionCandidate`: snapshot内だけで有効な `e1` 等のID、操作kind、短い説明、非公開の対象ref/fingerprint、要件（enabled/visible等）。
- `Judgment`: `action`、`goal_done`、`stuck`、確率、使用量、モデル識別子、元Snapshot参照。パース失敗は成功値に変換しない。
- `ActionProposal`: suggestion + snapshot/goal revision + side-effect class（`read`, `ambiguous`, `external_write` 等）。
- `ActionOutcome`: `observed`、`acted`、`stale`、`uncertain`、`blocked`、`needs_human`、`cancelled` 等。DOM変化と送信成功を混同しない。
- `RunResult`: `status`、終了理由、`verified`真偽、step数、利用量、出典・証跡ref、redacted step trace。
- `RunLimits`: `max_steps`、`max_seconds`、`max_jev_calls`、`max_cost`、`max_page_bytes`、`max_actions_per_run`。すべて有限かつ強制。
- `ErrorCode`: `INVALID_JUDGMENT`、`STALE_TARGET`、`POLICY_DENIED`、`TIMEOUT`、`CANCELLED`、`CHALLENGE`、`UNKNOWN_EFFECT`、`NEEDS_HUMAN` 等。ライブラリ例外とユーザー向け状態を混同しない。

**IDの所有権:** Standaloneの一時IDはHarnessのcanonical `taskId/runId/tenantId/leaseEpoch/goalRevision` と同一と仮定しない。後日のAdapterが明示的に翻訳する。共有スキーマをこのプロジェクトが勝手に確定させない。

## 5. Jevモデルとの接続

TypeSafe公式SDKに `AsyncTypeSafeClient`、`Choice`、`Noul` がある。3質問を**同一state・1リクエスト**で問い合わせる方式を基本にする（native selectの選択肢決定では追加問い合わせ可）。

```python
from typesafe_sdk import AsyncTypeSafeClient, Choice, Noul

async def ask_step(state: object, criteria: dict[str, str]):
    async with AsyncTypeSafeClient() as client:
        result = await client.system_one(
            state=state,
            questions={
                "action": Choice(
                    instructions="Which single action best advances the current goal?",
                    criteria=criteria,
                ),
                "goal_done": Noul(
                    instructions="Has the actual goal already been achieved?"
                ),
                "stuck": Noul(
                    instructions="Are recent actions failing to make progress?"
                ),
            },
        )
    return result  # 実行前に別validatorで厳格検証する
```

このコードはSDK利用法の**参考断片**。モデル選択・providerの実験条件・usageと費用の計測、deadline、secret redaction、429の扱いは実装側で補うこと。APIキーをREADMEに埋めない。CIは鍵なしのMock Transportで完結させる。

### 厳格な応答検証（Fail-closed）

1. `action/goal_done/stuck` の**要求した回答IDが過不足なく存在**するか。
2. `action` が現在の候補criterion IDに含まれ、未知IDを使用していないか。`Choice` の確率分布もcriterion集合と一致するか。
3. 確率の全値がfiniteで0〜1、総和・選択された候補の順位が適切か。許容誤差はJev Agent Toolsの公開コードに合わせて明文化し、テストする。
4. `Noul`値がfiniteで0〜1。usage tokenが非負・有限、model IDが空でないか。SDKで検証済みでも追加の境界チェックを行う。
5. 未知のレスポンスタイプ・不正JSON・provider障害・期限切れは**実行0**として終了する。
6. モデル判断は必ず元のSnapshot/goal revisionと結び、古い結果を新しいDOMへ適用しない。

Jev元実装の停止閾値（例：`goal_done > 0.85`）は互換性試験の比較材料。停止**提案**であって達成の客観証拠ではない。

### ActionSpace

- 1回の判断に提示する操作は**最大240要素程度**（上流がChoice上限を考慮して採用する値）。クリック、検索、入力、選択、送信の別、`back`、`done` を区別。
- 表示名、ARIA role/label、href、type、select optionなどから説明を作る。重複・ノイズ・不可視・無効・password/file inputを適切に処理。
- `search`と`fill`を区別し、フォームsubmitが暗黙に起きないよう実行契約を定める。
- 長文や大量のHTMLをそのままLLMへ投げない。可視文脈・必要な候補だけとし、token/byteを上限化。
- `eN` はそのSnapshot限りの使い捨てID。ページのリレンダリング後に再利用しない。
- selectオプションが大量の場合は候補上限・pagination/再質問戦略を決める。
- `scroll`、`wait`、iframe、Shadow DOM、popup等の追加操作はv1.0で必要性と実行安全性を評価して段階導入する。

### テキスト生成

Jev自体は「どの入力欄か」を選ぶ。**何を入力するか**は別のTypedTextGeneratorで生成または明示値から取得する。LLMが生成した文字列の長さ・型・目的・秘密漏洩・許可対象を検証し、フォーム送信を勝手に連鎖させない。API利用不能時に架空の個人情報などを推測入力しない。

## 6. Observe → Judge → Actの実行順

```python
async def run_loop(goal, context):  # 設計疑似コード。未実装
    while context.can_continue():
        context.assert_live_lease()
        before = await context.snapshotter.observe_and_redact()
        proposal = await context.judge(goal=goal, snapshot=before)

        # actionはまだ実行されていない
        context.assert_live_lease()
        if proposal.is_done or proposal.goal_done_high:
            if await context.verifier.verify(goal=goal, before=before):
                return context.success_verified()
            return context.needs_replan_or_unverified()

        if proposal.stuck:
            return context.stuck_or_replan_within_budget()

        context.assert_deadline_and_budget()
        await context.policy.authorize_or_deny(proposal, before)
        async with context.tab_action_lock():
            context.assert_live_lease()
            await context.executor.assert_current_target(
                proposal=proposal, snapshot=before
            )
            outcome = await context.executor.execute_one(proposal)
            after = await context.snapshotter.observe_and_redact()
        await context.record_redacted_step(before, proposal, outcome, after)

        if outcome.is_unknown_effect:
            return context.stop_unknown_without_retry()

    return context.budget_or_deadline_exceeded()
```

実装では、`CancellationToken`に相当するasync cancellation、ブラウザ操作開始前後の競合・期限、ネットワーク待ち、Context破棄を正しく処理する。これを既存Pageのlifecycleを壊さずテストする。

### 実行前の再照合

- `session/tab/epoch/goalRevision` が現在の所有者と一致すること（Standaloneでは専有ロックと世代、Harness統合時にcanonical IDs）。
- URL、frame、候補のfingerprint、DOM/ARIA上の対象がSnapshotと整合し、locatorの一致数が1、visible/enabled等の必要条件を満たすこと。
- ページ遷移やpopupで別originの操作にならないこと。Policyに従い再審査すること。
- 遅延応答やDOM更新で不明な場合は、決して古い`eN`のままクリックしない。

**操作後:** `click`のAPI成功をタスク達成とみなさない。URL、画面、DOM、必要に応じたHTTP/ネットワーク情報から結果を観測し、`observed / verified / uncertain / failed`等へ分類する。外部送信のACK喪失時には重複実行を防ぐ。

## 7. Scraplingとの段階的統合

### 最初の方法：同一HTMLを解析（推奨）

```python
from scrapling.parser import Selector

def parse_existing_html(html: str, url: str):
    tree = Selector(content=html, url=url)
    return {
        "title": tree.css("title::text").get(),
        "links": tree.css("a::attr(href)").getall(),
    }
```

このコードの狙いは、Playwrightで取得した`page.content()`などのHTMLを**再フェッチせず**解析すること。実際のスクレイプ対象セレクターはfixtureで確認する。認証Cookieや機密情報を含むHTMLを無条件でArtifactへ保存しない。

- **先に実装:** CSS/XPath、テキスト・リンク・metadata抽出、エラー分類、結果にURL・取得日時・選択条件・原本ハッシュ（アクセス制限付き）を付与。
- **後で実装:** Scrapling Fetcher/Spider、動的ページ取得、adaptive selectors、差分クロール。レート制限、robots、利用規約、エグレス、認証Scope、キャッシュの検証が必要。
- Scraplingの新しいBrowser Sessionを既存Playwright Pageと同一とみなさない。既存Pageの直接共有やCDP接続でのタブ同一性を仮定しない。
- Scraping用の「変更に追随するselector」を、操作用の「未検証のclick target」へそのまま使わない。

## 8. セキュリティ・プライバシー・障害復旧

**MUST（合格前の必須条件）**

1. **権限:** 未認可の外部作用は実行しない。購入、予約、投稿、削除、送金、対外送信は人間の個別承認が必要。曖昧な操作は拒否する。
2. **ネットワーク:** Standaloneでも許可Origin/URL、redirect、DNS/実IP、subresource、iframe、popup、download、WebSocket、Proxyなどを含む外向き通信を制御。Chromiumはページリクエストを自動で発行するため、PythonのURL文字列検査**だけでは十分ではない**。実証済みの隔離/egress制御ができない環境で、任意サイトへの安全なネットワークアクセスを保証しない。
3. **秘密:** APIキー/パスワード/Cookie/storageState/認証ヘッダー/個人情報をJev入力、trace、画面配信、screenshot、エラーへ漏らさない。Redaction失敗はfail-closed。ログにHTTP bodyを無制限に書かない。
4. **入力の信頼:** Webページ内の指示やPrompt injectionを高優先度の指示として扱わない。JSや任意コマンドを生成して実行するToolをJev判断から提供しない。
5. **取消:** Cancel/steer/期限切れ/lease喪失後は次の操作を開始しない。失敗した副作用の自動再送は禁止。無理な回復で別サイトへ無承認遷移しない。
6. **分離:** 別Run・別ユーザー・別TenantのContext/Cookie/Cache/Artifactを混同しない。ブラウザ/セッションの開閉を所有権で制御する。
7. **サイト保護:** CAPTCHA/MFA/ログイン壁・アクセス拒否は`needs_human`/`blocked`等を返し、突破成功を捏造しない。
8. **費用:** モデル呼出し数・token/費用、ブラウザ時間・ステップ、ページ数、HTML bytes、メモリ使用量を有限に制限する。
9. **証跡:** before/after・推論・許可・検証を関連付ける。本文・秘密そのものではなく、権限で保護された参照と安全な要約を基本とする。

## 9. 実装順序 — 小さく実際に動くPRへ

**依存順に進める。** 無意味にPRを細分化しない。各PRは必要な実装コード・テスト・必要最小限の文書変更を含める。レビュー済み・実行済み・未実施を区別し、マージ可否を日本語で報告する。

| PR | 作るもの | 依存 | 「完成」の証拠 |
|---|---|---|---|
| **PR-01** | Python基盤、型付き契約、TypeSafe adapter、MockTransport、検証器 | なし | APIキー不要の異常系までpytest PASS |
| **PR-02** | PlaywrightのSnapshot/ActionSpace/Secret Redaction | 01 | ローカルHTML fixtureで可視要素・候補が一致 |
| **PR-03** | Policy・単一操作Executor・実行前freshness | 02 | stale/ambiguous/無許可実行0 |
| **PR-04** | bounded Run Loop、停止/Stuck、Verifier、Typing | 01〜03 | 複数ページのローカルタスクを完遂 |
| **PR-05** | 障害復旧、cancel/race、セキュリティ強化 | 04 | crash/失効/secret/UNKNOWNテストPASS |
| **PR-06** | Scrapling `Selector`抽出とEvidence | 02〜05 | 既存HTMLから正しい値・証跡・追加Fetch0 |
| **PR-07** | Session、SPA/iframe/Shadow DOM/popup の対応範囲確定 | 05〜06 | 適用範囲内E2E成功、未対応は安全停止 |
| **PR-08** | 安定したPython API/CLI、配布、Docker例 | 07 | 初期環境から説明どおり使用可能 |
| **PR-09** | 上流比較、負荷/費用計測、最終監査、v1.0判定 | 01〜08 | P0/P1残件ゼロ、実測記録、リリースGate合格 |

### PR-01：今すぐ着手する内容

**実装対象**
- `pyproject.toml`、`uv.lock`、`src/jev_browser_py/__init__.py`、`contracts.py`、`questions.py`、`transport.py`、`judgment.py` と必要なテストだけ。
- `JevTransport` Protocolと公式TypeSafe SDK adapter（接続情報を外部設定から受け、Browser Loopと切り離す）。
- `Choice(action)`、`Noul(goal_done)`、`Noul(stuck)` の同時質問。
- Strict Validator: Answer ID、Criterion ID、確率、Usage、Model ID、未知回答、タイムアウト。型違反は実行可能Proposalへ変換しない。
- `MockTransport`を使う完全オフラインテストと、Ruff、型チェック、CIをセットアップ。鍵なしCIから有料APIを呼ばない。

**PR-01で作らないもの:** Playwright制御本体、ScraplingのFetch/Spider、CLIの実サイト操作、aiagent-md統合、過剰な空ファイル。

**合格条件:** 正常な固定レスポンスを1件以上処理でき、未知ID・範囲外確率・NaN・欠落回答・不正usage・API失敗・予算超過がすべて安全停止。CIはオフラインで再現できる。

### PR-02〜04：最初の実動作

- PR-02: 1ブラウザ所有者、要素候補の正確な識別、DOM改変によるgeneration失効、同じPageの注入・再利用。
- PR-03: 高いChoice確率でもpolicy拒否で実行0。form-submitなど潜在的な副作用を分類。対象一致数と状態を実行直前に検査。
- PR-04: 観測→推論→一操作→再観測のループ。独立Verifier、stop/stuck、取消、予算上限。ローカルfixtureで「検索→結果一覧→詳細→目標の文字列を抽出」を自律完遂。

ここまでが**MVPの達成条件**。v1.0ではない。

### PR-05〜09：リリース品質

- PR-05: Security/Timeout/Cancel/Browser crash/Unknown effectsの耐障害性。サイトに副作用を送った可能性がある場合、応答が失われても自動再送しない。
- PR-06: `Selector(content=html, url=url)`、CSS/XPath、元データ・抽出条件・時刻の記録。クロール機能は許可・ネットワーク分離が検証されるまで切る。
- PR-07: popup/iframe/ShadowDOM/SPA/複数タブ・Human takeover。サポート外を「成功」と偽らない。
- PR-08: asyncライブラリ + CLI、再利用Page、実用例、配布手順。認証なし公開HTTP APIを無制限で開かない。
- PR-09: Jev BrowserとPython先行例を**同じ条件**で比較し、正答率/誤成功、p50/p95、API費用、メモリ、失敗復旧を測定。実測条件を保存し、バージョンを固定。

## 10. 試験仕様と失敗注入

次のテストは**これから実装し、実行結果で合否判定**するもの。固定HTMLとローカルHTTPサーバー中心で、CIは外部サイト・APIキー・課金に依存させない。

| ID | 検証対象 | 合格条件 |
|---|---|---|
| PYJEV-T01 | 公式SDKのMock契約 | Choice/Noulを同一stateで問う、使用量取得 |
| PYJEV-T02 | invalid response（ID/NaN/分布/usage） | 実行0、エラーが分類される |
| PYJEV-T03 | 重複/不可視/秘密要素 | ActionSpaceから安全に整理される |
| PYJEV-T04 | 既存Pageを注入 | 不必要なBrowser起動0、呼出後もPage open |
| PYJEV-T05 | 同時DOM変更、locator二重一致 | 古い候補でクリック0 |
| PYJEV-T06 | modelのdoneだけ返却 | OutcomeVerifierの確認なしにsuccessにしない |
| PYJEV-T07 | 未承認submit/高confidence送信 | 実際の外部送信0 |
| PYJEV-T08 | 期限切れ・Cancel・stuck | 新しいAction開始0 |
| PYJEV-T09 | API失敗/429/モデル不明 | 無限retryなし、明確な終了状態 |
| PYJEV-T10 | password/Cookie/PIIを含むHTML | LLM入力・trace・エラーへの漏洩0 |
| PYJEV-T11 | Playwright → Scrapling Selector | 同一HTMLから一致抽出、二重Fetch0 |
| PYJEV-T12 | redirect/private IP/subresource/iframe | 制御下環境で未認可の送信0 |
| PYJEV-T13 | browser crash / lost ACK after submit | UNKNOWNへ遷移、自動再送0 |
| PYJEV-T14 | 複数Session/Runの交差 | Cookie/Tab/Evidence越境0 |
| PYJEV-T15 | SPA/iframe/Shadow DOM/popup | 範囲内で正常動作、範囲外は安全停止 |
| PYJEV-T16 | 固定タスクE2E | 決められた画面遷移と独立検証がPASS |
| PYJEV-T17 | P50/P95・token/cost・RSS測定 | 条件と記録が再現できる |

**CI:** Lint + 型チェック + オフライン単体 + ローカルChromium fixture E2E。Chromiumの導入・OS依存・Headless差をCIで検証する。手動のAPI課金付きスモークはシークレット付きの明示的ワークフローでのみ行う。

**測定指標:** success/verified rate、false success、policy violation、stale target、step count、API calls、token/step、実ブラウザstep p50/p95、total elapsed、browser/worker RSS、Scrapling抽出正確度。最適化は実測後。

## 11. v1.0リリース判定

次がすべて真で初めて「単体システム完成」とする。

- [ ] PythonライブラリAPIとCLIから実際のブラウザタスクを起動できる。
- [ ] CIの無課金fixtureでNavigate→Interact→Extractが成功する。
- [ ] Choice/Noulの異常値・未知Actionをfail-closed。
- [ ] Policyを通さないクリック・送信が存在しない。
- [ ] 独立Verifierが `done` の誤成功を防止できる。
- [ ] Secret/ログ/画面/永続化の重大漏洩がない。
- [ ] Cancel/Timeout/Crash/unknown side effects/二重実行の安全試験を通過。
- [ ] Scraplingの二重Fetchなし抽出ができ、証跡が残る。
- [ ] 複雑Webサイトへの対応範囲と制限をREADMEで説明できる。
- [ ] 依存固定、ライセンス、再現可能なベンチマーク、CIと配布手順が存在。
- [ ] 未解決P0/P1・既知の認可逸脱が0。実測していない主張がない。

## 12. aiagent-mdへの統合はv1.0の後

本プロジェクトは完成後に[aiagent-md](https://github.com/choko510/aiagent-md)へ**Adapterとして**接続する。該当の現行正本：

- [jev-adapter.md](https://github.com/choko510/aiagent-md/blob/main/docs/04-web-browser/jev-adapter.md) — `judgment-only`
- [browser-action-engine.md](https://github.com/choko510/aiagent-md/blob/main/docs/04-web-browser/browser-action-engine.md) — 承認済み一操作
- [browser-session-manager.md](https://github.com/choko510/aiagent-md/blob/main/docs/04-web-browser/browser-session-manager.md) — Context/Page/Tab owner
- [scrapling-sidecar.md](https://github.com/choko510/aiagent-md/blob/main/docs/04-web-browser/scrapling-sidecar.md) — 抽出Workerの信頼境界
- [tool-execution-gateway.md](https://github.com/choko510/aiagent-md/blob/main/docs/03-tools/tool-execution-gateway.md) — 予算・許可・副作用
- [sentinel-authority.md](https://github.com/choko510/aiagent-md/blob/main/docs/09-security/sentinel-authority.md) — 外部操作/ネットワークの最終許可

移行時はHarnessが発行するTask/Run/Lease/Tabを正本とし、Pythonライブラリは**モデル判断・Page観測・許可済み単一操作・抽出**を提供する。独立ライブラリ内の任意URLアクセスやPolicy callbackを、Harness本番の権限境界として信用しない。HarnessのTrusted Browser Worker隔離、Egress、Credential Broker、side-effect Ledger、Human takeover、Browser Live Observationに適合させる。共有ID/DB Schema/イベントを重複定義しない。

## 13. 最初の実装担当AIへのコピペ指示（PR-01専用）

> `choko510/jev-browser-py` の最新`main`、`README.md`、`AGENTS.md`、`IMPLEMENTATION_GUIDE.md` を全文読んでください。次にJev Browserの`src/questions.ts` / `src/lib.ts` / `src/provider.ts`、Jev Agent Toolsの回答検証、TypeSafe公式Python SDKの公開API・型・ライセンスを実際に確認してください。
>
> **今回はPR-01のみ実装してください。** Python 3.12+、uv/pyproject/lock、Pydantic型、JevTransport Protocol、TypeSafe公式SDK adapter、Choice(action)+Noul(goal_done)+Noul(stuck)の同時判断、厳格な応答検証、APIキー不要のMockTransportテスト、Ruff・型チェック・CIを完成させてください。
>
> 型が曖昧な回答、未知Action、欠落回答、確率不正、期限切れはfail-closed。Webアクセスやブラウザ実行機能をPR-01で作らず、Scraplingやaiagent-mdを統合したと偽らないでください。正常・異常fixtureを用意し、テストを実際に実行してください。未実行ならその理由を明記してください。
>
> 変更に必要な最小ファイルだけを作成し、PRタイトル・説明は日本語にしてください。上流のライセンスと著作権表示を守り、実装内容、実行コマンド、テスト結果、未実装範囲、レビュー観点を記載してください。重複PRや不要な文書の増殖を避けてください。マージは勝手にしないでください。

---

### 設計・実装・検証ステータスの区別

このガイドは2026-11-09時点に確認した公開ソース・既存設計・提案を区別しています。**仕様を書いたことはテストしたことではありません。** PRで実際のコード、テスト結果、境界条件を確認してから段階的にステータスを更新してください。
