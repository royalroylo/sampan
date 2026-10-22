# 平台與限制

[English](en/platforms.md) · 繁體中文 · [回 README](../README.md)

## 支援的平台

- **macOS**：完整支援（含每個整點的自動存檔排程）。
- **Linux**：實驗性。指令與畫面都能用，但沒有自動存檔排程——`sampan init` 會跳過 launchd 並告訴你，需要時自己跑 `sampan save`（或用 cron／systemd timer）；`sampan doctor` 不檢查 Homebrew 與 launchd。
  已在 Ubuntu 24.04（Docker）用假的 claude／codex 跑過 init、new、save、stop、restore 與 ui（[`scripts/linux-smoke/`](../scripts/linux-smoke/)），單元測試在 3.11–3.13 全綠；尚未用真的 Claude Code／Codex 在 Linux 上驗證。已知限制：需要 `ps`（procps），Codex 對話的辨識靠 `lsof`（沒有時退回 Codex 自己的資料庫，辨識率較低）；沒有 launchd，所以也沒有自動存檔。
- **Windows**：不支援。

Linux 的安裝（含一鍵安裝、cron／systemd 存檔範例）見[安裝：Linux](install.md#linux)；怎麼測試見 [CONTRIBUTING.md 的 Testing on Linux](../CONTRIBUTING.md#testing-on-linux)。

## Windows

sampan 依賴 Unix 的行程工具與 tmux，不支援 Windows。Windows 請用 [herdr](https://github.com/herdrdev/herdr)，並建議關掉它的 `resume_agents_on_restore`，避免開機後自動接回所有 agent。

## 限制

- 一個 tmux session 只能跑一個 agent；同一個 session 裡偵測到不只一個 agent，整個 session 不記、不接回、不停、不搬，並提醒你拆開。
- 找對話依賴 Claude Code 非公開的 session 檔（`<config_dir>/sessions/`）與逐字稿格式，以及 Codex 的 rollout 檔；它們改版時 sampan 可能需要跟著更新。`sampan doctor` 會提示已知的問題。
- 只完整支援 macOS；Linux 實驗性（沒有自動存檔排程）；不支援 Windows。
