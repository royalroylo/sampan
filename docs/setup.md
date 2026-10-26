# SSH 與 tmux 設定

[English](en/setup.md) · 繁體中文 · [回 README](../README.md)

## SSH 登入時自動開 sampan

在被連入那台機器的 `~/.zshrc` 最後加一行：

```
# SSH 登入、還沒在 tmux 裡、而且是互動終端機時，自動開 sampan
[[ -n "$SSH_CONNECTION" && -z "$TMUX" && -t 0 ]] && sampan
```

`scp`、`rsync`、`ssh 主機 指令` 這類非互動連線不會觸發；在 sampan 裡按 `q` 回到 shell。

不想改 `~/.zshrc`，也可以在自己電腦的終端機程式建一個 profile，Command 設成 `ssh -t 使用者@主機 sampan`（`-t` 不能省，否則沒有終端機畫面）。

## 建議的 tmux 設定

**不要**開 tmux-continuum 的自動還原（`@continuum-restore on`）或 tmux-resurrect 的自動接回：它們會在 sampan 之外還原 pane，與 `sampan restore` 的「由你確認、只用原對話」互相干擾。接回只交給 `sampan restore`。

## 讓 agent 用工具傳訊息（sampan MCP）

`sampan mcp install` 把 sampan MCP 裝進每個啟用的 Claude profile 與 Codex（`--dry-run` 先看會跑什麼指令，`sampan mcp uninstall` 移除）。裝好後重開該 profile 的 session。預設每次 `send`、`new_session`、`cancel_dispatch` 都會跳核准表單；用 `--dangerously-bypass-approvals-and-sandbox` 開的 Codex 會自動同意表單，見 [reference.md](reference.md#安裝與核准表單的前提)。
