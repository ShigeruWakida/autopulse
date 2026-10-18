# AI 開発ワークフロー（AutoPulse）

このリポジトリでは、タスクの正本は GitHub Issues、進捗の一覧は GitHub Projects「{{PROJECT_TITLE}}」です。
進捗は AI が `autopulse` コマンドで記録します。ユーザーに「進捗を更新して」と言われるのを待たないでください。

リポジトリ: `{{REPO}}`

## 1. セッションの始め
- セッション開始時に AutoPulse が現在の Issue 一覧と Next Action を表示します（Hook が有効な場合）。表示がなければ次を実行します。
  ```
  autopulse status --repo {{REPO}}
  ```
- 作業する Issue を 1 つ決めます。ユーザーの依頼がどの Issue か分からないときは、推測で決めずに確認します。
- 作業を始めるときに一度だけ記録します（`--agent` は自分に合わせて `claude` または `codex`）。
  ```
  autopulse start --repo {{REPO}} --issue <番号> --agent <claude|codex>
  ```
- 対応する Issue がない作業は、Issue を作るかユーザーに確認してから始めます。Issue は `.github/ISSUE_TEMPLATE/ai-task.yml` の形式に従います。

## 2. 作業中
- 意味のある区切り（実装の一段落、調査の結論、方針変更）ごとに記録します。
  ```
  autopulse note --repo {{REPO}} --issue <番号> --agent <claude|codex> --summary "<何をしたか>" --next "<次に取りかかれる具体的な一文>"
  ```
- `--summary` と `--next` には事実だけを短く書きます。会話の本文、秘密情報（トークン、パスワード、個人情報）は書きません。
- 外部の依存や判断待ちで進めなくなったら:
  ```
  autopulse block --repo {{REPO}} --issue <番号> --agent <claude|codex> --reason "<何を待っているか>"
  ```

## 3. 実装が終わったら
- テスト・確認の結果を証跡として Review にします。
  ```
  autopulse review --repo {{REPO}} --issue <番号> --agent <claude|codex> --evidence "<テストコマンドと結果 / PR / コミット>"
  ```
- Done にできるのは、次をすべて満たすときだけです。
  1. Issue の Acceptance Criteria がすべてチェック済み
  2. Verification に書かれた検証を実際に実行して成功した
  3. Acceptance Criteria に `[human]`（人間承認）が付いた項目がない
  ```
  autopulse complete --repo {{REPO}} --issue <番号> --agent <claude|codex> --evidence "<証跡>" --verified
  ```
- 条件を満たさない場合 AutoPulse は Done にせず Review に留めます（終了コード 4）。その結果を受け入れ、ユーザーに何が残っているかを伝えます。`--verified` を検証なしで付けてはいけません。

## 4. 守ること
- 仕様・優先順位・設計判断を AI が勝手に確定しません。決まった判断は、ユーザーの了承を得て `docs/DECISIONS.md` に追記します。
- Issue 本文の人間が書いた部分を書き換えません。AutoPulse が更新するのは `<!-- autopulse:start -->` から `<!-- autopulse:end -->` の間だけです。
- Projects の Next Action を人が書き換えた場合、その Status の間は AutoPulse は上書きしません（終了コード 5 で通知）。人の指示を優先して作業します。
- `autopulse` が失敗しても開発作業は止めません。終了コード 3（キュー保存）はそのまま進めてよく、後で `autopulse sync` が再送します。
- 1 つのリポジトリで複数の Issue が In Progress でも構いません。コマンドには必ず対象の `--issue` を明示します。

## 終了コード

Codex など、コマンドを `pwsh -Command` 経由で実行する環境では、0 以外の終了コードがすべて 1 に見えることがあります。
その場合は出力の文面（「キューに保存しました」「Review に留めました」など）で判断してください。
Codex のサンドボックス内では GitHub に書き込めないため、記録は一時キューに保存され、応答の終わりに Hook が送信します。
| コード | 意味 | AI の対応 |
| --- | --- | --- |
| 0 | 反映した | そのまま続ける |
| 3 | キューに保存（GitHub 未反映） | 続けてよい。後で sync |
| 4 | 方針により拒否 / Review に留めた | 理由を読み、ユーザーに伝える |
| 5 | 人の編集と競合し、現行値を保持 | 人の値を尊重して続ける |
| 6 | 設定・認証の問題 | `autopulse doctor` の結果をユーザーに伝える |
