# sampan

[English](README.en.md) · 繁體中文

在 tmux 裡「一個 session 一個 agent」（Claude Code 或 Codex）的管理工具。

sampan 記住每個 session 跑的是哪一段對話。agent 結束、tmux 關掉，或機器重開機之後，可以一個個接回原本的對話。
它也能讓多個 Claude profile 的 plugin 保持一致。

## 需求

- macOS（Linux 實驗性，見[平台與限制](docs/platforms.md)）
- tmux
- Python 3.11 以上、[uv](https://docs.astral.sh/uv/)
- Claude Code（`claude`），必要
- Codex CLI（`codex`），選用；不用 Codex 可以不裝

沒有任何第三方 Python 相依。套件名和指令都是 `sampan`。

## 安裝

```
curl -fsSL https://raw.githubusercontent.com/royalroylo/sampan/main/install.sh | sh
```

或手動兩行：

```
uv tool install git+https://github.com/royalroylo/sampan@v0.6.3
sampan init
```

`sampan init` 會問問題，請自己在終端機執行。

一鍵安裝做了什麼、看過再跑、Linux、新機器、PATH、`sampan init` 做了什麼，見[安裝說明](docs/install.md)。

提示：sampan 名字長，macOS 上 `sam` 加 Tab 會撞到 `/usr/bin/sample`。可在 shell 設定檔加一行 `alias sp=sampan`（sampan 本身不附 `sp`）。

## 裝了之後會改到什麼

- 設定放 `~/.config/sampan/`，登記表與歷史放 `~/.local/state/sampan/`。
- macOS：新增一個 launchd 排程，每小時跑 `sampan save`，只存檔。
- 不會動 Claude 的 `settings.json`，除非你自己跑 `sampan init --hook`（或同意修復以前裝的 hook）。
- 只有你動手（開、接回、停止、搬目錄）時，才會對 tmux 的 pane 打字。
- 網路：畫面開著時，最多每 6 小時到 GitHub 查一次 sampan 的版本 tag；按 `U` 或跑 `sampan update` 會當下重新查。
- `sampan init` 會問問題，要由你在終端機自己跑；沒有終端機時它會拒絕，除非加 `--yes`。

詳細見 [SECURITY.md](SECURITY.md)。

## 使用

打 `sampan`。左邊是 session 清單，右邊是游標所在那一個的資訊卡。

| 鍵 | 作用 |
|---|---|
| Enter | 進到該 session（已停止的先接回） |
| `1`–`9` | 跳到那一列（10 以上快速連打兩位數） |
| `/` | 依名稱搜尋（Enter 開啟，Esc 取消） |
| `n` | 開新的工作 |
| `s` | 收起來（agent 正常結束，對話保留） |
| `R` | 一次接回好幾個 |
| `m` | 換工作目錄 |
| `U` | 更新 sampan（當下重新查最新版；已是最新時可選擇重新安裝） |
| `q` | 離開 |

- 每天開工：`sampan`，Enter 進到要做的那個。
- 做到一半想收起來：`s`；要繼續時 Enter，會接回同一段對話。
- 重開機之後：`sampan`，按 `R` 確認要接回的。sampan 不會自己接回。
- 腳本或很窄的終端機：`sampan ui --plain`，或直接用各個指令。

完整的鍵位、指令、restore 的規則見[使用說明](docs/usage.md)。

## 文件

- [安裝](docs/install.md)
- [使用說明](docs/usage.md)
- [SSH 與 tmux 設定](docs/setup.md)
- [設定檔與登記表格式](docs/reference.md)
- [平台與限制](docs/platforms.md)
- [SECURITY.md](SECURITY.md)
- [CONTRIBUTING.md](CONTRIBUTING.md)
- [ARCHITECTURE.md](ARCHITECTURE.md)
- [CHANGELOG.md](CHANGELOG.md)

## 授權

MIT，見 [LICENSE](LICENSE)。
