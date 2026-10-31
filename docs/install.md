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
- 用 `uv tool install --reinstall git+https://github.com/royalroylo/sampan@<版本>` 安裝（預設是最新的 `v*` tag，查不到時用 `v0.15.2`）；已經裝過 sampan 就是原地升級。
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
uv tool install git+https://github.com/royalroylo/sampan@v0.15.2
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
uv tool install git+https://github.com/royalroylo/sampan@v0.15.2
sampan init
```

Claude Code 的安裝同上（`curl -fsSL https://claude.ai/install.sh | bash`，裝好後執行一次 `claude` 並登入）。

**自動存檔**：Linux 上 `sampan init` 會跳過自動存檔排程並告訴你。需要的話自己排，例如 cron（`crontab -e`）：

```
*/5 * * * * $HOME/.local/bin/sampan save --quiet
```

或 systemd user timer（已在 WSL2 實測，WSL 重開後 timer 會自己起來，`Persistent=true` 開機後補跑一次；存成 `~/.config/systemd/user/sampan-save.service` 與 `sampan-save.timer`，再 `systemctl --user enable --now sampan-save.timer`）：

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

### WSL

Windows 上要在 WSL（例如 Ubuntu）裡安裝和使用，步驟同上面的 Linux：

- 在 WSL 的終端機裡跑安裝腳本。在 Windows 的命令提示字元或 Git Bash 裡跑會停下來（`unsupported system MINGW…`）。
- WSL 裡的 Claude Code 要另外裝（Linux 版）、另外登入：WSL 的 `~/.claude` 跟 Windows 的不共用。
- session 盡量開在 WSL 自己的目錄（`~` 底下），`/mnt/c/…` 很慢。
- Ubuntu 的 tmux 常比 Homebrew 的舊（例如 3.4）。從 Mac 帶過來的 `~/.tmux.conf` 用到新版才有的選項（例如 3.5 的 `extended-keys-format`）時，每次接上 tmux 都會跳錯誤畫面；`sampan doctor` 會列出是哪幾行。改完要重開 tmux server 錯誤畫面才會消失。
- 自動存檔用上面的 systemd user timer：先在 `/etc/wsl.conf` 加 `[boot]` `systemd=true`，再重開 WSL。
- 重開 WSL 是 `wsl.exe --shutdown`（在 WSL 裡打 `wsl --shutdown` 會找不到指令）。之後開一個新的終端機分頁、用 `wsl ~` 進去，再 `sampan restore` 接回；在原本的分頁重新進 WSL，有的終端機（例如 WezTerm）按鍵會變成亂碼。
- Windows 重開機或 WSL 關掉時，tmux 和所有 session 都會結束，要自己 `sampan restore` 接回。
- plugin：把 Windows 上用的 marketplace 和 plugin 寫進 `config.toml`，再 `sampan plugins sync`。私有 repo 要先在 WSL 裡設好 git 認證，否則 marketplace 加不進來（`sampan doctor` 會報「清單上的 marketplace … 沒有加進來」）：自架的 Git 伺服器可用 `git config --global credential.helper store`；GitHub 可以借 Windows 的 Git Credential Manager：
  ```sh
  git config --global credential.helper ""   # 先清掉其他 helper
  git config --global --add credential.helper "/mnt/c/Program\ Files/Git/mingw64/bin/git-credential-manager.exe"
  git config --global credential.https://github.com.username <你的 GitHub 帳號>
  ```
- 使用者層級的 MCP（`claude mcp add --scope user`）不會跟著 plugin 裝好，要在 WSL 裡另外加。要 Node 的，先在 WSL 裝 Linux 版 Node（例如 NodeSource）：WSL 的 PATH 預設帶著 Windows 的路徑，`npx` 可能會用到 `/mnt/c/…` 裡 Windows 的那個。
- 把 Windows 上進行中的 Claude 對話搬進 WSL（手動）：
  1. 記下對話 id（`/status` 看得到），在 Windows 那邊 `/exit`。
  2. 把 `%USERPROFILE%\.claude\projects\C--Users-…\<id>.jsonl`（有同名資料夾的話一起）複製到 WSL 的 `~/.claude/projects/-mnt-c-Users-…/`（資料夾名稱是 WSL 裡的工作目錄把英數字以外的字元，包括 `/`、`.`、`_`、空白，都換成 `-`，例如 `/mnt/c/Users/a.b/my_app` → `-mnt-c-Users-a-b-my-app`）。
  3. 在 WSL 裡 `cd` 到同一個目錄（`/mnt/c/…`），`claude --resume <id>`，確認接上後再 `sampan adopt` 交給 sampan。
  Codex 的對話記錄裡寫的是 Windows 路徑（`C:\…`），目前沒有搬家的方法。

## sampan init 做什麼

`sampan init` 一開始會檢查 Homebrew、tmux、claude、codex（選用）與 PATH：缺 tmux 而有 Homebrew 時，問過你才執行 `brew install tmux`；其他缺的只列出安裝指令，由你自己執行（Homebrew 的安裝程式要輸入密碼）。缺 tmux 或 claude 時 init 會停下，什麼都不寫。`sampan doctor` 也做同樣的檢查，但只回報。

`sampan init` 會問幾個會寫入檔案的問題，所以 stdin 不是終端機時會拒絕執行（結束碼 2，什麼都不寫）；腳本裡要全部用預設值，加 `--yes`（每個問題照樣印出，後面附 `→ 預設（--yes）`）。**agent 不要替使用者執行 init**，請使用者自己在終端機跑。init 會偵測 `~/.claude`、`~/.claude-profiles/*/`（名稱含 `.retired.` 的目錄視為已退役，略過）、執行中 claude 的 `CLAUDE_CONFIG_DIR`，詢問 profile 名稱後寫出 `~/.config/sampan/config.toml`（寫入前會先顯示內容）。再次執行只會提議新增的項目，不會改動讀不懂的設定檔。init 也會：

- 選用：在各 profile 的 `settings.json` 安裝 SessionStart hook。**預設不問也不裝**，要的話執行 `sampan init --hook`；已裝但壞掉（指向不存在的 sampan）的 hook，不加 `--hook` 也會問你要不要修復。
- 安裝自動存檔排程（僅 macOS；先問，預設是；`sampan init --no-launchd` 略過）：`~/Library/LaunchAgents/io.github.royalroylo.sampan-save.plist`，每 5 分鐘跑 `sampan save --quiet`（電腦睡眠時錯過的那次，醒來時會補跑）。執行 init 時設了 `SAMPAN_CONFIG`／`SAMPAN_STATE_DIR` 的話，排程也用同樣的值。**排程只存檔，不會 restore、new 或 move。**

範例設定見 [config.example.toml](../config.example.toml)；格式說明見 [reference.md](reference.md)。
