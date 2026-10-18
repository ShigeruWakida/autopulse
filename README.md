# AutoPulse

AI（Codex CLI / Claude Code）が開発作業の進捗を GitHub Issues と GitHub Projects に自動で記録するための CLI です。
ユーザーが毎回「進捗を更新して」と指示しなくても、AI がルール（`.ai/workflow.md`）に従って `autopulse` を呼び、
Issue のコメント・本文の管理欄・Projects のフィールドを更新します。

- タスクの正本は **GitHub Issues**、一覧は **GitHub Projects**（User Project v2）
- 「実装が終わった」と「検証が終わった」を分けます。検証の証跡がなければ Done にしません
- 人間が書いた Issue 本文・コメント・Projects の値を上書きしません
- 回線断や GitHub の障害でも記録は消えず、後で `autopulse sync` が再送します
- ツールが動かなくても開発は止めません（Hook は常に成功扱いで終わります）

仕様: [AutoPulse_Spec_v1.1.md](AutoPulse_Spec_v1.1.md)

## 必要なもの

| もの | 確認方法 |
| --- | --- |
| Windows 10/11 + PowerShell 7 | `$PSVersionTable.PSVersion` |
| Python 3.12 以降 | `python --version` |
| GitHub CLI（`gh`） | `gh --version`（なければ `winget install --id GitHub.cli`） |
| `gh` の認証（`repo` と `project` スコープ） | `gh auth status` |
| uv（推奨。`autopulse` コマンドの導入に使用） | `uv --version` |

外部 Python パッケージは使いません（標準ライブラリのみ）。

## 導入

```powershell
# 1. 取得
git clone https://github.com/ShigeruWakida/autopulse.git $HOME\Documents\AutoPulse

# 2. autopulse コマンドを導入（ソースを直接参照する editable 導入。templates/ を使うため editable にする）
uv tool install --editable $HOME\Documents\AutoPulse
autopulse --version

# 3. GitHub にログイン（Projects 操作には project スコープが必要）
gh auth login --scopes project
#   すでにログイン済みならスコープだけ追加:
gh auth refresh -s project

# 4. 診断（read-only）
autopulse doctor
```

`uv` を使わない場合は `python -m pip install --user -e $HOME\Documents\AutoPulse` でも導入できます。
導入せずに試すだけなら、リポジトリ直下で `$env:PYTHONPATH="src"; python -m auto_pulse doctor` と実行できます。

## 初期化（Project とフィールドの作成）

`init` は既定で **dry-run** です。差分を確認してから `--apply` を付けて実行します。

```powershell
# 差分を表示するだけ（何も変更しない）
autopulse init --owner <login> --title "Personal Development" --repo <login>/<repo>

# 内容を確認して実行。新規 Project の Status（Todo/In Progress/Done）を 6 状態に置き換えるには
# --update-status-options が必要（Project の項目が 0 件のときだけ実行されます）
autopulse init --owner <login> --title "Personal Development" --repo <login>/<repo> --apply --update-status-options
```

作成されるフィールド:

| フィールド | 型 | 値 |
| --- | --- | --- |
| Status | Single select | 1.未着手, 2.着手可, 3.作業中, 4.停止中, 5.確認待ち, 6.完了（Backlog〜Done。表示名は `[statuses]` で変更可） |
| Priority | Single select | P0, P1, P2, P3（未設定なら P2 を入れます） |
| Category | Single select | Spec, Hardware, PCB, Firmware, Software, Test, Docs, Other |
| Last Activity | Date | 記録された作業があった日（start だけでは更新しません） |
| Next Action | Text | 次に取りかかれる具体的な一文 |
| AI Agent | Single select | Codex, Claude, ChatGPT, Mixed |
| Effort | Number | 人が設定します。AutoPulse は書き換えません |
| Repo Name | Text | リポジトリ名だけ（例 `RoboScope`）。標準の Repository 列は `owner/name` 表示で変えられないため、こちらを表示して Repository 列は隠します |

**表示列は手動で一度だけ設定します。** API で作ったフィールドは既存のビュー（`View 1`）の列に自動では加わらず、
GitHub には View を変更する API もありません。Project の画面で表の見出し右端の「+」から
Repo Name / Priority / Next Action / Last Activity / AI Agent / Category を表示し、Save してください
（標準の Repository 列は `owner/name` で表示されるので、表示しているなら隠します）。
既存の項目の Repo Name 列は `autopulse reconcile --apply` で埋まります。
Status ごとの列で見たい場合は「+ New view」で Board を作ります。

既存の Project に同名で型の違うフィールドがある場合は、削除や作り直しはせず「衝突」として報告して止まります。
Status の表示名は `config.toml` の `[statuses]` で決まります（既定は番号付きの日本語）。ルールは内部の名前（Backlog〜Done）で動くので、
表示名を変えるときは Project の Settings で選択肢の名前を変えてから `[statuses]` を合わせます（選択肢の名前変更なら各項目の値は保たれます）。

設定は `%LOCALAPPDATA%\AutoPulse\config.toml` に保存されます（例: [examples/config.toml](examples/config.toml)）。
書き込むのは `[repos] allow` に載っているリポジトリだけです。

## リポジトリへの導入（onboard）

```powershell
# 変更内容の一覧（dry-run）
autopulse onboard --repo <login>/<repo> --path C:\path\to\clone
# 差分も表示
autopulse onboard --repo <login>/<repo> --path C:\path\to\clone --diff
# 実行
autopulse onboard --repo <login>/<repo> --path C:\path\to\clone --apply
```

| ファイル | 既存のとき |
| --- | --- |
| `AGENTS.md`, `CLAUDE.md` | 末尾に `<!-- autopulse:onboard:start -->` 〜 `end` のブロックを追記（既存の内容は残す） |
| `.ai/workflow.md` | 変更しない（`--update-workflow` で更新） |
| `.github/ISSUE_TEMPLATE/ai-task.yml`, `docs/DECISIONS.md`, `docs/STATUS.md` | 変更しない |
| `.claude/settings.json`（Claude Code の Hook） | 既存の設定を残して AutoPulse の Hook を追加 |
| `.codex/hooks.json`（Codex の Hook） | 同上。ただし `~/.codex/hooks.json` に AutoPulse の Hook があれば作らない（二重実行を防ぐ） |

- 上書き・追記する前のファイルは `%LOCALAPPDATA%\AutoPulse\backups\` にコピーされます（リポジトリには入りません）
- GitHub に `ai-managed` ラベルがなければ作成し、リポジトリを allowlist に追加します
- **コミットはしません**。差分を確認してから自分でコミットしてください
- Codex の Hook は信頼の確認が必要です。Codex で `/hooks` を開き、AutoPulse の Hook を確認して信頼してください

### Codex の Hook を PC ごとに入れる（推奨）

`.codex/` を PC 固有として git から除外しているリポジトリでは、リポジトリ側の `.codex/hooks.json` は他の PC に届きません。
その場合は各 PC で一度だけ、ユーザー設定（`~/.codex/hooks.json`）に入れます。allowlist にないリポジトリでは何もしないので、
すべてのリポジトリで共通に使えます。

```powershell
autopulse install-hooks           # 差分を表示（dry-run）
autopulse install-hooks --apply   # 既存の Hook を残して追加。書き換え前の内容はバックアップ
```

導入後、Codex で `/hooks` を開いて信頼してください。

### Hook の動き

| Hook | Claude Code | Codex | 動作 |
| --- | --- | --- | --- |
| SessionStart | ○ | ○ | 進行中の Issue と Next Action を AI の文脈に表示 |
| Stop | ○ | ○ | 未送信イベントがあれば送信（なければネットワークに触れず即終了） |
| SessionEnd | ○ | — | 同上 |

Hook は常に終了コード 0 で終わり、AI を止めたり（block）、会話を続けさせたりはしません。
セッションの開始・終了を完全には検知できない前提で、記録そのものは AI がルールに従って `start/note/review/complete` を呼んで行います。

## 日々の使い方

ふだんは AI が `.ai/workflow.md` に従って実行します。手で実行する場合の例:

```powershell
$repo = "<login>/<repo>"
autopulse status --repo $repo                         # 一覧（Status / Last Activity / Next Action）
autopulse status --repo $repo --issue 12              # 1 件の詳細（完了条件のチェック状況も表示）

autopulse start    --repo $repo --issue 12 --agent claude
autopulse note     --repo $repo --issue 12 --agent claude --summary "UART 受信を実装" --next "受信バッファの溢れを試験"
autopulse block    --repo $repo --issue 12 --agent claude --reason "評価ボードの到着待ち"
autopulse review   --repo $repo --issue 12 --agent claude --evidence "pytest 42 passed (commit abc1234)"
autopulse complete --repo $repo --issue 12 --agent claude --evidence "実機で 1 時間連続受信 OK" --verified --close

autopulse sync                  # 未送信イベントを再送（バックオフ中のものは待つ）
autopulse sync --force          # バックオフを無視して今すぐ再送
autopulse sync --retry-failed   # 失敗扱いのイベントもキューに戻して再送
autopulse reconcile             # Issue / Projects / キューの矛盾を報告（dry-run）
autopulse reconcile --apply     # 安全な修正（ai-managed Issue の Projects 追加、Repo Name 列の補完）だけ実行
```

- `--repo` を省略すると、カレントディレクトリの git `origin` から推定します
- 変更系コマンドはすべて `--dry-run`（GitHub を読むだけ）と `--json`（機械可読な出力）に対応します
- `AUTOPULSE_AGENT=claude` のように環境変数を設定すると `--agent` を省略できます

### 状態遷移

```
1.未着手 →(人) 2.着手可 → 3.作業中 ⇄ 4.停止中
                             ↓
                          5.確認待ち → 6.完了
6.完了 → 3.作業中 / 4.停止中（--reason 必須）
```

- 1.未着手 から直接 `start` はできません（要件を確認して人が 2.着手可 にしてから）
- `complete` が Done にするのは次をすべて満たすときだけです。満たさなければ Review に留めます（終了コード 4）
  - `--verified` と `--evidence` がある
  - Issue 本文の Acceptance Criteria がすべてチェック済み
  - Acceptance Criteria に `[human]` / `人間承認` を含む項目がない（人間だけが Done にできる）

### 人間の編集との関係

- Issue 本文で AutoPulse が書き換えるのは `<!-- autopulse:start -->` 〜 `<!-- autopulse:end -->` の間だけです。
  その中を人が編集した場合は検出して更新を止め、コメントで知らせます
- 本文の更新直前に読み直し、同時に誰かが編集していたら最大 3 回やり直します
- Projects の Next Action を人が書き換えた場合、その Issue の Status が変わるまで AI の値で上書きしません。
  人が欄を空にすれば、すぐ AI に戻ります
- Effort と既存の Priority / Category は書き換えません

### 終了コード

| コード | 意味 |
| --- | --- |
| 0 | 成功（または反映済みで何もしなかった） |
| 1 | 予期しないエラー / GitHub が恒久的なエラーを返した |
| 2 | 引数の誤り |
| 3 | GitHub に反映できずキューに保存した（`sync` で再送） |
| 4 | 方針により拒否、または Done にせず Review に留めた |
| 5 | 人の編集と競合し、現行値を保持した |
| 6 | 設定・認証・権限の問題（`autopulse doctor` を参照） |

## ChatGPT で検討しているプロジェクト

ChatGPT は GitHub に書き込まず（仕様 §9）、検討で決まったことだけを「決定記録」として持ち帰ります。

```powershell
# 1. 書式を表示して、ChatGPT の Project の指示（Instructions）に貼る
autopulse decision-template --repo ScanPilot

# 2. 検討の区切りで ChatGPT が出した決定記録をファイルに保存し、取り込む内容を確認（dry-run）
autopulse import-decisions --repo ScanPilot --file C:\path\to\record.md --path C:\path\to\ScanPilot

# 3. 反映する
autopulse import-decisions --repo ScanPilot --file C:\path\to\record.md --path C:\path\to\ScanPilot --apply
```

| 決定記録の節 | 反映先 |
| --- | --- |
| 決定 | `docs/DECISIONS.md` に追記。Issue 番号があればその Issue に ChatGPT の記録としてコメント（AI Agent・Last Activity も更新）。**Status は変えない**（完了にするのは人） |
| 新しくやること | Issue を作り、Projects に 1.未着手 で追加（同じタイトルがあれば作らない） |
| 未決 | Issue にはしない。記録ファイルに残す |
| 記録そのもの | `docs/records/<日付>-<ハッシュ>.md` に保存 |

同じ記録を二度取り込んでも、追記・コメント・Issue は重複しません。`docs/` の変更はコミットしないので、確認してからコミットします。
`--repo` は `owner/name` のほか、設定の owner を補って `ScanPilot` だけでも指定できます。

## 新しいリポジトリを加える手順

1. GitHub にリポジトリを作り、ローカルにクローンする
2. `autopulse onboard --repo <login>/<repo> --path <clone>` で差分を確認
3. `--apply` で導入し、差分を確認してコミット・push
4. Codex を使う場合は、その PC でまだなら `autopulse install-hooks --apply` を実行し、`/hooks` で Hook を信頼する
5. `autopulse doctor` で `repo:<login>/<repo>` が OK になることを確認

## データの置き場所

| パス | 内容 |
| --- | --- |
| `%LOCALAPPDATA%\AutoPulse\config.toml` | 設定（秘密情報は含みません。認証は常に `gh` から） |
| `%LOCALAPPDATA%\AutoPulse\queue\` | 未送信イベント（送信後に削除） |
| `%LOCALAPPDATA%\AutoPulse\queue\failed\` | 恒久的に失敗したイベント（`sync --retry-failed` で戻せる） |
| `%LOCALAPPDATA%\AutoPulse\backups\` | onboard が書き換える前のファイル |
| `%LOCALAPPDATA%\AutoPulse\hook.log` | Hook の実行記録（1 回 1 行。イベント・所要時間・結果） |

イベントには会話の本文を入れません。`--summary` などの自由文はトークン・パスワードらしき文字列を伏せてから保存します。
`AUTOPULSE_HOME` / `AUTOPULSE_CONFIG` 環境変数で場所を変えられます。

## GitHub Actions について

CI（`.github/workflows/test.yml`）は単体テストだけを実行し、Projects には触れません。
`GITHUB_TOKEN` では User Project を更新できないため、Actions から Projects を照合・更新するには
スコープを絞った PAT か GitHub App を Secrets に用意する必要があります。初期版ではこれを用意せず、
照合はローカルの `autopulse reconcile` で行います（仕様 Phase 4 で検討）。

## トラブルシューティング

| 症状 | 対処 |
| --- | --- |
| `gh` が見つからない（インストール直後） | 新しいターミナルを開く。MulmoTerminal などから起動している場合はその親アプリも起動し直す |
| `doctor` で auth が FAIL / `missing required scopes` | `gh auth refresh -s project` |
| 終了コード 3 が続く | `autopulse sync --dry-run` で `last_error` を確認。回線・プロキシ・GitHub の状態を確認して `autopulse sync --force` |
| 失敗イベントがある（`doctor` の queue が WARN） | 原因を直してから `autopulse sync --retry-failed` |
| `Backlog の Issue は...` で start できない | Projects で Status を Ready にしてから start |
| complete が Review に留まる | 出力の理由（未チェックの完了条件、`--verified` なし、`[human]` 項目）を解消する |
| 管理欄が更新されない（競合） | 管理欄の中を手で編集すると AutoPulse は更新を止めます。管理欄のブロックを削除すると次回作り直されます |
| 出力が文字化けする | パイプ時は UTF-8 で出力します。PowerShell 側は `[Console]::OutputEncoding = [Text.Encoding]::UTF8` |
| Codex の Hook が動かない | Codex で `/hooks` を開き、信頼されているか確認。プロジェクトの `.codex/` が信頼済みか確認 |
| Hook が `Failed` と表示される | `%LOCALAPPDATA%\AutoPulse\hook.log` を確認。記録がなければ Hook 自体が起動していない（PATH・信頼設定）。記録があれば AutoPulse は正常終了しており、出力形式の問題 |
| `autopulse` が Hook から見つからない | `uv tool install --editable` で導入し、`%USERPROFILE%\.local\bin` が PATH にあるか確認 |

## 開発

```powershell
$env:PYTHONPATH = "src"
python -m unittest discover -s tests -t .
```

GitHub へのアクセスはすべて `gh` のサブプロセス経由で、`Runner` を差し替えるとテストで模擬できます（`tests/fakes.py`）。
