# Flash → H5 批量转化工具

三个脚本对应三类 Flash 课件。判别方法: 先跑 `swf2video.py`,报"未识别分页"或只有 1 帧静止画面时,再看是否有 data.xml(用 `swf2quiz.py`),否则试 `swf2card.py`。

## 成品与模板位置

所有生成品统一放在项目根的 `output/` 下:

```
output/
├── videos_full/   整条视频成品 (Egypt/teacher/welcome.mp4)
├── h5card/        识字卡成品 (姹/嫣)     ← swf2card 的样板
├── h5quiz/        答题成品+模板 (我是小法官/填空乐园)  ← swf2quiz 的运行依赖,勿删
├── card/cha/      姹手工精修版
└── swf-player/    Ruffle 兜底播放器(疑难文件用)
```

## swf2video.py — 分页/线性课件 → 整条 MP4

```bash
python3 tools/swf2video.py a.swf b.swf -o output/xxx     # 默认整条连播
python3 tools/swf2video.py a.swf --paged -o output/xxx   # 按原Flash翻页结构拆段
python3 tools/swf2video.py a.swf --keep-work -o out      # 保留中间产物(调试)
```

音频按 SWF 帧位置精确对齐(间隙还原原 Flash 翻页停顿)。**默认转完一个文件立即删除其帧PNG等中间产物**——3000+ 文件批量时不会撑爆磁盘。

## swf2card.py — 识字卡 → H5 交互页

```bash
python3 tools/swf2card.py x.swf -o output/card
```

## swf2quiz.py — 判题/填空课件 → H5 交互页

```bash
# 注意: JPEXS 对中文路径有bug,输入先复制到英文路径
mkdir -p /tmp/qin && cp -r "某课件目录" /tmp/qin/quiz1
python3 tools/swf2quiz.py /tmp/qin/quiz1 -o output/quiz_out
```

自动判题型: `<top answer="right|wrong">` → 判断题; `<answer>` 子节点 → 填空题。

## 依赖

- Java(JRE 11+) — JPEXS 自带于 `tools/ffdec/`,无需安装
- ffmpeg (`brew install ffmpeg`)
- Python3 + Pillow + numpy (`pip3 install pillow numpy`)

## 动画层管线(2026-10-27 重写: 矩阵定位)

识字卡类课件的装饰动画(荷花/水车/云等)自动还原:

1. **干净底图**: swf2xml → 删除动画元件的PlaceObject条目 → xml2swf回写 → 重渲染
   (替代"合影bg", 动画元件不再印死在底图里)
2. **网格sprite-sheet**: 多帧精灵导出为10列网格(不压缩变形), 帧率精确时长 = 帧数/SWF头fps
3. **矩阵链定位(2026-10-27 起, 替代模板匹配)**:
   - 位置从 SWF 放置矩阵直读: 世界矩阵 = 主时间轴 ∘ 容器sprite ∘ 实例(逐层复合)
   - 全帧并集bounds: 递归计算 sprite 内部所有放置(含逐帧 move 矩阵)×shape bounds
     的四角包络 — 与 JPEXS 导出 canvas 吻合到亚像素(281/282, 401/401 等)
   - 注意 `hasScale=false` 时 scale=1.0 而非 0(JPEXS XML 属性语义)
   - 多实例全量还原(如 sprite15 放置3次→3个实例), 镜像(scaleX<0)用 CSS
     `scaleX(-1)`, 旋转实例按矩阵角预烘焙专属 sheet + 像素对齐标定
   - 模板匹配(locate_anims)已废弃保留, 仅作最后兜底
4. **solo渲染对账(保真护栏)**: 每个实例剪XML生成只含它的solo SWF → JPEXS渲染
   → 量内容bbox 与矩阵计算值对比。无旋转实例阈值3px(实测0.1~1.2px);
   旋转实例直接用渲染值+邻域像素对齐标定
5. **资源hash命名**: 内容变文件名变, 根治浏览器缓存
6. **按钮热区/内容面板**: 同样矩阵计算(btN按钮bounds×放置矩阵, zmt/zm矩阵链),
   不再写死 14%×13%

已知限制:
- z-index层级: 底图(z1) < 动画层(z1) < 按钮热区(z2) < 内容面板(z3)
- 旋转实例的sheet按实例烘焙(同元件多旋转实例会有多份sheet, 体积略增)
- 同一套模板课件(姹/嫣)几何完全一致, 属正常(差异只在汉字内容/音频)
