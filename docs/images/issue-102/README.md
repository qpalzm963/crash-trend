# V3.6 Overview 視覺驗收證據 (Issue #102)

固定 viewport 的 before / after 截圖，以及各決策狀態的呈現。#102 要求「留下實際 render
的視覺驗收證據」，而這個 repo 沒有 pixel-perfect 的 screenshot CI，因此改為：

* **可測的部分**寫成 DOM / CSS contract：`tests/test_overview_visual_hierarchy.py`
  （閱讀順序、字級層級、狀態色調、窄版不裁切也不推寬頁面）。
* **只能用眼睛判斷的部分**留成這些截圖。

## 產生方式

截圖不是手動抓的，可以重現（`before` 對應 `main`，`after` 對應本分支）：

```bash
# 1. 用 fixture 產出自包含的 dashboard
python3 -c "from crash_trend.dashboard import generate_dashboard; \
  generate_dashboard(data_path='tests/fixtures/dashboard_v2_release_decision.json', \
                     output_path='out/_preview/after.html')"

# 2. 起一個靜態伺服器（file:// 下 JS 會被部分瀏覽器擋掉）
python3 -m http.server 8791

# 3. 固定 viewport 截圖
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --headless=new --hide-scrollbars --window-size=1440,1600 \
  --virtual-time-budget=3500 --screenshot=after-1440.png \
  "http://localhost:8791/out/_preview/after.html"
```

## 檔案

| 檔案 | 內容 |
| :-- | :-- |
| `after-390.png` | 390px 手機（#103 review 補的 phone breakpoint；見下方註） |
| `before-1440.png` / `after-1440.png` | 1440px 桌機 |
| `before-1024.png` / `after-1024.png` | 1024px compact desktop / tablet |
| `before-768.png` / `after-768.png` | 768px 窄版 |
| `state-pass-warn-1440.png` | `pass`（Android 3.1.2）+ `warn`（iOS 3.2.0） |
| `state-neutral-1440.png` | `insufficient_data`（Android 3.1.0）+ `baseline`（iOS 3.1.2） |
| `state-legacy-1440.png` | 沒有 canonical decision 的 app：`未評估` / `無 release 資料` 降級 |

三張狀態截圖用的是同一份 fixture、只改 `release_catalog[].status` 讓不同版本成為
`latest`（衍生 bundle 落在 `out/`，不進 repo）——刻意不新增 fixture，因為那會讓
「畫面看起來對」與「契約資料」兩件事有兩份真相。

## 390px 那張為什麼是「框在畫面裡」的

headless Chrome 有最小視窗寬度：給 `--window-size=390,1400` 它仍會以較寬的寬度排版、
再把截圖裁到 390px，結果看起來像「內容被切掉」，其實是截圖被裁。因此手機那張改成把
dashboard 放進一個寬 390px 的 `<iframe>`（iframe 的寬度對 media query 而言就是 viewport
寬度），再截外層頁面：

```bash
# out/_preview/phone_frame.html 只是一個 390px 寬的 iframe 外框
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --headless=new --hide-scrollbars --window-size=450,1380 \
  --virtual-time-budget=4000 --screenshot=after-390.png \
  "http://localhost:8791/out/_preview/phone_frame.html"
```

真正的判定證據不是截圖而是量測：320~1920px 掃 22 個寬度，逐一量
`documentElement.scrollWidth - clientWidth`、每個 header 控制項的矩形是否互相重疊、
是否超出視窗。結果全部是 0 / 無重疊 / 無超出，數字附在 PR 說明裡。
