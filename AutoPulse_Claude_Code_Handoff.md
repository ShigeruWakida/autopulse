# Claude Code 実装指示 — AutoPulse

## 依頼
同封の `AutoPulse_Spec_v1.1.md` を仕様の正本として読み、Windows上で動作する `autopulse` を実装してください。目的は、GitHub ProjectsとGitHub Issuesを唯一の進捗管理基盤とし、Codex CLI・Claude Code CLIの両方が開発作業の進捗を自動記録できることです。

## 名称・命名規則
正式名称は **AutoPulse**。リポジトリ名 `autopulse`、CLIコマンド `autopulse`、Pythonパッケージ `auto_pulse`、ローカルイベント保管先 `%LOCALAPPDATA%/AutoPulse/queue/`、Issue本文マーカー `<!-- autopulse:start -->` / `<!-- autopulse:end -->` とする。旧仮称を新規作成物に使用しない。

## 指示の優先順位
1. 仕様書のセキュリティ・データ保護・Done判定を厳守する。
2. **作業開始時に実機を調査すること**。GitHub CLI、Codex CLI、Claude Code CLIのバージョンと有効なHook設定方式、Projectsのフィールド/権限を確認する。仕様書に記載したオプション名を鵜呑みにしない。
3. 調査はread-only。GitHub Project/リポジトリ/Issueを作成・更新する前に差分を提示し、私の許可を得ること。
4. 未検証の作業をDoneにしない。人間が書いたIssue本文、既存AGENTS.md、CLAUDE.mdを破壊しない。
5. トークンや会話内容をログ・Git履歴へ残さない。

## 作業内容
- 既存リポジトリを確認し、共通管理ツール用の場所を選定（初期候補 `autopulse`）。他プロジェクトに依存しない独立実装とする。
- Python 3.12以降、標準ライブラリ中心のCLIを構築する。GitHub API接続は`gh` CLIを利用し、プロセス起動と結果を抽象化して単体テスト可能にする。
- `doctor`, `init`, `status`, `start`, `note`, `block`, `review`, `complete`, `sync`, `reconcile`, `onboard` を実装する。初期段階で必要な機能に優先順位をつけ、段階ごとにテストする。
- Issue追加とProjectsのカスタムフィールド更新を検証。Projectsの実フィールドを取得して既存設定との衝突を検出する。
- ローカル耐障害キュー・イベントID・重複送信防止・競合検知・ドライランを作る。
- Codex/Claude共通の開発ルールと、両CLIの対応可能な設定をテンプレート化する。
- CIテストを整備する。GitHub ActionsからProjectsを更新する際の権限を確認し、権限がない場合に無理な書き込みをしない。
- 設置手順README、PowerShellの実行例、新規リポジトリ導入手順、トラブルシューティングを作る。

## 実装順序
1. 環境調査と実装計画を簡潔に提示。
2. GitHub API呼び出しのモックを用いたテストとCLI基本実装。
3. `doctor` と `status` をread-onlyで動かす。
4. `start/note/review/complete` とスプールの実装・テスト。
5. 実GitHub接続のドライラン（変更なし）。
6. 私の承認後、テスト用Issueで最小E2Eテスト。
7. 共通指示とHookを1リポジトリに導入。
8. 受入テストを実施し、仕様書の項目ごとに合否を報告。

## 完了報告のフォーマット
- 作成/変更したファイル
- 実装済みコマンド一覧
- 対応したCLIとバージョン、Hookの動作確認結果
- テスト実行コマンドおよび成功/失敗結果
- GitHub側に行った操作の一覧
- 未解決課題と次の手順

## 最初の返答
まず**変更を加えずに**、手元で確認できたバージョンと既存環境・必要な認証権限を報告し、そのうえで実装の段取りを提示してください。環境確認ができない項目は推測せず「未確認」としてください。
