# AutoPulse 実装仕様書 v1.1

作成日: 2026-10-18  更新: AutoPulse命名確定  対象: Windows / PowerShell / Codex CLI / Claude Code CLI / ChatGPT Web

## 1. 製品名称・目的・原則
正式名称: **AutoPulse**（オートパルス）。意味: AIが開発活動を検知し、進捗を自律的に更新・同期する。GitHub Repository: `autopulse`、CLI実行名: `autopulse`、Pythonパッケージ: `auto_pulse`、ローカルデータディレクトリ: `%LOCALAPPDATA%/AutoPulse/`。新規のコード・資料・テンプレートでは旧仮称 `autopulse` を使用しない。既存の旧仮称由来の設定があれば、意図的に移行策を検討し、黙って削除・上書きしない。
複数の個人開発リポジトリを、GitHub Projects（統合ボード）とGitHub Issues（タスクの正本）で管理する。AIが作業中に進捗を記録し、ユーザーに毎回「進捗更新」を指示させない。コードの完了と検証完了を分ける。設計判断をAIが勝手に確定しない。

## 2. 構成
- GitHub User Project v2 を原則1つ: `Personal Development`（名称は変更可能）
- 各開発案件は個別GitHub Repository。Issueがタスクの唯一の正本。
- Projectsは複数リポジトリのIssueを一覧にする。PRをタスクとして数えず、Issueとの関連付けだけを行う。
- 共有ユーティリティ `autopulse`（Python 3.12+, stdlib中心、`gh` CLIをサブプロセスとして利用）。実装は独立リポジトリに保管。
- AI連携は薄いアダプター（AGENTS.md、CLAUDE.md、対応するCLIの利用可能なHooks/通知）。特定バージョンの未確認Hooksを必須条件にしない。
- ローカルイベントスプール: `%LOCALAPPDATA%/AutoPulse/queue/`。git管理しない。機密情報と会話本文を含めない。
- GitHub Actionsの定期照合は第2段階。初期版ではローカルCLIだけで動作すること。

## 3. Projectsフィールド
標準Title/Repository/Assigneesに加え、以下を可能な範囲で用意。
| Field | Type | Values/Meaning |
| --- | --- | --- |
| Status | Single select | Backlog, Ready, In Progress, Blocked, Review, Done |
| Priority | Single select | P0, P1, P2, P3（初期値P2） |
| Category | Single select | Spec, Hardware, PCB, Firmware, Software, Test, Docs, Other |
| Last Activity | Date | 最後に検証済みの作業イベントがあった日（セッション開始のみでは更新しない） |
| Next Action | Text | 次回に取りかかれる具体的な一文 |
| AI Agent | Single select | Codex, Claude, ChatGPT, Mixed |
| Effort | Number | 手動設定・推定可能でも自動上書き禁止 |

まず標準フィールドを確認し、既存フィールドと衝突する場合は自動削除・再作成せず確認を求める。フィールド名を設定ファイルにマップし、APIのIDを動的取得する。

## 4. Issueのテンプレート
必須: Summary、Acceptance Criteria（チェック可能な完了条件）、Verification（テスト方法）、Next Action、Blockers（なければNone）。
任意: Related decisions、Related PR/commit、Notes。
ラベル: `ai-managed`、`type:bug`、`type:feature`、`type:research`等。ラベルを勝手に大増殖させない。
Issue本文の人間編集欄を破壊せず、同期ツールの更新範囲は `<!-- autopulse:start -->` / `<!-- autopulse:end -->` の間だけに限定する。追記履歴はIssueコメントに記載する。

## 5. 状態遷移（厳格）
- Backlog -> Ready: 作業開始できる程度に要件・完了条件が明確。
- Ready -> In Progress: 特定Issueについて実際に作業開始。
- In Progress -> Blocked: 外部依存や判断待ちで前進不可。Blocker理由を記録。
- In Progress -> Review: 実装済みだが確認・テスト・レビュー待ち。
- Review -> Done: Acceptance Criteriaを満たし、要求された検証に成功し、証拠（テスト/PR/コミット等）を記録。
- Done -> In Progress/Blocked: 回帰や未完了が判明した際の再開。理由を記録。
- 検証不要の調査Issueでは「成果物を記録し、ACを満たす」を検証として扱える。
- Doneを自動設定する際は`--verified`または構造化された検証証跡を要求。証跡が不十分ならReviewに留める。人間承認指定のACがある場合、AIはDoneにしない。
- 同じリポジトリで複数IssueがIn Progressでもよい。セッション単位で対象Issueを明示する。

## 6. AutoPulse CLI（`autopulse`）のインターフェース
サブコマンドの案（実装詳細はClaude Codeが整理可）:
- `autopulse doctor` : Python、gh認証、project権限、フィールド、repoアクセスをread-onlyで検査。
- `autopulse init --project <number> --owner <login>` : 設定ファイルを作成、dry-runで差分提示後に明示承認を得て初期化。
- `autopulse status --repo owner/repo` : Issueと現状態、前回活動、次の作業を表示。
- `autopulse start --repo owner/repo --issue 123 --agent codex|claude` : IssueをIn Progress、作業開始の記録。
- `autopulse note --repo owner/repo --issue 123 --summary "..." --next "..."` : 作業要約と次の一手。Issueコメント/管理欄を更新。
- `autopulse block --repo owner/repo --issue 123 --reason "..."` : Blockedへ。
- `autopulse review --repo owner/repo --issue 123 --evidence "..."` : Reviewへ。
- `autopulse complete --repo owner/repo --issue 123 --evidence "..." --verified` : 条件確認後にDoneへ。
- `autopulse sync` : イベントキューの再送（指数バックオフ）。
- `autopulse reconcile --dry-run` : Issue/PR/Projectの矛盾を報告。修正は明示的な `--apply` の場合のみ。
- `autopulse onboard --repo owner/repo --dry-run` : AI共通ファイル等の導入差分を表示。既存ファイルを破壊しない。
すべての変更系コマンドに `--dry-run`、`--json` を用意。終了コードを明確にする。ログは秘密情報をマスク。

## 7. 更新・重複排除・並行実行
- 各イベントにUUIDを付与し、永続キューに記録してから送信する（write-ahead）。一度反映したイベントを再送しても重複コメントを残さない（コメント内にイベントIDの不可視マーカーを記録する等）。
- Issue更新前に最新のIssue/Projectsデータを再取得。人間編集した内容を上書きしない。
- 同一Issueの同時更新はローカル排他だけでは不十分。読み直し・衝突検出・限定リトライ・最終的なIssueコメントでの競合報告を実装。
- `Next Action`等の自由文は最終更新を無条件に優先しない。競合時は現行値を保持し、競合を通知する。
- GitHubのAPIレート制限（403/429）、ネットワーク障害、CLI未認証は、エラーを保持して再試行可能とする。
- 対象プロジェクト外のIssueを書き換えない。repo allowlistを設ける。

## 8. GitHub Projects APIと認証
- Windows開発PC: `gh auth login`、Projects権限（必要に応じ `gh auth refresh -s project`）を検査。
- Projectの項目追加: `gh project item-add`。項目更新: `gh project item-edit`。名前ベースのインターフェースを使い、必要に応じGraphQL IDをキャッシュ。
- GitHub Actionsの`GITHUB_TOKEN`単独ではProjects操作に十分でない。ユーザープロジェクトはスコープを絞ったPAT等をSecretsに保存するか、権限を設定したGitHub Appを利用。認証情報をリポジトリに保存・コミットしない。
- 初期版でActionsの書き込み資格情報を用意しない場合は、定期照合をread-onlyのレポート生成に限定するか後回しにする。
- 仕様の自動作成、Issueの大量登録、既存Projectの変更はdry-run→確認後に実施。人間の許可なく公開可視性・権限を変えない。

## 9. AI連携
- Codex: AGENTS.mdから `.ai/workflow.md` を参照。CLIの実環境・バージョンを調べ、利用可能な通知/Hookの範囲でsyncを補助。セッション開始・終了の完全検知を前提とせず、AIルール内から `start/note/review/complete` を呼ぶ。
- Claude Code: CLAUDE.mdから同じ共通ルールを参照。SessionStart/Stop/SessionEnd等は実際のCLIバージョンの仕様を確認して設定。Hookによる終了時同期はベストエフォート。無限ループに注意。
- ChatGPT Web: 原則read-onlyな進捗照会と仕様相談。GitHub上の書き込み手段の有無を確認するまでは、自動書き込みできると主張しない。決定内容はClaude/Codex用のhandoff.mdにまとめる。
- ChatGPT会話本文をGitHubに自動転記しない。明示的に決定した仕様のみドキュメント化する。

## 10. フォルダ構成
共通リポジトリ:
```
autopulse/
  src/auto_pulse/
  tests/
  templates/AGENTS.snippet.md
  templates/CLAUDE.snippet.md
  templates/workflow.md
  templates/issue-template.yml
  examples/config.toml
  README.md
```
個別リポジトリ:
```
AGENTS.md
CLAUDE.md
.ai/workflow.md
.github/ISSUE_TEMPLATE/ai-task.yml
docs/DECISIONS.md
docs/STATUS.md
```
既存の指示ファイルはバックアップ・追記方式。既存の内容を削除しない。

## 11. 最初の受入テスト
1. `doctor`で必要権限を診断し、未設定なら具体的な修正手順を示す。
2. 作業開始により対象IssueがProjectsへ重複なしで追加され、In Progressになる。
3. Codexで始めたIssueをClaude側から読み、Next Actionを理解できる。
4. Claudeが記録したnoteがIssueに残り、再起動後も参照できる。
5. 未検証のIssueをDoneにしようとすると拒否またはReviewになる。
6. 回線断時イベントが消えず、`sync`で復旧し、重複コメントが発生しない。
7. 並行更新が競合したとき、人間のコメント/本文を失わない。
8. ツールが実行できない場合も開発自体を妨げない。
9. 新しいリポジトリを`onboard`で導入できる。
10. `reconcile --dry-run`が更新漏れを検出できる。

## 12. 非対象（初期版）
- 専用Webダッシュボード・ローカル常駐サーバー・データベース
- AIによる仕様や優先順位の無断確定
- ChatGPT Web会話内容の自動スクレイピング
- 進捗率をAIの印象で生成すること
- 他ユーザー/他組織のプロジェクトを無断変更すること

## 13. 進め方
Phase 0: 利用者PCの`gh`、Codex/Claudeバージョン、実際のGitHub Project/Repo、既存指示ファイルを検査（read-only）。
Phase 1: Pythonの同期ツール+ユニットテスト+模擬APIを実装。
Phase 2: テスト用IssueでのE2E確認。実データの破壊的操作前には確認する。
Phase 3: 1つの開発repoへAIルールを導入。Hookは存在する機能だけ追加。
Phase 4: 他リポジトリへ展開し、必要な権限が揃えばGitHub Actions照合を追加。
