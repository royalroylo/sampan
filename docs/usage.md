# 使用說明

[English](en/usage.md) · 繁體中文 · [回 README](../README.md)

## 先懂三件事

- **一個 tmux session = 一個 agent。** 每個 session 裡跑一個 Claude Code（某個 profile）或 Codex，做一件事、放在一個專案目錄。
- **sampan 有一本登記表**，記住每個 session 的名稱、agent、profile、目錄，以及**它正在進行的那段對話**。所以 agent 結束、tmux 關掉、甚至重開機之後，都能接回同一段對話，就像沒停過。
- **每個 session 有三種狀態：**

  | 狀態 | 意思 | 怎麼變成這樣 |
  |---|---|---|
  | 執行中 | tmux 裡的 agent 正在跑 | `n` 開新、接回、接手 |
  | 已停止 | agent 已結束，對話保留著，需要時接回 | `s` 停掉、關機、重開機 |
  | retired | 這件事做完了，以後不用接回 | `r` retire（`a` 可恢復） |

## 怎麼用（大部分時候只要打 `sampan`）

打 `sampan` 會開一個畫面：左邊是 session 清單，右邊是游標所在那一個的資訊卡（狀態、目錄、對話、最後活動、context 用量、最後一句話，以及這一列能按的鍵），下面是訊息區。有在 tmux 裡跑的 session，右邊會即時顯示它的畫面（每秒更新、只能看不能打字；要操作就按 Enter 進去）。所有問題、進度、結果都在這個畫面裡，做完一個動作會回到清單；按 `q` 才離開。

| 想做的事 | 在 `sampan` 裡按 | 會發生什麼 |
|---|---|---|
| 進到某個 session 工作 | 移到那一列，Enter | 進到它的 tmux（已在 tmux 裡就直接切過去）。已停止的會先接回再進去 |
| 跳到某一列 | `1`–`9`（數字是該列在完整清單的位置） | 游標跳到那一列；10 以上在 1 秒內連打兩位數，例如 `1`、`2` 是第 12 列 |
| 依名稱搜尋 | `/`，再打文字 | 不分大小寫，名稱含有該文字的列留下來；Enter 開啟游標那一列，Esc 取消，空的搜尋框按 Backspace 離開搜尋。搜尋時數字當文字用，不會跳列 |
| 開一個新的工作 | `n` | 問名稱、agent／profile，再用選目錄畫面挑工作目錄（從 `[ui] start_dir` 開始；沒設時用 `~/Projects`（存在的話），否則家目錄），開好後直接進去 |
| 暫時收起來（省記憶體、清畫面） | `s`（空白鍵可勾多個） | 在畫面底部問確定；agent 正常結束，tmux 也一起關掉。對話保留，之後 Enter 或 `R` 接回 |
| 一次接回好幾個 | `R` | 勾選清單（預設勾最近 7 天有動過的），接回後進到第一個 |
| 把 tmux 外的終端機（任何終端機程式）裡的 Claude 收進來 | `A` | 勾選要接手的，逐一取名；你自己在原本那個終端機分頁打 `/exit`，sampan 等它結束後在 tmux 接回同一段對話，然後進去 |
| 改名 | `e` | tmux 與登記表一起改，agent 照常跑 |
| 換工作目錄 | `m` | 用選目錄畫面挑新目錄（從它現在的目錄開始；那個目錄已經不在時，從 `[ui] start_dir` 開始，和 `n` 一樣），確認後 agent 結束、在新目錄接回同一段對話 |
| 這件事做完了 | `r` | 標成 retired，之後不會出現在接回清單 |
| 立刻存一次登記表 | `w` | 平常每個整點會自動存 |
| 更新 sampan | `U`（有新版時按鍵說明上面會出現「有新版 v…（目前 …），按 U 更新」） | 在畫面底部問確定，跑 `uv tool upgrade sampan`（版本沒變再 `uv tool install --reinstall git+https://github.com/royalroylo/sampan@v<最新版>`），輸出顯示在訊息區；成功後自動重新開啟 sampan 回到清單，失敗就留在清單。不知道有新版時，`U` 會先當下重新查一次（不看快取）；仍是最新版（或查不到）就問要不要重新安裝目前版本 |

**選目錄畫面**（`n`、`m`）：左邊是目前目錄底下的資料夾，右邊是「最近用過」的目錄（登記表裡用的 session 多的在前），游標那個目錄裡已經有 session 時會列出來；終端機不到 100 欄時只顯示左邊。

| 鍵 | 作用 |
|---|---|
| ↑↓ | 移動 |
| → 或 Enter | 進入游標那個資料夾（Enter 在 `..` 上 = 上一層） |
| ← 或 Backspace | 上一層 |
| 空白 | 選「目前這個目錄」（標題列顯示的那個） |
| `+` | 在目前目錄新建資料夾並進入 |
| `/` | 直接打路徑（`~` 可用；`n` 時目錄要已存在，`m` 時不存在會建立） |
| Tab | 切到右邊「最近用過」，在那裡 Enter／空白 直接選那個目錄 |
| `.` | 顯示／隱藏 `.` 開頭的資料夾 |
| Esc | 取消 |

選到家目錄 `~` 本身時會再問一次（建議用專案目錄）。`sampan ui --plain` 仍是直接打路徑。

**新版提示**：`sampan` 開啟時在背景查 GitHub 上最新的版本 tag（結果快取 6 小時，畫面開著時過期會再查；沒網路就不提示），有新版才顯示提示、按 `U` 更新。`--plain` 只看快取、在開頭印一行；每小時的自動存檔不檢查。

## 常見情境

**每天開工**：打 `sampan`，Enter 進到要做的那個。要做新的事就按 `n`。

**做到一半想先收起來**：在清單上按 `s`。之後要繼續，移到那一列按 Enter，會接回同一段對話並進去。

**關機或重開機**：關機前可以先 `sampan stop`（所有閒置的 agent 正常結束；忙碌中的不動，會告訴你）。重開機後打 `sampan`，按 `R`，確認勾選的，就一個個接回原本的對話。sampan **不會自己接回**，一定要你按。

**在 tmux 外的終端機（任何終端機程式）開了好幾個 Claude，想統一管理**：打 `sampan`，按 `A`。

**agent 一啟動就問問題**（Claude 的「信任這個資料夾？」、Codex 的更新、信任目錄、hooks 審查）：`n` 開新的、在已停止的那一列按 Enter 接回一個時，sampan 送出啟動指令就直接帶你進 tmux，問題在那裡自己回答。`R` 一次接回好幾個、`m` 搬目錄時，sampan 會把問題和選項顯示在訊息區，游標預設在「進去自己操作」。挑一個選項（↑↓ 加 Enter，或直接按編號），sampan 只把你選的那個送過去，接著問下一個；選「進去自己操作」就進到那個 session 自己處理；Esc「先不管」，最後的結果會列出停在提示的 session。sampan 不會替你選。`sampan new`、`sampan restore` 在終端機裡直接執行也會用編號問一樣的問題（非互動的腳本、排程則完全不看、不按）。

**在 tmux 裡要回到清單**：detach（預設 `Ctrl-b d`）就會回到 sampan 的清單；按 `q` 才離開 sampan。也可以在 tmux 裡開一個專門放 sampan 的 session 或 pane：在那裡選了之後會切過去，sampan 留在原處，切回來看到的還是清單。

**不想用全螢幕畫面**（例如在腳本或很窄的終端機）：`sampan ui --plain` 用輸入編號，打 `/文字` 可搜尋（只有一個符合就直接進去，多個會列出編號讓你挑）；每個動作也都有獨立指令，見下面「指令一覽」。

## 指令一覽

每個動作也都有獨立指令，適合腳本或想直接打指令的時候。

| 情境 | 指令 |
|---|---|
| 看狀況 | `sampan status`、`sampan doctor`（第一行是 sampan 版本與最新版） |
| 更新 sampan | `sampan update [--yes] [--force]`：當下查最新版（不看快取），有新版就問要不要更新；`--force` 已是最新也重新安裝；`--yes` 不問。不需要設定檔，更新完直接結束 |
| 互動畫面 | `sampan`（等於 `sampan ui`）：見上面「怎麼用」。tmux 裡選了會切過去、sampan 留在原 pane；tmux 外會在前景 attach，detach 後回到清單。`sampan ui --plain` 改用輸入編號，規則相同 |
| 開新的工作視窗 | `sampan new <名稱> <claude:profile｜profile｜codex> <目錄>`（名稱不要用 `.` 或 `:`） |
| 以某個帳號開 claude／開 codex | `sampan claude [profile] [claude 參數…]`（不帶 profile 會列選單；第一個字不是 profile 也不是 `-` 開頭會報錯，不會當成提示詞送出）、`sampan codex` |
| 存下目前每個 session 的對話 id | `sampan save`（排程每個整點也會跑） |
| 讓 agent 正常結束（先收起來，要用時再 restore） | `sampan stop [名稱…] [--profile <p>] [--force] [--keep-tmux]`：結束後若那個 tmux session 只剩這一個 pane，就一起關掉；有其他視窗或 pane 則保留 |
| 重開機後接回 | `sampan restore [名稱…] [--all] [--dry-run]` |
| 接手在 tmux 外（例如終端機程式的分頁）跑的 claude／codex | `sampan adopt [--dry-run]`（或 ui 按 A）：列出 tmux 外的 claude／codex（目錄、最後活動、最後一句話），勾選後逐一問 session 名稱；你自己在原視窗 `/exit`（Codex：`/quit`），sampan 等它結束後才在新的 tmux session 接回同一段對話並登記。sampan 不對原視窗按任何鍵；判斷不出對話 id 或 profile 的不能選 |
| 改 session 名稱 | `sampan rename <名稱> <新名稱>`（或 ui 按 e）：tmux session 與登記表一起改，agent 照常執行、不送任何按鍵。名稱不能含 `.` 或 `:` |
| session 換到別的工作目錄 | `sampan move <名稱> <目錄>`（或 ui 按 m）：結束 agent、在新目錄接回同一段對話、自動信任新目錄（只在提示顯示的路徑就是新目錄時）、更新登記表。目錄不存在會建立；忙碌中、worktree、自己的 pane 不動 |
| 某個 session 以後不用重開 | `sampan retire <名稱>`（恢復：`sampan activate <名稱>`） |
| 看各 profile 的 plugin 版本 | `sampan plugins status` |
| plugin 有新版／拿掉 plugin | 改 `config.toml` 後 `sampan plugins sync`；`sampan plugins sync --prune --dry-run` 先看要移除什麼 |
| 新增 Claude 帳號 | `sampan profile add <名稱>`（建立 `~/.claude-profiles/<名稱>` 並登入）；`--dir <既有目錄>` 用現成目錄（不建、不登入） |

多數會動到 session 的指令都有 `--dry-run`。plugin 換版後，要把該 profile 的 Claude session 整個重開才會生效（stop 後再 restore）。

## restore 的規則

`sampan restore` 是整個工具裡最保守的一步：

- **不會自動執行**：沒有任何排程或 hook 會呼叫它，只有你手動下指令。
- **逐一確認**：先列出可接回的 session，預設勾選的是「active 而且最近 `recent_days` 天內有動過」的；retired 與太久沒動的不勾。`--all` 勾選全部可接回的，仍會先確認。
- **一個一個接**，每個都用原對話：`claude --resume <id>`、`codex resume <id>`。
- **不送任何訊息**，也不在提示畫面替你按任何東西：停在啟動提示時，只有在 `sampan` 畫面或互動終端機裡由你選了選項，才送那一個選項（見「常見情境」）；信任提示只有 `sampan move` 在路徑完全相等時才會自動按。沿用既有的 shell pane 時，只會先送一個 `C-u` 清掉那一行還沒送出的輸入，再打接回指令。
- 確認時直接按 Enter（或輸入 `y`）接回預設勾選的；輸入編號改選；`q` 取消。
- **找不到原對話就跳過**，不會改開一個新對話頂替。
- tmux server 還沒起來也可以：restore 會自己建 session；登記表裡的名稱已經有 shell pane 就沿用。

## Codex 注意事項

- Codex 啟動時可能出現更新提示（「Update now」）或「Hooks need review」。sampan 不替你回答，會把它們轉到 sampan 的畫面讓你選（見「常見情境」）；非互動執行時就停在那裡等你。要減少打擾：更新提示選「Skip until next version」（不要選 Update now，它會直接跑 npm 安裝），hooks 審查處理一次（信任或不信任）；關閉啟動時檢查更新的選項請看 Codex 的設定文件（`~/.codex/config.toml`），本文件不替它指定設定名稱。
- Codex 的信任提示會依 pane 寬度截斷長路徑；路徑看不完整時 `sampan move` 不會自動信任（不猜），改成問你或請你到視窗確認。把視窗拉寬再試。
- Codex 的 session 要等你送出第一個提示後才有 thread id。`sampan new … codex` 逾時沒認出對話時會提醒；之後 `sampan save`（或排程）才會登記。還沒有 thread id 的 Codex 無法接回。
- Codex 的對話是靠 `lsof` 和 Codex 的本機檔案找出來的；沒有 `lsof` 時，對話會被回報為未知（Linux 上會退回 Codex 自己的資料庫，辨識率較低）。
