# 安裝

[English](en/install.md) · 繁體中文 · [回 README](../README.md)

## 一鍵安裝

macOS 和 Linux 都適用：

```
curl -fsSL https://raw.githubusercontent.com/royalroylo/sampan/main/install.sh | sh
```

它會依序做這些事（每個要安裝的東西都會先問你；加 `--yes` 則全部視為「是」）：

- 檢查系統：macOS 或 Linux，其他系統直接停止。
- 缺 uv：問過後用 uv 官方安裝程式（`curl -LsSf https://astral.sh/uv/install.sh | sh`）安裝；不會幫你改 shell 設定檔。
- 缺 tmux：macOS 有 Homebrew 就問你要不要 `brew install tmux`；沒有 Homebrew 只印出 Homebrew 的安裝指令然後停止（不替你裝 Homebrew）。Linux 用 apt、dnf、pacman 或 zypper（不是 root 時用 sudo）；認不得的套件管理員就列出要裝什麼然後停止。
- Linux 另外確認 `ps`、`lsof`、`git`，缺的一起問、一起裝。
- 用 `uv tool install --reinstall git+https://github.com/royalroylo/sampan@<版本>` 安裝（預設是最新的 `v*` tag，查不到時用 `v0.12.0`）；已經裝過 sampan 就是原地升級。
- 沒有 Claude Code（`claude`）：**不會幫你裝**，只印出 `curl -fsSL https://claude.ai/install.sh | bash` 和「裝好後執行一次 `claude` 並登入」；sampan 照裝，但略過 `sampan init`（沒有 claude，init 會停下）。
- `~/.local/bin` 不在 PATH：印出該加到你的 shell（zsh、bash、fish）的那一行，不會自己改設定檔。
- 最後執行 `sampan init`（互動式，答案從終端機讀；`curl | sh` 的 stdin 是管線，所以改讀 `/dev/tty`；沒有終端機時略過並提示你自己跑），並印出 `sampan --version`。

選項（接在 `sh -s --` 後面，例如 `curl ... | sh -s -- --yes --no-init`）：`--yes`（有終端機時也會讓 `sampan init` 以 `--yes` 執行，所有問題用預設值，沒有終端機就略過 init；init 失敗時只提示，不中斷安裝）、`--no-init`、`--version vX.Y.Z`、`--help`。

**看過再跑**：不想直接把網路上的腳本接給 shell，可以先下載、讀完，再執行：

```
curl -fsSL https://raw.githubusercontent.com/royalroylo/sampan/main/install.sh -o install.sh
less install.sh
sh install.sh
```

腳本全部包在 `main` 函式裡、最後一行才呼叫，下載被截斷時不會執行到一半。唯一會「`curl | sh`」的是 uv 的官方安裝程式；Claude Code 只會印出指令。

## 手動安裝

不想用安裝腳本時，新機器照順序從上往下貼（已經有的步驟跳過；以下是 macOS，Linux 見下一節）：

```
# 1. Homebrew
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
# 2. tmux 和 uv
brew install tmux uv
# 3. Claude Code（裝好後執行一次 claude 並登入）
curl -fsSL https://claude.ai/install.sh | bash
# 4. sampan
uv tool install git+https://github.com/royalroylo/sampan@v0.12.0
sampan init
```

套件名和安裝後的指令都是 `sampan`。
`uv tool install` 把 `sampan` 裝在 `~/.local/bin`；若不在 PATH，在 `~/.zshrc` 加上 `export PATH="$HOME/.local/bin:$PATH"`。
升級：在 `sampan` 畫面按 `U`、執行 `sampan update [--yes] [--force]`，或 `uv tool install --reinstall git+https://github.com/royalroylo/sampan@v<新版號>`（`sampan --version` 看版本）。

## Linux

Linux 是實驗性支援（見[平台與限制](platforms.md)）。安裝腳本會替你處理下面這些；手動的話：

```
# Debian / Ubuntu
sudo apt-get update && sudo apt-get install -y tmux procps lsof git
# Fedora / RHEL
sudo dnf install -y tmux procps-ng lsof git
# uv，然後 sampan
curl -LsSf https://astral.sh/uv/install.sh | sh
uv tool install git+https://github.com/royalroylo/sampan@v0.12.0
sampan init
```

Claude Code 的安裝同上（`curl -fsSL https://claude.ai/install.sh | bash`，裝好後執行一次 `claude` 並登入）。

**自動存檔**：Linux 上 `sampan init` 會跳過自動存檔排程並告訴你。需要的話自己排，例如 cron（`crontab -e`）：

```
*/5 * * * * $HOME/.local/bin/sampan save --quiet
```

或 systemd user timer（**範例，未測試**；存成 `~/.config/systemd/user/sampan-save.service` 與 `sampan-save.timer`，再 `systemctl --user enable --now sampan-save.timer`）：

```
# sampan-save.service
[Service]
Type=oneshot
ExecStart=%h/.local/bin/sampan save --quiet

# sampan-save.timer
[Timer]
OnCalendar=*:0/5
Persistent=true
[Install]
WantedBy=timers.target
```

同樣只存檔，不會 restore、new 或 move。Linux 上的測試方式見 [CONTRIBUTING.md](../CONTRIBUTING.md#testing-on-linux)。

## sampan init 做什麼

`sampan init` 一開始會檢查 Homebrew、tmux、claude、codex（選用）與 PATH：缺 tmux 而有 Homebrew 時，問過你才執行 `brew install tmux`；其他缺的只列出安裝指令，由你自己執行（Homebrew 的安裝程式要輸入密碼）。缺 tmux 或 claude 時 init 會停下，什麼都不寫。`sampan doctor` 也做同樣的檢查，但只回報。

`sampan init` 會問幾個會寫入檔案的問題，所以 stdin 不是終端機時會拒絕執行（結束碼 2，什麼都不寫）；腳本裡要全部用預設值，加 `--yes`（每個問題照樣印出，後面附 `→ 預設（--yes）`）。**agent 不要替使用者執行 init**，請使用者自己在終端機跑。init 會偵測 `~/.claude`、`~/.claude-profiles/*/`（名稱含 `.retired.` 的目錄視為已退役，略過）、執行中 claude 的 `CLAUDE_CONFIG_DIR`，詢問 profile 名稱後寫出 `~/.config/sampan/config.toml`（寫入前會先顯示內容）。再次執行只會提議新增的項目，不會改動讀不懂的設定檔。init 也會：

- 選用：在各 profile 的 `settings.json` 安裝 SessionStart hook。**預設不問也不裝**，要的話執行 `sampan init --hook`；已裝但壞掉（指向不存在的 sampan）的 hook，不加 `--hook` 也會問你要不要修復。
- 安裝自動存檔排程（僅 macOS；先問，預設是；`sampan init --no-launchd` 略過）：`~/Library/LaunchAgents/io.github.royalroylo.sampan-save.plist`，每 5 分鐘跑 `sampan save --quiet`（電腦睡眠時錯過的那次，醒來時會補跑）。執行 init 時設了 `SAMPAN_CONFIG`／`SAMPAN_STATE_DIR` 的話，排程也用同樣的值。**排程只存檔，不會 restore、new 或 move。**

範例設定見 [config.example.toml](../config.example.toml)；格式說明見 [reference.md](reference.md)。
