# swf-player — 原始 SWF 网页播放器

用 [Ruffle](https://ruffle.rs)（Flash Player 模拟器，WebAssembly 版）在浏览器里直接播放
`/Users/ff/Downloads/flash` 的**原始 SWF**，无需转码 —— 动画、配音、交互（翻页/点选/答题）与原 Flash 一致。

## 使用

```bash
cd output/swf-player
python3 -m http.server 8080
# 浏览器打开 http://localhost:8080
```

> 必须走 HTTP（file:// 无法加载 WASM 和 SWF 资源）。

## 包含的课件

| 课件 | 类型 | 舞台尺寸 |
|---|---|---|
| welcome / teacher / Egypt | 分页动画课件（SWF v9） | 720×576 |
| 姹 / 嫣 | 识字卡（SWF v8，自包含） | 720×576 |
| 填空乐园 / 我是小法官 | 答题课件（SWF v15, AS3 + data.xml + pic/） | 800×600 |

## 目录结构

```
swf-player/
├── index.html      # 播放页（SWF 清单 + 加载逻辑）
├── ruffle/         # Ruffle 0.7.1 selfhosted（ruffle.js + wasm）
├── swfs/           # 原始 SWF 及其外部资源（data.xml / pic/）
└── fonts/          # CJK 字体（黑体=STHeiti、宋体=华文宋体），供设备字体回退
```

## 踩过的坑（换新 SWF 时需要知道的）

1. **`player.load()` 只收一个对象参数**：`load({url, ...config})`。
   第二个参数会被静默忽略（初始版本因此丢掉了所有配置）。
2. **外部资源相对路径按页面 URL 解析**：AS3 课件运行时 `URLRequest("data.xml")`
   会请求到页面根目录。用 `urlRewriteRules` 把 `data.xml`/`pic/` 重写回 SWF 所在目录。
3. **`System.useCodePage` 在 Ruffle AVM2 里是 Missing**（AVM1 已实现）。
   SWF 声明按系统 ANSI 码页解码外部文本，Ruffle 却固定按 UTF-8 →
   需把 `data.xml` 真正转为 UTF-8（本目录 swfs/ 下的已转）。
   换新课件若中文乱码，先 `iconv -f GBK -t UTF-8`。
4. **浏览器沙箱拿不到系统字体**：`defaultFonts` 写系统字体名（PingFang SC 等）全部无效
   （日志报 `Unknown device font`，中文动态文本消失）。必须用 `fontSources` 加载 TTF；
   且 TTF 缺 full-name 时**以 URL 字符串为注册名**，`defaultFonts` 里要填一模一样的
   路径字符串（`fonts/黑体.ttf`）才能匹配。
5. SWF 里的字体名（如 `KaiTi_GB2312`/`Times New Roman`）会报 Unknown warning，
   属正常回退，不影响显示（最终走 `_sans`/`_serif` 映射）。
