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
