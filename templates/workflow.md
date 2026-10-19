# AI 開発ワークフロー（AutoPulse）

このリポジトリでは、タスクの正本は GitHub Issues、進捗の一覧は GitHub Projects「{{PROJECT_TITLE}}」です。
進捗は AI が `autopulse` コマンドで**自分の判断で**記録します。ユーザーに確認したり、「進捗を更新して」と言われるのを待ったりしないでください。
多少の誤りは構いません。記録が止まるほうが困ります。

リポジトリ: `{{REPO}}`

## 1. セッションの始め
- セッション開始時に AutoPulse が現在の Issue 一覧と Next Action を表示します（Hook が有効な場合）。表示がなければ次を実行します。
  ```
  autopulse status --repo {{REPO}}
  ```
- ユーザーの依頼にいちばん近い Issue を選んで作業を始めます。迷ったら近いものを選んでよく、確認は不要です。
  ```
  autopulse start --repo {{REPO}} --issue <番号> --agent <claude|codex>
  ```
  （`--agent` は自分に合わせて `claude` または `codex`。Status が何でも start できます）
- 対応する Issue がなければ、自分で作ってすぐ始めます。
  ```
  autopulse create --repo {{REPO}} --agent <claude|codex> --start --title "<タイトル>" --summary "<何をするか>" --criteria "<完了条件>" --priority P2 --category Software
  ```

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
- 作業の途中で別の作業が必要になったら、`autopulse create` で Issue を作っておきます（すぐやらないなら `--start` は付けない）。

## 3. 作業が終わったら
- 終わったら完了にします。確かめたこと（テストの結果、コミット、動作確認）があれば `--evidence` に書きます。
  ```
  autopulse complete --repo {{REPO}} --issue <番号> --agent <claude|codex> --evidence "<確かめたこと>" --close
  ```
- 完了条件にチェックが無い項目があっても完了になります（足りない点は AutoPulse がコメントに書き残します）。
- 確認してもらいたい段階で止めたいときだけ `autopulse review --evidence "<…>"` を使います。

## 4. 守ること
- Issue 本文の人間が書いた部分は書き換えません。AutoPulse が更新するのは `<!-- autopulse:start -->` から `<!-- autopulse:end -->` の間だけです。
- Projects の Next Action を人が書き換えた場合、その Status の間は AutoPulse は上書きしません（終了コード 5）。人の値を尊重して続けます。
- 決まった設計判断は `docs/DECISIONS.md` に追記します。
- `autopulse` が失敗しても開発作業は止めません。終了コード 3（キュー保存）はそのまま進めてよく、後で自動的に再送されます。
- 1 つのリポジトリで複数の Issue が {{STATUS_IN_PROGRESS}} でも構いません。コマンドには必ず対象の `--issue` を明示します。

## 終了コード

Codex など、コマンドを `pwsh -Command` 経由で実行する環境では、0 以外の終了コードがすべて 1 に見えることがあります。
その場合は出力の文面（「キューに保存しました」など）で判断してください。
Codex のサンドボックス内では GitHub に書き込めないため、記録は一時キューに保存され、応答の終わりに Hook が送信します。

| コード | 意味 | AI の対応 |
| --- | --- | --- |
| 0 | 反映した | そのまま続ける |
| 3 | キューに保存（GitHub 未反映） | 続けてよい |
| 4 | 方針により拒否 | 理由を読み、別の方法で記録する |
| 5 | 人の編集と競合し、現行値を保持 | 人の値を尊重して続ける |
| 6 | 設定・認証の問題 | `autopulse doctor` の結果をユーザーに伝える |
