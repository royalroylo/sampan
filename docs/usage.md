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
| 跳到某一列 | `1`–`9`（數字是畫面上第幾個 session 列；資料夾列、收合起來看不到的不編號） | 游標跳到那一列；10 以上在 1 秒內連打兩位數，例如 `1`、`2` 是第 12 列 |
| 依名稱搜尋 | `/`，再打文字 | 不分大小寫，名稱含有該文字的列留下來；Enter 開啟游標那一列，Esc 取消，空的搜尋框按 Backspace 離開搜尋。搜尋時數字當文字用，不會跳列。搜尋不管收合，資料夾裡的會在名稱前面用灰字標出資料夾路徑（`客戶/甲 · 名稱`）；結束搜尋時游標所在的 session 若在收合的資料夾裡，會自動展開讓它看得到 |
| 依狀態篩選 | `Tab` 往後、`Shift+Tab` 往前，在「全部、閒置、忙碌、已停止、異常」之間切換 | 只列出那一類：閒置 = 執行中且 idle（就是 `S` 會重開的）；忙碌 = 執行中但不是 idle（回答中、等核准或選單、判斷不出來）；已停止；異常 = profile／agent 不符、未登記、沒有 agent、多個 agent、無法判斷、等待信任確認。retired 只在「全部」。標題列顯示目前的篩選和筆數（`sampan … › 閒置（12）`）。資料夾只列裡面符合的 session，括號裡的數字也只算符合的，一個都沒有的資料夾不顯示；「Codex 其他對話」只在「全部」出現。可以再用 `/` 搜尋（在篩選結果裡找），數字跳列只數畫面上的列。篩選不會記住，重開 sampan 回到「全部」。勾選只留在畫面上看得到的 session，資料夾列上的 `s`／`S` 也只動符合篩選的 session |
| 全選 | `*` | 勾選畫面上看得到的每個 session 列（收合資料夾裡的、資料夾列本身不勾）；都已勾選時再按一次就全部取消。典型用法：只重開開著的 → `Tab` 到「閒置」、`*`、`S`、`y`；把停掉的全部接回 → `Tab` 到「已停止」、`*`、`S`、`y`（要勾 session 列；在資料夾列上按 `S` 照舊不接回已停止的）。篩選在做完動作回到清單後照舊。勾選的 session 因為狀態改變而離開篩選時勾選會取消，下一個 `s`／`S`／`g`／`f` 只提示「請重新勾選」，不會改去動游標那一列 |
| 開一個新的工作 | `n` | 問名稱、agent／profile，再用選目錄畫面挑工作目錄（從 `[ui] start_dir` 開始；沒設時用 `~/Projects`（存在的話），否則家目錄），開好後直接進去 |
| 暫時收起來（省記憶體、清畫面） | `s`（空白鍵可勾多個） | 在畫面底部問確定；agent 正常結束，tmux 也一起關掉。對話保留，之後 Enter 或 `R` 接回 |
| 重開（停掉再接回同一段對話，不進去） | `S`（空白鍵可勾多個；在資料夾列上是底下全部） | 畫面底部列出會重開的和略過的（`將重開：A、B　略過：C（忙）　確定？(y/N)`），按 y 開始。閒著的正常結束後在背景接回；正在回答中的、sampan 自己所在的 pane、retired 的略過並在最後列出原因。已停止的只有在它那一列（或勾了它）按 `S` 時才在背景接回，只經由資料夾列選到的已停止 session 維持停止。確認行太長時名單會縮短成前幾個加「等 n 個」，`確定？(y/N)` 一定看得到。記在 `actions.log`（`[重開]`） |
| 整理成資料夾 | `g` 包成新資料夾、`f` 搬進資料夾；資料夾列上 Enter 收合／展開、`e` 改名、`X` 刪除 | 只改 `folders.json`，不動任何 session，見下面「資料夾」 |
| 一次接回好幾個 | `R` | 勾選清單（預設勾最近 7 天有動過的），接回後進到第一個 |
| 把 tmux 外的終端機（任何終端機程式）裡的 Claude 收進來 | `A` | 勾選要接手的，逐一取名；你自己在原本那個終端機分頁打 `/exit`，sampan 等它結束後在 tmux 接回同一段對話，然後進去 |
| 改名 | `e` | tmux 與登記表一起改，agent 照常跑 |
| 換帳號繼續（例如額度用完） | `P`（只有 Claude） | 列出 profile 讓你選，確認後 claude 結束、用新的 profile 在同一個目錄接回同一段對話；新帳號第一次進這個目錄時自動信任（規則同 `m`）。已停止的 session 直接用新 profile 接回。新 profile 沒裝原本有的 plugin 時會先列出來，但照樣可以換。卡在額度選單、等不到 claude 結束時，sampan 不亂按鍵，會請你自己按 Esc 再打 `/exit`，之後再按 `P` 一次 |
| 換工作目錄 | `m` | 用選目錄畫面挑新目錄（從它現在的目錄開始；那個目錄已經不在時，從 `[ui] start_dir` 開始，和 `n` 一樣），確認後 agent 結束、在新目錄接回同一段對話 |
| 這件事做完了 | `r` | 標成 retired，之後不會出現在接回清單 |
| 從登記表移除一個不用的 session | `X`（只有已停止或 retired 的列） | 在畫面底部問確定（之後不會再列出，也不會被 `R` 接回）；移除後印出手動接回的指令。對話紀錄不會刪，舊的登記表留在 `history/` |
| 看各帳號的額度、改名、停用、新增、裝狀態列 | `o` | 開帳號畫面，見下面「帳號管理」。清單上方會有每個帳號一行（`名稱 N 個執行中・5h …・週 …（時間）`） |
| 看派工、取消還沒送出的 | `D` | 列出派工（編號、從、給、狀態、建立時間、原因，新的在上）；游標移到排隊中的按 `x`，在畫面底部問確定後取消；已送出、等回覆的按 `x` 問確定後改成不再等回覆（訊息收不回）；已結束的只會說明為什麼不能取消。沒有派工時只顯示「沒有派工」。有排隊或等回覆的派工時，清單下方會多一行 `派工：N 排隊、M 等回覆（D 看清單）`；畫面開著時 sampan 每 3 秒在背景推進這些派工（送出、讀回覆、補發完成或失敗通知）；離開畫面時若正好在送，會等它送完（最多約 35 秒，畫面會寫「等派工送完再離開…」） |
| 管理 plugin 與 marketplace 來源 | `p` | 進到 plugin 管理畫面，見下面「plugin 管理畫面」 |
| 立刻存一次登記表 | `w` | 平常每 5 分鐘會自動存（畫面底部的按鍵說明放不下就沒列，照樣能用；數字跳列、↑↓、`Enter`、`R`、`A` 也一樣） |
| 更新 sampan | `U`（有新版時按鍵說明上面會出現「有新版 v…（目前 …），按 U 更新」） | 在畫面底部問確定，跑 `uv tool upgrade sampan`（版本沒變再 `uv tool install --reinstall git+https://github.com/royalroylo/sampan@v<最新版>`），輸出顯示在訊息區；成功後自動重新開啟 sampan 回到清單，失敗就留在清單。不知道有新版時，`U` 會先當下重新查一次（不看快取）；仍是最新版（或查不到）只會告知，不會問；要重新安裝請用 `sampan update --force` |

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

**新版提示**：`sampan` 開啟時在背景查 GitHub 上最新的版本 tag（結果快取 6 小時，畫面開著時過期會再查；沒網路就不提示），有新版才顯示提示、按 `U` 更新。`--plain` 只看快取、在開頭印一行；每 5 分鐘的自動存檔不檢查。

## plugin 管理畫面

在清單按 `p` 進來（`--plain` 也有，用輸入編號）。這裡管的是各 profile 的 plugin 與 marketplace 來源；改的內容會一起寫回 `config.toml`，所以設定檔與實際安裝不會分家。**打開畫面不會連網**；只有按 `r`、`u`、`U`，或確認儲存時才會執行 `claude` 指令（連到 GitHub 等來源）。

**兩層**：第一層是 plugin 清單（名稱、狀態：最新／有新版／未裝／不一致），右邊（窄螢幕在下面）是資訊卡，列出來源、各 profile 目前的版本與狀態。游標那個 profile 不能改時，資訊卡會寫原因（例如「目錄不存在」、「來源不同：…」）。按 Enter 進到第二層：每個 profile 一個勾選框，空白勾選或取消，勾完按 `s` 儲存，`q` 或 `Esc` 返回（有改動時會問「放棄變更？(y/N)」）。

| 鍵 | 作用 |
|---|---|
| ↑↓、`1`–`9`、`/` | 移動、跳到那一列、依名稱搜尋 |
| Enter | 進到第二層，選要裝在哪些 profile |
| `r` | 檢查新版：查各來源最新的版本（會連網），之後狀態會標「有新版」 |
| `u` | 更新游標那個 plugin，不另外問：先更新它的 marketplace，再在每個有裝它的 profile 更新；不會新裝或移除任何東西 |
| `U` | 全部 plugin、全部 profile 都更新；因為會動到每個 profile，先問「全部 plugin、全部 profile 都更新？(y/N)」 |
| `a` | 新增 plugin：從已下載的 marketplace 目錄挑，或按 `i` 自己輸入 `plugin@marketplace` |
| Tab | 在「plugin」與「來源」兩個分頁間切換（`--plain` 用 `t`） |
| `q` | 返回清單 |

**儲存**：在第二層按 `s` 會先列出「將會：」要做的每一件事，每一行都寫明 profile（例如 `+ 安裝到 work（…）`、`− 從 work 移除`），再問「確定？(y/N)」，按 `y` 才執行。安裝與移除用 `--scope user`，只動 user scope。

- **設定檔備份**：只有設定檔真的被改寫時，才先備份成 `config.toml.bak-<時間>`（同一次儲存最多一份）；全部失敗就沒有備份。設定檔其他部分（註解、排版）一個位元組都不動。儲存時會重新讀磁碟上的設定檔，所以你在畫面開著時手動改的內容不會被蓋掉。
- **部分失敗**：某個 profile 的指令失敗，其他 profile 照常完成；設定檔只改指令成功的那幾個 profile，失敗的會列出來（「… 沒有改（設定檔也沒動那幾個 profile）」）。
- **重開才生效**：安裝或移除成功後（在來源分頁移除來源而順便解除安裝了 plugin，也一樣），畫面會說哪些 profile 有幾個 session 要重開（「要生效需重開 …：按 R 立刻重開，其他鍵稍後」）。按 `R` 才會重開（stop 後 restore）；不按就不會動，畫面會提醒「稍後要生效：sampan stop --profile <p> 後 sampan restore」。sampan 自己所在的 pane、忙碌中的 session 絕不重開，會列在「這些還在跑舊版，處理後再重開：名稱（原因）…」，稍後用 `sampan stop --profile <p>` 加 `sampan restore`。只更新設定檔清單、沒有真的安裝或移除時，不需要重開。worktree 裡的 session 一樣會送 `/exit`；如果 Claude 接著問要不要保留 worktree（只有那次執行是它自己建立 worktree 時才問），sampan 不替你選，原因會寫「在問要不要保留 worktree」，請進去選「Keep worktree」再 `sampan restore`。

**來源分頁**（Tab）：列出 marketplace 來源，每個來源有一個狀態：

| 狀態 | 意思 |
|---|---|
| 一致 | 設定檔與各 profile 裝的來源相同 |
| 來源不同 | 同名 marketplace 在某個 profile 指向別的來源；那個 profile 的框不能勾，資訊卡寫出對方的來源 |
| 清單外 | profile 裡有，但設定檔 `[marketplaces]` 沒有；只能移除 |
| 沒用到 | 設定檔有，但沒有 plugin 用它 |

在這個分頁按 `a` 新增來源（問 `owner/repo` 或 git 網址），第二層勾選要加到哪些 profile，儲存時一併寫進 `[marketplaces]`。`u` 對游標那個來源，在每個有它的 profile 跑 `marketplace update`；`U` 對全部來源（先問）。從某個 profile 移除來源時，如果那個 profile 的設定檔清單還列著用它的 plugin，會被拒絕（資訊卡寫「清單還有 N 個 plugin 用它（先在 plugin 頁移掉）」），要先到 plugin 頁移掉那些 plugin，否則一次儲存會悄悄把它們解除安裝。

**為什麼網址裡不能放 token**：sampan 會把來源寫進設定檔，也會原樣傳給 `claude`（指令列與日誌都看得到）。帶帳號或 token 的網址會被拒絕（「網址裡不要放帳號或 token，認證交給 git 的 credential helper」）。私有的 repo 請先設好 git 的 credential helper，來源寫乾淨的網址即可。

**操作紀錄**：畫面上的訊息區只留最後一次操作的結果。真正執行了的操作，它印出的訊息都記在狀態目錄的 `actions.log`（預設 `~/.local/state/sampan/actions.log`，`SAMPAN_STATE_DIR` 可改；只有你讀得到），每行開頭是時間和操作名稱，例如 `[plugin 存檔 …]`、`[重開]`、`[接回]`、`[接手]`、`[new …]`、`[stop]`、`[move …]`、`[來源 …]`、`[forget …]`、`[switch …]`、`[資料夾]`。儲存、`u`／`U`、`r`、來源的新增移除與更新、`R` 重開的每一步（包括「還在跑舊版」的名單）、`X`／`sampan forget`、`P`／`sampan switch`、`R`／`sampan restore`／在已停止的那一列按 Enter 接回（先記選了哪些）、`A`／`sampan adopt` 接手、`n`／`sampan new`、`s`／`sampan stop`、`m`／`sampan move` 都會記，這些操作途中遇到啟動提示（信任目錄等）時你選了什麼也會記（一個操作裡帶著另一個時，例如重開裡的停止與接回，記在外層的名稱下）；`--dry-run` 不記；`sampan doctor` 會列出檔案位置和最後一筆的時間；預覽、「確定？」和取消的操作不記。網址裡的帳號、GitHub token 和 `…TOKEN=` 這類寫法會先遮成 `***`。檔案超過 1 MB 時，舊的會改名成 `actions.log.1`。

腳本或排程不要用這個畫面，繼續用 `sampan plugins sync`（見「指令一覽」）。

## 帳號管理

一個 profile 就是一個帳號（Claude 的設定目錄，或 Codex 的 `CODEX_HOME`）。清單最上方每個使用中的帳號一行：`名稱 N 個執行中・5h 42%・週 18%（17:20）`，括號裡是那筆額度讀到的時間；最多每 60 秒更新一次。額度的來源：Claude 要先裝狀態列（見下面）才有資料；Codex 讀它最新一份 rollout 檔裡的紀錄。sampan 不讀帳號憑證、也不為了額度連網。

**額度警告**：任一項用到 80% 起，帳號行改成 `⚠ 週 85%（10/27 09:10 重置）・5h 40%`（最緊的一項排最前面，括號是重置時間）；用完是 `⛔ 5h 已用完（08:28 重置）・週 73%`。

**Codex 常駐服務**：Codex 的額度、session 狀態與對話清單，在 Codex 常駐服務（app-server，用 `codex remote-control start` 啟動）有跑時直接問它，唯讀、單次最多等 3 秒，每 30 秒一次，額度也存起來供之後使用；沒在跑時額度退回讀最新的 rollout 檔，`sampan doctor` 會提示「Codex 常駐服務沒在執行」，主畫面原本放「Codex 其他對話」的位置改成一行「Codex 常駐服務沒在執行（codex remote-control start）」。打開 `sampan` 畫面時若常駐服務沒在執行，會在背景自動跑一次 `codex remote-control start`（`[ui] codex_auto_start = false` 關掉），畫面顯示「正在啟動 Codex 常駐服務…」或失敗原因（「找不到 codex 指令」只在清單裡有 Codex session 時才顯示）；每次開畫面只試一次，只在 `sampan` 畫面，`status`、`doctor`、`restore` 不會啟動。有常駐服務時：
- tmux 裡的 Codex session 的狀態欄用它的即時狀態（等核准與等輸入都顯示 `waiting`，另有 `working`、`idle`、`error`）。
- 主畫面下方多一段「Codex 其他對話（24 小時內）」，狀態用等你核准、等你輸入、錯誤、執行中、閒置、未開啟：常駐服務知道、但不是 tmux session 的對話（多半在 Codex Desktop 開的），最多 10 列，只能看，不能進入或操作（按 Enter 只提示「到 Codex Desktop 操作」）。

Codex session 的 context 用量（超過模型上限 80% 標 ⚠）讀自 rollout 檔，不需要常駐服務。

按 `o` 開帳號畫面：

| 鍵 | 作用 |
|---|---|
| ↑↓ | 移動 |
| `r` | 改名（只改設定檔與登記表，目錄不動；該帳號有 session 在跑時拒絕） |
| `d` | 停用／恢復。停用會問 y/N |
| `f` | forget：把已停用的帳號從設定檔拿掉，連同登記表裡它的 session |
| `i` | 幫游標那個 Claude 帳號裝狀態列（已接上時不動作；移除用 `sampan statusline uninstall`） |
| `a` | 新增帳號 |
| `q` | 返回清單 |

**指令**（`--plain` 與腳本用）：

| 想做的事 | 指令 |
|---|---|
| 列出所有 profile | `sampan profile list`（類型、目錄、帳號、使用中／停用、執行中的 session 數） |
| 帳號管理 | `sampan profile list｜add｜rename｜disable｜enable｜forget`（`add <名稱> [--dir D] [--codex]`），見「帳號管理」；ui 裡按 `o` |
| 讓 sampan 看到 Claude 額度 | `sampan statusline install｜uninstall [--profile P] [--yes]`，見「帳號管理」 |
| 新增 Codex 帳號 | `sampan profile add <名稱> --codex`（建立 `~/.codex-profiles/<名稱>` 並用 `codex login` 登入）；`--dir` 同上 |
| 改名 | `sampan profile rename <舊> <新>` |
| 停用／恢復 | `sampan profile disable <名稱>`、`sampan profile enable <名稱>` |
| 從設定檔拿掉 | `sampan profile forget <名稱>`（只有已停用的；同時移除登記表裡它的 session） |

**停用**：停用的帳號不出現在清單與選單、不能開新 session、`sampan plugins sync` 跳過它；`sampan restore` 遇到屬於它的列不會接回，並說明原因（`sampan profile enable` 後就可以）。預設的 profile 不能停用。有 session 在跑的 profile 不能改名、停用或 forget，先 `sampan stop`。

**sampan 從不刪目錄。** forget 只是把帳號從設定檔和登記表拿掉，目錄與裡面的對話都還在，並印出手動接回的指令。要真的刪掉一個帳號的目錄，自己來，順序是：

1. `sampan profile disable <名稱>`，再 `sampan profile forget <名稱>`。
2. 看目錄裡有沒有連到別處的捷徑（`projects/`、`commands/` 常是 symlink，指向其他 profile 共用的資料）：`find <目錄> -maxdepth 1 -type l -ls`。
3. 只刪目錄裡面的東西；對 symlink 只刪捷徑本身，不要跟進去刪它指向的地方。確認無誤後才刪目錄。

**設定檔裡的樣子**：見[設定檔格式](reference.md)。`[profiles.<名稱>]` 多了 `agent = "codex"` 和 `disabled = true` 兩個鍵。舊的 `[codex]` 設定在記憶體裡會被當成一個隱含的 Codex profile；第一次執行 `sampan profile rename`／`disable`／`enable`／`forget`／`add --codex`（或在 `o` 帳號畫面做同樣的事）時，才轉成 `[profiles.<名稱>]` 寫進去（訊息會印出它的目錄）；整個 `[codex]` 表會被移除，自訂的 `[codex] home` 成為新 profile 的 `config_dir`。新增 Claude profile（`sampan profile add` 不加 `--codex`）和 `sampan init` 不會轉換。設定檔裡只要有任何 `agent = "codex"` 的 profile，`[codex]` 就整個不再作用（`add --codex` 會順手拿掉留下的 `[codex]`）。`[codex] enabled = false` 表示不用 Codex（沒有 Codex profile）；forget 掉最後一個 Codex profile 時 sampan 會寫入這一行。

**Codex 多帳號還不支援**：目前不能替新 session 挑 Codex 帳號，也不能在 Codex 帳號之間切換；sampan 用第一個使用中的 Codex profile。有不只一個 Codex profile 時，所有執行中的 Codex session 都算在第一個使用中的 Codex profile 名下（登記表、帳號行的執行中數字都是）。另外 Claude 與 Codex 的對話格式不同，不能互相切換（`P` 只在 Claude 帳號之間換）。

### 狀態列（讓 sampan 知道 Claude 的額度）

Claude Code 只在狀態列指令的輸入裡提供 5h／週額度，所以要讓 sampan 看到額度，要把它接在狀態列上：

- `sampan statusline install [--profile P] [--yes]`：對每個使用中的 Claude profile（或只指定的）改它的 `settings.json` 的 `statusLine`。你原本已有狀態列指令時，會包成 `<sampan> statusline -- sh -c '<原指令>'`，原指令的輸出照舊顯示；沒有時 sampan 自己印一行（模型・目錄・context・額度）。改之前先列出會怎麼改並確認（`--yes` 不問），並備份成 `settings.json.bak-<時間>`，只動 `statusLine` 這個鍵；`settings.json` 不是合法的 JSON 物件時拒絕。`sampan init` 也會問要不要裝。
- `sampan statusline uninstall [--profile P] [--yes]`：還原成原指令（原本沒有就拿掉這個鍵）。
- 原指令是在 `sh` 下執行的（Linux 上通常是 dash，不是 bash），所以 bash 才有的寫法（`[[ ]]`、陣列、`<(…)` 等）要自己改成 `bash -c '…'`。
- **專案層的 `.claude/settings.json`／`settings.local.json` 若有 `statusLine`，會蓋過使用者層的設定**，那個專案裡的 session 因此不經過 sampan，不會記下額度。要在該專案也記，得自己把專案層的指令同樣包起來，或拿掉它。
- 額度記在狀態目錄的 `usage/<profile>.json`（只有你讀得到）。`sampan doctor` 會回報每個 Claude profile 的狀態列有沒有接上、最近一筆額度。

## 常見情境

**每天開工**：打 `sampan`，Enter 進到要做的那個。要做新的事就按 `n`。

**做到一半想先收起來**：在清單上按 `s`。之後要繼續，移到那一列按 Enter，會接回同一段對話並進去。

**關機或重開機**：關機前可以先 `sampan stop`（所有閒置的 agent 正常結束；忙碌中的不動，會告訴你）。重開機後打 `sampan`，按 `R`，確認勾選的，就一個個接回原本的對話。sampan **不會自己接回**，一定要你按。macOS 上，Claude 的登入資訊可能同時有兩份：鑰匙圈一份、`.credentials.json` 一份。Claude 先用鑰匙圈那份（帳號是你的使用者名稱），鑰匙圈打不開時才用檔案那份，所以其中一份會變舊；用到舊的那份時，那個 profile 就會變成未登入（Remote Control 也連不上）。`sampan doctor` 看到同一個 profile 有兩份時會提醒：檔案比較新時出 ⚠，處理方法是在那個 profile 執行 `/login`（例如 `sampan claude <profile>`），鑰匙圈那份變成比較新的，就降成 ℹ️。鑰匙圈裡同名、但帳號不是你的那份（例如用別的使用者身分跑過 Claude 留下的），Claude 不會用，doctor 不拿它判斷，只用 ℹ️ 列出來並附上刪除指令，要不要刪由你決定。sampan 不讀登入內容，只看鑰匙圈項目的屬性和兩份的修改時間。

**在 tmux 外的終端機（任何終端機程式）開了好幾個 Claude，想統一管理**：打 `sampan`，按 `A`。

**agent 一啟動就問問題**（Claude 的「信任這個資料夾？」、Codex 的更新、信任目錄、hooks 審查）：`n` 開新的、在已停止的那一列按 Enter 接回一個時，sampan 送出啟動指令就直接帶你進 tmux，問題在那裡自己回答。`R` 一次接回好幾個、`m` 搬目錄時，sampan 會把問題和選項顯示在訊息區，游標預設在「進去自己操作」。挑一個選項（↑↓ 加 Enter，或直接按編號），sampan 只把你選的那個送過去，接著問下一個；選「進去自己操作」就進到那個 session 自己處理；Esc「先不管」，最後的結果會列出停在提示的 session。sampan 不會替你選。`sampan new`、`sampan restore` 在終端機裡直接執行也會用編號問一樣的問題（非互動的腳本、排程則完全不看、不按）。

**在 tmux 裡要回到清單**：按 `Ctrl-q`（`[ui] detach_key`，只在 sampan 管的 session 裡有效）或 detach（預設 `Ctrl-b d`）就會回到 sampan 的清單；按 `q` 才離開 sampan。也可以在 tmux 裡開一個專門放 sampan 的 session 或 pane：在那裡選了之後會切過去，sampan 留在原處，切回來看到的還是清單。

**不想用全螢幕畫面**（例如在腳本或很窄的終端機）：`sampan ui --plain` 用輸入編號，打 `/文字` 可搜尋（只有一個符合就直接進去，多個會列出編號讓你挑）；每個動作也都有獨立指令，見下面「指令一覽」。

## 指令一覽

每個動作也都有獨立指令，適合腳本或想直接打指令的時候。

| 情境 | 指令 |
|---|---|
| 看狀況 | `sampan status`、`sampan doctor`（第一行是 sampan 版本與最新版） |
| 更新 sampan | `sampan update [--yes] [--force]`：當下查最新版（不看快取），有新版就問要不要更新；`--force` 已是最新也重新安裝；`--yes` 不問。不需要設定檔，更新完直接結束 |
| 互動畫面 | `sampan`（等於 `sampan ui`）：見上面「怎麼用」。tmux 裡選了會切過去、sampan 留在原 pane；tmux 外會在前景 attach，detach 後回到清單。`sampan ui --plain` 改用輸入編號，規則相同 |
| 開新的工作視窗 | `sampan new <名稱> <claude:profile｜profile｜codex> <目錄>`（名稱不要用 `.`、`:`、換行或控制字元，也不能是「終端機」或以「未登記的 session」開頭；`rename`、`adopt` 同規則） |
| 以某個帳號開 claude／開 codex | `sampan claude [profile] [claude 參數…]`（不帶 profile 會列選單；第一個字不是 profile 也不是 `-` 開頭會報錯，不會當成提示詞送出）、`sampan codex` |
| 存下目前每個 session 的對話 id | `sampan save`（排程每 5 分鐘也會跑） |
| 讓 agent 正常結束（先收起來，要用時再 restore） | `sampan stop [名稱…] [--profile <p>] [--force] [--keep-tmux]`：結束後若那個 tmux session 只剩這一個 pane，就一起關掉；有其他視窗或 pane 則保留 |
| 重開指定的 session | `sampan restart <名稱…>`：閒置的先停再背景接回原對話（不會把你帶進去），已停止的直接接回；忙碌中的、sampan 自己所在的 pane、retired 的略過，並在最後列出名稱與原因。全部都不能重開時回傳 1 |
| 重開機後接回 | `sampan restore [名稱…] [--all] [--dry-run]` |
| 接手在 tmux 外（例如終端機程式的分頁）跑的 claude／codex | `sampan adopt [--dry-run]`（或 ui 按 A）：列出 tmux 外的 claude／codex（目錄、最後活動、最後一句話），勾選後逐一問 session 名稱；你自己在原視窗 `/exit`（Codex：`/quit`），sampan 等它結束後才在新的 tmux session 接回同一段對話並登記。sampan 不對原視窗按任何鍵；判斷不出對話 id 或 profile 的不能選（Codex：新開的要先送出第一個 prompt、剛 `codex resume` 的要先送一則訊息，sampan 才認得出是哪段對話） |
| 改 session 名稱 | `sampan rename <名稱> <新名稱>`（或 ui 按 e）：tmux session 與登記表一起改，agent 照常執行、不送任何按鍵。名稱不能含 `.` 或 `:` |
| session 換 profile（換帳號）繼續同一段對話 | `sampan switch <名稱> <profile> [--dry-run]`（或 ui 按 P）：執行中的照 `sampan move` 的流程在原目錄、用新 profile 接回；已停止的改登記表後直接接回。逐字稿在新 profile 讀不到時（各 profile 的 `projects/` 沒共用）會**搬**過去而不是複製，永遠只有一份；新 profile 已經有同一段對話的逐字稿時不動，請你先決定留哪份。Codex 沒有 profile，不適用 |
| session 換到別的工作目錄 | `sampan move <名稱> <目錄>`（或 ui 按 m）：結束 agent、在新目錄接回同一段對話、自動信任新目錄（只在提示顯示的路徑就是新目錄時）、更新登記表。目錄不存在會建立；忙碌中、worktree、自己的 pane 不動 |
| 某個 session 以後不用重開 | `sampan retire <名稱>`（恢復：`sampan activate <名稱>`）；對 retired 的 session 打 `sampan restore <名稱>` 會說「<名稱> 是 retired；先 sampan activate <名稱>」 |
| 把沒在跑的 session 從登記表移除 | `sampan forget <名稱>… [--dry-run]`（或 ui 按 X；可一次給多個名稱，逐一處理，有一個失敗其他照做）：只限已停止或 retired 的（還在 tmux 裡的先 `sampan stop`）；ui 裡會先問確定。移除後印出手動接回的指令；對話紀錄永遠不刪，移除前的登記表留在 `history/` |
| 整理 session 的資料夾 | `sampan folder list｜add <路徑>｜rename <路徑> <新名稱>｜rm <路徑>｜move <名稱或路徑>... <目的｜/>`，見「資料夾」 |
| 傳訊息給一個 session | `sampan send <名稱> <訊息｜-> [--wait] [--timeout 秒]`，見「傳訊息給 session」 |
| 看／取消派工 | `sampan dispatches [--all]`、`sampan dispatches show <編號> [--wait 秒]`、`sampan dispatches cancel <編號>` |
| 讓 agent 用工具傳訊息 | `sampan mcp serve`（MCP server，由 agent 啟動），見「讓 agent 用 sampan」 |
| 看各 profile 的 plugin 版本 | `sampan plugins status`（互動管理在 ui 按 `p`） |
| plugin 有新版／拿掉 plugin | 改 `config.toml` 後 `sampan plugins sync`（給腳本用，仍然可用）；`sampan plugins sync --prune --dry-run` 先看要移除什麼 |
| 帳號管理 | `sampan profile list｜add｜rename｜disable｜enable｜forget`（`add <名稱> [--dir D] [--codex]`），見「帳號管理」；ui 裡按 `o` |
| 讓 sampan 看到 Claude 額度 | `sampan statusline install｜uninstall [--profile P] [--yes]`，見「帳號管理」 |

多數會動到 session 的指令都有 `--dry-run`。plugin 換版後，要把該 profile 的 Claude session 整個重開才會生效（stop 後再 restore）。

## 資料夾

session 可以放進資料夾（多層用 `/`，例如 `客戶/甲`）。資料夾只是整理顯示用：建、改名、刪、搬資料夾都不會停掉、忘記或改動任何 session，也不動登記表；資料放在狀態資料夾的 `folders.json`（格式見 reference）。

**在 `sampan` 畫面上**：每一層先列子資料夾、再列 session，都依名稱排；沒放進資料夾的 session 排在最後，「Codex 其他對話」照舊在最下面。資料夾列長這樣：`▾ 客戶 (12)`（括號是底下各層 session 的總數），收合時是 `▸`，裡面有正在回答中的會加 `　2 忙`；session 依層數縮排。游標在資料夾列時，右邊的資訊卡是路徑、session 數、各狀態的數量和能按的鍵，沒有預覽。

| 鍵 | 在 session 列 | 在資料夾列 |
|---|---|---|
| Enter | 照舊：進去（已停止的先接回） | 收合／展開（記在 `folders.json`） |
| → ／ ← | ← 跳到它所在的資料夾列 | 展開／收合；已收合時 ← 跳到上一層的資料夾列 |
| 空白鍵 | 勾選 | 勾選資料夾 |
| `g` | 把勾的 session 和資料夾（沒勾就是游標那一列）包進一個新資料夾：問名稱，建在它們最深的共同上層 | 同左 |
| `f` | 把勾的（或游標那一列）搬進資料夾：從清單挑，最上面是「（最外層）」和「新資料夾…」 | 同左（整個資料夾一起搬，清單裡沒有它自己和它的子層） |
| `e` | 照舊：改 session 名稱 | 改資料夾名稱 |
| `X` | 照舊：從登記表移除 | 刪除資料夾：問一次 y，裡面的子資料夾和 session 往上移一層，不會停掉 |
| `s`、`S` | 照舊：停、重開 | 停、重開底下所有 session（包括收合起來看不到的；確認行會全部列出） |
| `n` | 新 session 放進游標所在的資料夾 | 新 session 放進這個資料夾 |

- 名稱不能空白、不能有 `/`；同一層已經有同名的時候會問「同一層已經有「X」，合併進去？(y/N)」。
- `g`、`f`、`e`、`X` 做完標記就清掉；每一次都記在 `actions.log`（`[資料夾]`）。
- `folders.json` 壞掉（讀不了、不是 JSON、版本不認得）時清單照樣平坦列出全部 session，畫面底部提示一次「資料夾設定讀不到：…，先照原樣列出」；這時資料夾按鍵只回「資料夾設定壞了，修好或刪掉 folders.json 再試」，不會覆寫壞檔。
- `sampan ui --plain` 照樹的順序列出，名稱前面加資料夾路徑，不能收合；`S` 一樣先問 y。資料夾的整理請用下面的 `sampan folder …`。

**終端機指令**：

- `sampan folder list`：印出樹，資料夾 `▾ 名稱/`（`▸` 是收合的），session 縮排在下面；不在任何資料夾的 session 在最外層。
- `sampan folder add 客戶/甲`：建資料夾，缺的上層一起建。
- `sampan folder rename 客戶/甲 乙 [--merge]`：只改最後一段；同層已有同名時要加 `--merge` 才合併。
- `sampan folder rm 客戶`：刪掉資料夾，裡面的 session 與子資料夾搬上一層（不問，也不會動到任何 session）。
- `sampan folder move s1 客戶/甲 開發 [--merge]`：最後一個是目的（`/` 是最外層），前面的可以是 session 名稱或資料夾路徑；不能把資料夾搬進它自己裡面。
- `sampan new <名稱> <目標> <目錄> --folder 開發`：開成功才放進資料夾（沒有就建）；Codex 已經開好、只是還沒登記（結束碼 1）也算開成功。
- `sampan rename`、`sampan forget`、`sampan profile forget` 會一併更新資料夾裡的名稱；`folders.json` 壞掉時這些指令照常完成，只多一行警告。`folders.json` 壞掉時資料夾指令一律拒絕（結束碼 2）、不覆寫它；修好或刪掉再試。

## 傳訊息給 session

`sampan send <名稱> <訊息>` 把一則訊息交給一個執行中的 session（訊息寫 `-` 就從 stdin 讀，多行、引號、`$` 都照原樣）。sampan 先建一筆「派工」，對方開頭會先看到一行來源：`〔sampan 轉送｜來自 終端機｜派工 7f3a｜直接回覆文字即可，sampan 會帶回〕`（提醒對方用文字回覆，sampan 會把回覆帶回來）。agent 用它的 shell 工具跑 `sampan send` 時，來源寫那個 session（或「未登記的 session」），不會冒充「終端機」；送給自己照樣拒絕。

- 只送給 sampan 登記表裡、狀態是執行中的 session；找不到、沒在執行、送給自己都會拒絕（結束碼 2），不猜。
- **Claude**：對方閒著（`sampan status` 的活動是 idle）**而且輸入框是空的**，才用 tmux 貼上整則訊息再按 Enter。對方在忙、在等你回答、畫面停在選單或提示、輸入框裡有人打了一半的字（「對方輸入框有未送出的字」）、畫面在 tmux 捲動模式（「對方畫面在捲動模式」）→ 不送、繼續排隊，不會清掉別人的草稿。輸入框裡 Claude 自己的灰字建議（例如剛跑過的 `/compact` 用灰字建議再打一次）算空的。按 Enter 後 sampan 會再看幾次輸入框（約 3 秒，這段時間派工清單是鎖著的）：提示行開頭還是 sampan 貼的那段字、畫面也不在捲動模式，才再按一次 Enter（不會重貼）；還是沒送出就標成失敗（「已貼進輸入框但沒按到 Enter…」），請自己去那個 session 看。畫面看不懂或已經換成別的東西時不會再按任何鍵。
- **排隊上限**：派工建立 30 分鐘後還在排隊、而且最後一次的原因是畫面用不了（選單或提示、輸入框有未送出的字、捲動模式、看不出是否閒著）或 `codex queue` 出錯，就標成失敗（「排隊 30 分鐘還送不出去（最後一次：…）」），要通知的照樣通知發派工的 session。只是在等對方（對方正在忙、在等使用者回應、沒在執行、還沒有對話編號）的沒有上限，照舊等到可以送。
- **Codex**：用 `codex queue` 交給它的對話，Codex 自己排在目前這輪之後。`codex queue` 逾時（30 秒）算「可能已送出，請確認」的失敗，不會再送一次。
- 最多送一次：按鍵或 `codex queue` 之前紀錄就先寫成已送出，sampan 送到一半被砍掉也不會重送（那筆之後會變「找不到回覆」）。
- 回覆只認**這則訊息**：在對方的逐字稿找到這則訊息，取它之後、對方回到閒著時的最後一段回答。對方中途 `/clear`、換了對話或被停掉 → 派工變「失敗」並寫明原因（例如「找不到回覆」），不拿別段文字充數。對方沒回這則就有人打了下一則（例如按 Esc 中斷後繼續打字）→「對方沒回這則就換了下一則」；這輪結束了卻沒有回覆文字 →「對方這輪結束了，但沒有回覆文字」。
- 不帶 `--wait`：試送一次，印出派工編號就結束（排隊中也是結束碼 0）；之後由 `--wait` 的指令、`sampan dispatches show`、MCP 工具或開著的 `sampan` 畫面接著推進（沒人盯時派工停在原狀態，下次有人盯再接著做）。
- `sampan dispatches show <編號> [--wait 秒]`：推進一次（帶 `--wait` 就最多等那麼久），印出狀態、原因與**完整回覆**。沒帶 `--wait` 送出的，用它看回覆。`--wait` 只能跟 `show` 一起用，其他用法會報錯。
- `--wait`：每 2 秒看一次，對方回完就把回覆印到 stdout（結束碼 0）；失敗印原因（結束碼 1）；超過 `--timeout`（預設 600 秒）印「還在做，派工 <編號>」（結束碼 124），派工照樣留著；按 Ctrl-C 也一樣留著（結束碼 130）。
- `sampan dispatches` 列排隊中與等回覆的派工（編號、從誰、給誰、狀態、建立時間、原因）；`--all` 連已回覆、失敗、取消的一起列。`sampan dispatches cancel <編號>` 取消還沒送出的；已送出、等回覆的改成不再等回覆（訊息已在對方那裡，收不回）。
- 開新 session 時一起送：`sampan new <名稱> <目標> <目錄> --message <訊息｜->`（`-` 從 stdin 讀）。訊息當作 agent 啟動時的第一個 prompt（`claude "$(cat <檔>)"`、`codex "$(cat <檔>)"`），所以資料夾信任等啟動提示照常先問，答完才送出；同時建一筆狀態直接是「已送出」的派工，之後照一般派工看回覆；新 session 還沒被認出來（還在開、停在信任提示、Codex 還沒有對話）時不判失敗，原因寫「對方 session 還在啟動」。下指令的不會收到完成通知，用 `sampan dispatches show` 看。沒帶 `--message` 時行為跟以前完全一樣。
- 送出、送達、取消都記在 `actions.log`（`[send]`、`[cancel]`），每行最後標出是哪個 sampan 寫的（`〔sampan 0.13.3，pid 12345〕`，版本是那個 process 啟動時載入的；畫面、每個 agent 的 MCP server 和指令都會推進派工，升級後還在跑舊版的也看得出來）。排程的 `sampan save` 永遠不送、不推進派工。

## 讓 agent 用 sampan

`sampan mcp serve` 是一個 stdio MCP server：agent（Claude Code、Codex）透過它的工具 `list_sessions`、`send`、`new_session`、`dispatch_result`、`cancel_dispatch`、`folder_move` 傳訊息給其他 session、開新 session、看回覆、把 session 放進資料夾，規則跟 `sampan send` 一樣。用 `sampan mcp install` 把它登記到每個啟用的 Claude profile 與 Codex（`sampan mcp uninstall` 移除，兩者都有 `--dry-run`，也都可用 `--profile <名稱>` 只處理指定的 profile，可重複），裝好後重開該 profile 的 session 才看得到工具。

- 預設 `[mcp] confirm = true`：會動作的工具（`send`、`new_session`、`cancel_dispatch`、`folder_move`）每次都由 agent 自己的權限詢問問你。sampan 在這些工具上標記「需要使用者互動」，Claude Code 每次呼叫都會跳自己的權限提示（終端機每次都問，auto／bypass 模式也問，沒有「不再詢問」；Remote Control 照官方文件應該會顯示完整的權限提示、不給一鍵核准，但還沒在手機上實測過）；Codex 靠 `approval_mode = "prompt"`，`sampan mcp install` 會替你寫進 Codex 的 `config.toml`（先備份）。提示裡看得到工具與完整參數；不同意就回「不要」，要改訊息就請 agent 改了再送。看狀態的工具永遠不問。
- 失敗就關：`confirm = true` 時，這三個工具只給確定會先問人的 client（`claude-code`、`codex-mcp-client`）用，其他 client 會被拒絕（「這個 client 不會先問使用者；要用請在 config 設 [mcp] confirm = false」），不是由 `claude`／`codex` 直接啟動的 `sampan mcp serve`（例如從 shell 灌 JSON-RPC 進去）也被拒絕（只看行程名稱，可被假冒，見下方「做不到的事」）；`codex exec` 在 approval policy 為 `never` 時這三個工具會被 Codex 拒絕（已實測），不會不問就執行；Codex 的 sampan 工具沒設成每次詢問時也拒絕（「Codex 的 sampan 工具沒設成每次詢問（<路徑>）：跑 sampan mcp install」）。
- `confirm = true` 時，agent 從 Bash 跑 `sampan send`、`sampan new … --message`、`sampan dispatches cancel` 會被拒絕（結束碼 2，「agent 請改用 sampan MCP 的工具（會先問使用者）；要讓 agent 直接用指令，在 config 設 [mcp] confirm = false」）；人在終端機跑照舊。
- 改 `[mcp] confirm` 之後：Claude 在 MCP server 重啟時才讀到（開新 session，或 `/mcp` 重新連線）；Codex 要再跑一次 `sampan mcp install`。
- 做不到的事：這擋不住自己對 tmux pane 打字的 agent（`tmux send-keys`）。Claude Code 的 sandbox 可以擋，但 sampan 不替你設定。上層行程檢查只看名稱，擋得住單純灌 JSON-RPC 的 agent，但名稱可以假冒（`exec -a claude`、叫 claude 的連結），所以跟 `tmux send-keys`、改 `[mcp] confirm` 一樣，擋不住刻意繞過 sampan 的 agent。
- `send` 不等的話馬上回派工編號；對方做完時 sampan 往呼叫者的 session 送一行通知（`〔sampan：<對方> 已回覆派工 <編號>，用 sampan 工具 dispatch_result 看內容〕`，會按 Enter），失敗時也送一行（`〔sampan：給 <對方> 的派工 <編號> 失敗：<原因>〕`），同意派工就算同意這則通知。`wait=true` 最多等 60 秒，等到就連回覆一起回。
- 呼叫者不是 sampan 管的 session（例如不在 tmux 裡）→ 工具照樣能用，只是收不到完成通知，自己用 `dispatch_result` 查。
- 工具的參數與回傳見 [reference](reference.md#sampan-mcp-serve-的工具)。

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
