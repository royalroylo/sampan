# 設定檔與登記表格式

[English](en/reference.md) · 繁體中文 · [回 README](../README.md)

給外部轉換或遷移腳本用。sampan 讀不懂的版本不會覆蓋，會報錯。

## `~/.config/sampan/config.toml`（`version = 1`）

| 欄位 | 說明 |
|---|---|
| `version` | 整數，必須是 `1` |
| `[restore] recent_days` | 非負整數，預設 7；`restore` 預設只勾選最近幾天有動過的 |
| `[ui] start_dir` | 字串路徑，`n`（開新工作）的選目錄畫面從這裡開始；沒寫時用 `~/Projects`（存在的話），否則 `~` |
| `[codex] enabled` | 布林，預設 `true`。沒有任何 `agent = "codex"` 的 profile 時，`[codex]` 在記憶體裡當成一個隱含的 Codex profile，第一次寫設定檔的 profile 指令才轉成 `[profiles.<名稱>]`；設成 `false` 表示不用 Codex（沒有 Codex profile），forget 最後一個 Codex profile 時會寫入 |
| `[codex] home` | Codex 資料夾；沒寫時用環境變數 `CODEX_HOME`，再沒有就是 `~/.codex` |
| `[marketplaces] <名稱> = "owner/repo"` | plugin 來源：GitHub `owner/repo`，或 git 網址（`https://…`、`ssh://…`、`git@host:path`，例如自架的 Gitea）。本機目錄不支援 |
| `[profiles.<名稱>] config_dir` | 必填，字串路徑（可用 `~/`） |
| `[profiles.<名稱>] default` | 剛好一個 profile 要設 `true`（該 profile 啟動時不設 `CLAUDE_CONFIG_DIR`） |
| `[profiles.<名稱>] label` | 顯示用，例如帳號 email |
| `[profiles.<名稱>] agent` | `"claude"`（預設）或 `"codex"`；Codex profile 的 `config_dir` 是它的 `CODEX_HOME`，沒有 `plugins` |
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

`version = 1` 的舊檔仍可讀（缺的 `agent` 視為 `claude`、`pid` 視為 `null`），下次寫入時升為 2。每次改寫前會把舊檔複製到 `history/registry-<時間>.json`（保留最近 20 份）。時間（`saved_at`、`seen_at`、歷史檔名）一律以 UTC 寫入（`+00:00`），畫面上顯示時才換成本地時間；舊版留下的歷史檔名可能是本地時間。
