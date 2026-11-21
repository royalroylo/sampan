# hub：自己的幾台機器之間互傳訊息（0.26）

sampan 管的是一台機器上的 session。hub 讓你**自己的另一台機器**上的 session 也能收到訊息、回覆，用法和本機一樣：`sampan send laptop/proj-a "…"`，或 agent 用 MCP 的 `send`、`dispatch_result`。

hub 是中繼站：每台機器主動連它（HTTPS 長輪詢），機器不用開任何連入的埠。**第一階段只處理同一個人的機器之間**；別人的訊息還不支援。

> hub 看得到訊息明文（它只是中繼站，不做端到端加密），送達確認後內容只留 14 天，沒送達的 7 天後過期。除了 hub 自己，每台機器的 `dispatches.db` 才有完整歷史。

## 組成

- **`sampan connect`**（每台機器一個常駐程式）：只用標準函式庫，每個請求都帶 `User-Agent: sampan-connect/<版本>`，不跟隨轉址（被代理的登入頁擋住時它會說）。
- **`sampan hub serve`**（只放在一台主機上）：獨立套件 `sampan[hub]`（驗證 token 需要 PyJWT 與 cryptography），只聽 `127.0.0.1`，對外請用 Tunnel 或反向代理。
- 認證：沿用你的 OIDC 身分提供者。每台機器一個 OAuth client（client credentials），hub 只收 RS256 的 token，檢查 issuer、audience（陣列裡要**包含**它）、`azp`、`exp`、`nbf`；token 建議 15 分鐘。
- 訊息帶 **HMAC-SHA256 簽章**：你的每台機器共用一把「owner key」，由你**手動**放到每台機器，不經過 hub。所以即使 hub 被入侵，也偽造不出「來自你另一台機器」的訊息（它仍能讀、丟、延遲訊息）。

## 設定

**每台機器：**
1. 在第一台：`sampan connect keygen`。它建立 `~/.config/sampan/hub/owner.key`（權限 600）並印出一行；用你自己的方式（不要貼進對話、記憶或 hub）放到其他機器的同一個位置，權限 600。
2. 放 `~/.config/sampan/hub/credentials.toml`（權限 600，不然 sampan 拒絕用）：
   ```toml
   hub_url = "https://hub.example.com"
   token_url = "https://idp.example.com/realms/work/protocol/openid-connect/token"
   client_id = "sampan-laptop"
   client_secret = "…"
   # 選填：前面有 Cloudflare Access 時，這台機器的 service token
   access_client_id = "…"
   access_client_secret = "…"
   ```
   憑證不要放進環境變數或 launchd plist。
3. `sampan hub publish <session> [--as 別名]`：**預設沒有任何 session 被其他機器看見**，要明確發布。
4. `sampan connect`（前景；要常駐就交給 launchd 或 systemd）。`sampan connect status` 看這台機器的狀態與缺什麼。

**hub 主機：**
- 建一個專屬的 OS 使用者跑 hub（資料夾 0700），不要和你的 agent 同一個使用者；資料庫與備份排除（hub 資料夾不要進 Time Machine、同步或其他備份）。
- `sampan hub serve --listen 127.0.0.1:8787 --issuer <issuer> --audience <audience> --jwks-url <jwks> --client sampan-laptop --client sampan-desktop`：**`--client` 要列出允許連的 OAuth client（每台機器一個，可重複給）**，同一個身分提供者、同一個 audience 的其他 client 不能在這裡登記機器。`--dev` 只給開發，用測試 token，**前面接了代理或 Tunnel 就拒絕服務**（看到 `X-Forwarded-For`、`CF-Connecting-IP` 等 header 就回 401）。
- 新機器第一次連上是 **pending**，不會收發任何東西。那台機器的 `sampan connect` 會印一組驗證碼；在 hub 主機 `sampan hub machines` 看到同一組，比對相同才 `sampan hub approve <名稱> --code <碼>`（`--code` 一定要給，不給不核准）。
- 撤銷：`sampan hub revoke <名稱>` 立刻生效（不等 token 過期），並丟掉等它的訊息；也要停用它的 OAuth client 與 service token。`sampan hub audit` 看稽核紀錄（不含訊息內容）。

## 用法

- `sampan send laptop/proj-a "…"`：「機器/session」，機器名是 `sampan machine` 的名稱（hub 要求唯一，重複用 `sampan machine --name` 改）；session 可以用發布時的別名。agent 用 MCP `send` 同樣寫法，每則都照現有核准閘問使用者。
- `sampan hub peers`：其他機器與它們發布的 session 和忙閒。MCP `list_sessions` 帶 `remote: true` 也看得到。
- `dispatch_result` 多一個 `remote` 區塊：`outbox`（還沒送到 hub，hub 連不上時會一直留著）→ `at-hub` → `taken`（對方機器已收下）→ `sent`（已貼進對方的 session）→ `replied`；失敗有原因：對方機器拒收、hub 拒收、對方一直沒上線而過期。
- 對方那端：訊息像本機訊息一樣進該 session（門鈴、`inbox_take`、`reply`），來源標「機器/session」。
- 還不支援：`restore`、`profile`、`raw` 對遠端、遠端的 `resend`（請再送一則）、`send_batch` 的遠端目標（請用 `send` 逐一送）。

## 傳輸保證與限制

- **至少送達一次**：信封有唯一 id；沒被 hub 接受就在 outbox 重試（退避加抖動，最久 5 分鐘一次），hub 對同一個 id 的重送回同一個答案；對方以 id 去重，同一則不會做兩次。
- 過期：沒被取走的 7 天後過期（本文立刻刪）；取走後本文再留 14 天；中繼資料（誰傳給誰、時間、大小）留 90 天。
- 額度：單則 8,000 字、信箱預設 200 則／1 MB、每人每日上限；超過會回可讀的原因。等核准的機器最多 20 台。
- 文字限制：訊息裡不能有控制字元（只放行換行與 Tab：ESC、`\r`、零寬字元、雙向控制字元、U+2028／2029 一律拒收；CRLF 會先轉成換行）——ESC 可以提早結束貼上，讓後面的字被當成按鍵。寄件端、hub、收件端各檢查一次。`expires` 最多比 `created` 晚 7 天。
- hub 伺服器：一個請求（請求行、標頭、本文）總共最多 15 秒讀完，一個位元組一個位元組慢慢送也一樣會被切斷（長輪詢等信的時間不算）、同時最多 64 個連線（超過回 503）。機器名稱與寄件者的 session 名稱也不能含控制字元、零寬字元或換行（它們會被放在貼進 session 的第一行）。
- 版本協商：太舊的客戶端會被 hub 擋下並說要更新；簽章不能被降級（hub 說「不用簽」會被忽略）。
- 時鐘：兩邊差 5 分鐘以上 `doctor` 會提醒；超過 15 分鐘訊息會被拒。
- 沒有已讀回報：`replied` 才代表對方的 agent 回了話。
