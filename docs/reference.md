# 設定檔與登記表格式

[English](en/reference.md) · 繁體中文 · [回 README](../README.md)

給外部轉換或遷移腳本用。sampan 讀不懂的版本不會覆蓋，會報錯。

## `~/.config/sampan/config.toml`（`version = 1`）

| 欄位 | 說明 |
|---|---|
| `version` | 整數，必須是 `1` |
| `[restore] recent_days` | 非負整數，預設 7；`restore` 預設只勾選最近幾天有動過的 |
| `[ui] start_dir` | 字串路徑，`n`（開新工作）的選目錄畫面從這裡開始；沒寫時用 `~/Projects`（存在的話），否則 `~` |
| `[ui] codex_auto_start` | 布林，預設 `true`。打開 `sampan` 畫面時若 Codex 常駐服務沒在執行，就在背景跑一次 `codex remote-control start`；`false` 不啟動 |
| `[mcp] confirm` | 布林，預設 `true`。`sampan mcp serve` 的 `send`、`new_session`、`cancel_dispatch` 每次都用 MCP 核准表單（elicitation）問你，可以同意、拒絕或改訊息再送；呼叫端沒宣告支援 elicitation 就拒絕，不跳過。`false` 都不問、直接做（照樣記 `actions.log`、可取消）。唯讀的 `list_sessions`、`dispatch_result` 永遠不問 |
| `[codex] enabled` | 布林，預設 `true`。沒有任何 `agent = "codex"` 的 profile 時，`[codex]` 在記憶體裡當成一個隱含的 Codex profile，第一次 `sampan profile rename／disable／enable／forget／add --codex` 才轉成 `[profiles.<名稱>]`；只要有任何 `agent = "codex"` 的 profile，整個 `[codex]` 都不再作用；設成 `false` 表示不用 Codex（沒有 Codex profile），forget 最後一個 Codex profile 時會寫入 |
| `[codex] home` | Codex 資料夾；沒寫時用環境變數 `CODEX_HOME`，再沒有就是 `~/.codex`（只用在隱含的 Codex profile） |
| `[marketplaces] <名稱> = "owner/repo"` | plugin 來源：GitHub `owner/repo`，或 git 網址（`https://…`、`ssh://…`、`git@host:path`，例如自架的 Gitea）。本機目錄不支援 |
| `[profiles.<名稱>] config_dir` | 必填，字串路徑（可用 `~/`） |
| `[profiles.<名稱>] default` | 剛好一個 profile 要設 `true`（該 profile 啟動時不設 `CLAUDE_CONFIG_DIR`） |
| `[profiles.<名稱>] label` | 顯示用，例如帳號 email |
| `[profiles.<名稱>] agent` | `"claude"`（預設）或 `"codex"`；Codex profile 的 `config_dir` 是它的 `CODEX_HOME`，沒有 `plugins`。有多個 Codex profile 時，sampan 開 Codex 用第一個使用中的，執行中的 Codex session 也都算在它名下（多帳號還不支援） |
| `[profiles.<名稱>] disabled` | 布林，`true` 表示停用（隱藏、不能開新 session、plugins sync 跳過）；預設 profile 不能停用 |
| `[profiles.<名稱>] plugins` | 字串清單，每項 `plugin@marketplace`，marketplace 必須在 `[marketplaces]` |

路徑覆寫：環境變數 `SAMPAN_CONFIG`（設定檔，預設 `~/.config/sampan/config.toml`）、`SAMPAN_STATE_DIR`（狀態資料夾，預設 `~/.local/state/sampan/`）。

## `registry.json`（`version = 2`，在狀態資料夾裡）

```json
{
  "version": 2,
  "saved_at": "2026-01-01T09:00:00+00:00",
  "sessions": [
    {"tmux": "work", "agent": "claude", "profile": "alice", "session_id": "…",
     "cwd": "/Users/u/proj", "state": "active", "seen_at": "2026-01-01T09:00:00+00:00",
     "server": "…", "pid": 12345}
  ]
}
```

| 欄位 | 說明 |
|---|---|
| `tmux` | tmux session 名稱（唯一） |
| `agent` | `"claude"` 或 `"codex"` |
| `profile` | config.toml 的 profile 名稱；codex 為 `""` |
| `session_id` | Claude 的對話 id，或 Codex 的 thread id |
| `cwd` | 工作目錄（絕對路徑） |
| `state` | `"active"` 或 `"retired"` |
| `seen_at` | 最後一次看到的時間 |
| `server` | 最後一次看到時的 tmux server 識別（pid 與啟動時間），用來分辨「使用者關掉了」與「重開機後等待接回」 |
| `pid` | 最後一次看到的 agent 行程，整數或 `null` |

`version = 1` 的舊檔仍可讀（缺的 `agent` 視為 `claude`、`pid` 視為 `null`），下次寫入時升為 2。每次改寫前，若 session 有實際變動（`seen_at`、`pid`、`server` 不算），會把舊檔複製到 `history/registry-<時間>.json`（保留最近 20 份）。時間（`saved_at`、`seen_at`、歷史檔名）一律以 UTC 寫入（`+00:00`），畫面上顯示時才換成本地時間；舊版留下的歷史檔名可能是本地時間。

## `dispatches.json`（`version = 1`，在狀態資料夾裡）

派工紀錄：一則從呼叫者 session（或終端機）送給目標 session 的訊息。讀寫時用同資料夾的 `dispatches.lock` 鎖住；檔 600、資料夾 700。格式是 `{"version": 1, "dispatches": [...]}`，每筆：

| 欄位 | 說明 |
|---|---|
| `id` | 4 個小寫 hex |
| `from_name`、`from_profile`、`from_agent` | 呼叫者（人在終端機送的，`from_name` 是「終端機」；不是 sampan session 的 agent 透過 MCP 送的，是「未登記的 session（claude）」這樣，收不到完成通知） |
| `from_kind` | 呼叫者是哪一種：`terminal`（人在終端機）、`unregistered`（不是 sampan session 的 agent）、`session`（sampan session）。只有 `session` 才會依 `from_name` 認呼叫者（完成通知、MCP 看結果與取消）。沒有這欄的舊紀錄：`from_name` 是「終端機」視為 `terminal`，其他視為 `session` |
| `to_name`、`to_agent`、`to_profile` | 目標 session |
| `text` | 含來源行的完整送出內容 |
| `state` | `queued`、`sent`、`done`、`failed`、`cancelled` 其中之一 |
| `created`、`sent_at`、`done_at` | UTC ISO 時間；`sent_at`、`done_at` 可為 `null` |
| `reply`、`reason` | 回覆內容（或 `null`）；失敗或取消的原因 |
| `notify`、`notified` | 是否要在完成時通知呼叫者、是否已通知 |
| `thread_id` | 對話識別，或 `null` |

`sampan new --message` 建的派工一開始就是 `sent`（`sent_at` 是開新 session 的時間；`text` 與 `outbox/<id>.txt` 都不以換行結尾，因為 `$(cat …)` 會去掉結尾換行，不然回覆對不上）；`thread_id` 由之後的推進從狀態列補。

狀態怎麼走：`queued`（排隊）→ `sent`（已送出，`sent_at`）→ `done`（`reply` 是這則訊息的回覆）或 `failed`（`reason` 寫原因）；`cancelled` 只能從 `queued` 來（`sampan dispatches cancel`）。`done_at` 是結束的時間。推進只在鎖裡做（先讀最新的再判斷再寫），所以同一筆不會被送兩次；貼上後按 Enter 失敗（文字已在對方輸入框）直接算 `failed`，不會再貼一次（完成通知同理：算已通知，`reason` 寫明）。`sampan send` 的結束碼：0 送出（`--wait`：已回覆）、1 失敗、2 拒絕或讀不到檔、124 `--wait` 逾時（派工照樣留著）。完成通知（`notify`）用 `outbox/<id>-notice.txt` 送到呼叫者的 session。

讀不懂的檔不會覆蓋。已完成（`done`、`failed`、`cancelled`）超過 7 天的會被清掉，總數只留最新 200 筆。`outbox/<id>.txt` 放要送出的原文（檔 600）。

## `sampan mcp serve` 的工具

stdio MCP server（每行一則 JSON-RPC 2.0；協定版本 `2025-06-18`，client 要 `2025-03-26` 時用它）。由 Claude Code／Codex 啟動，呼叫者是誰從 server 的上層行程找：最近的 claude／codex 行程所在的 tmux pane，再對到登記表裡同名、同 agent 的那列；對不到就是「未登記的 session」（來源行與派工紀錄寫「未登記的 session（claude）」這樣，不會寫成「終端機」），工具照樣能用，只是收不到完成通知。每次呼叫會動作或看派工的工具前都重新找一次（Codex 要有對話後才登記、session 可能被接手或改名）；只有讀不到（tmux、ps 或登記表讀不到）時才沿用已知的身分；確定對不到登記表（沒有那列、agent 不同）就改成未登記。`dispatch_result`、`cancel_dispatch` 只認這個呼叫者發出的派工（這個 server 建的，或 `from_kind` 是 `session` 而且 `from_name` 是呼叫者的 session），別人的一律回「沒有派工 <編號>（或不是這個 session 發出的）」。工具回傳的文字是 JSON；拒絕（找不到對方、使用者拒絕、client 不支援核准表單）是 `isError: true` 的結果。

| 工具 | 參數 | 回傳 | 核准 |
|---|---|---|---|
| `list_sessions` | 無 | `sessions`：每列 `name`、`agent`、`profile`、`cwd`、`state`（`running`、`stopped`…）、`activity`（`idle`、`busy`…）、`last`（UTC ISO 或 `null`）；讀不到 tmux 時多一個 `warning` | 不問 |
| `send` | `session`、`message`、`wait`（預設 `false`） | `dispatch`、`state`，做完時有 `reply`，還沒送出時有 `reason`。`wait` 最多等 60 秒；沒等到的照樣留著，完成時往呼叫者的 session 送一則通知 | 問 |
| `new_session` | `name`、`target`（`claude:<profile>`、`<profile>` 或 `codex`）、`cwd`、`message` | `dispatch`（跟 `sampan new --message` 一樣，開場訊息的派工） | 問 |
| `dispatch_result` | `id`、`wait`（0–60 秒，預設 0） | `dispatch`、`state`、`label`、`to`、`reply`、`reason` | 不問 |
| `cancel_dispatch` | `id` | `dispatch`、`state`（只能取消排隊中的） | 問 |

核准表單寫明送給誰（名稱、profile、Claude／Codex）、從哪個 session、完整訊息，呼叫者是 sampan session 時加一句「完成後會在你的 session 送出一則通知」；agent 給的訊息一律放在固定的分隔線「──── 訊息內容（由 agent 提供）────」下面，分隔線以上只有 sampan 寫的字；名稱、目標、目錄有換行或控制字元會在問之前就拒絕，目錄顯示解析後的實際路徑。表單只有一個 `message` 欄位（預設是原訊息），改了就送改過的；同意後會再檢查一次對方（表單開著時對方可能停了）。`new_session` 跟 `sampan new` 用同一套檢查（名稱、profile 停用、Codex 停用、tmux session 已存在、目錄不存在），都在問之前做。同意派工就算同意它的完成通知，不另外問。server 活著時每 3 秒推進這個呼叫者發出的派工。


### 安裝與核准表單的前提

`sampan mcp install` 對每個啟用的 Claude profile 跑 `claude mcp add --scope user sampan -- <sampan 路徑> mcp serve`（帶該 profile 的 `CLAUDE_CONFIG_DIR`），Codex 啟用時跑 `codex mcp add sampan -- <sampan 路徑> mcp serve`（Codex 家目錄不是 `~/.codex` 時帶 `CODEX_HOME`）；已經裝了的略過。`sampan mcp uninstall` 對應 `remove`。兩者都有 `--dry-run`，每行結果記在 `actions.log`（標籤 `mcp`）。裝好後要重開該 profile 的 session 才看得到工具。`sampan doctor` 用一行顯示裝在哪些 profile（都沒裝時不顯示）。

`[mcp] confirm = true` 靠呼叫端真的把表單顯示給人。查證結果（2026-10-26）：

- **Codex CLI 支援 elicitation 表單。** 官方設定參考列出 `approval_policy = { granular = { mcp_elicitations = … } }`：「`true` 時 MCP elicitation 提示會顯示，不是自動拒絕」，也就是設成 `false` 會被自動拒絕（sampan 的 `send` 等會失敗，不會繞過核准）。第三方整理的 release notes 指出 v0.119／v0.120 起支援，而且在 `danger-full-access`（`--dangerously-bypass-approvals-and-sandbox`）下 Codex 會**自動同意**所有 elicitation。本機 `codex-cli 0.164.0` 的 `codex mcp --help` 沒有 elicitation 相關選項（只有 `-c`）。
- **所以：用 `--dangerously-bypass-approvals-and-sandbox` 開的 Codex，核准表單等於沒有。**`confirm = true` 擋不住這種呼叫端；要擋，就別用該模式，或不要在那個 agent 裝 sampan MCP。
- **Claude Code**：官方 MCP 文件有 elicitation 對話框與 `Elicitation` hook，非互動（headless）session 會要求 server 取消需要輸入的請求；文件**沒有**說 `bypassPermissions`／`--dangerously-skip-permissions` 會不會自動回答 elicitation，也沒有驗證過，視為未確認。另外 `Elicitation` hook 可以代為回答，裝了這種 hook 的 profile 同樣會繞過人。

來源：官方 [Codex 設定參考](https://developers.openai.com/codex/config-reference)（轉到 learn.chatgpt.com/docs/config-file/config-reference）；第三方 [MCP Server Elicitations in Codex CLI](https://codex.danielvaughan.com/2026/04/10/mcp-elicitation-codex-cli/)；Claude Code [MCP 文件](https://code.claude.com/docs/en/mcp)。Codex 官方 MCP 頁沒有 elicitation 的說明，版本細節只有第三方來源。
