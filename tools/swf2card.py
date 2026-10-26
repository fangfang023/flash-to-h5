#!/usr/bin/env python3
"""
swf2card.py — 识字卡型 SWF → H5 交互页 批量转化工具

适用对象: "识字卡/点击切换" 型 SWF（特征: 主时间轴 1~N 帧 stop(), 若干命名按钮(bt1..btN)
控制某个内容精灵(zmt.zm) gotoAndStop 到不同帧, 无翻页导航）。

流程:
  1. JPEXS 导出主时间轴首帧 PNG(背景) + 内容精灵逐帧 PNG(面板) + 声音
  2. 解析 PlaceObject 矩阵: 按钮坐标、内容精灵坐标(twip→百分比)
  3. 图像模板匹配: 面板在背景上的精确位置
  4. 解析 AS 脚本: btN -> zm.gotoAndStop(k) 映射
  5. 生成 index.html(热区点击切换面板 + 音频)

用法:
  python3 swf2card.py <a.swf> [b.swf ...] [-o 输出目录]

依赖: java (JPEXS), ffmpeg(音频转码), Pillow+numpy(模板匹配)
"""
import argparse, json, re, struct, subprocess, sys, zlib
from pathlib import Path

FFDEC = Path(__file__).parent / "ffdec/ffdec.jar"


# ---------- SWF 解析 ----------

def decompress(path: Path) -> bytes:
    data = path.read_bytes()
    if data[:3] == b'CWS':
        return data[:8] + zlib.decompress(data[8:])
    if data[:3] == b'FWS':
        return data
    raise ValueError(f"{path}: 不是 SWF 文件")


def stage_size(raw: bytes):
    """舞台尺寸(twip)"""
    nbits = raw[8] >> 3
    # RECT: 5+nbits*4 bits, 位级读取简化: 用已知结构解析
    # xMin(15bit) xMax yMin yMax 各15bit
    bits = ''.join(f'{b:08b}' for b in raw[8:8+9])
    xMin = int(bits[5:20], 2); xMax = int(bits[20:35], 2)
    yMin = int(bits[35:50], 2); yMax = int(bits[50:65], 2)
    return xMin, xMax, yMin, yMax


def parse_tags(raw: bytes):
    """yield (code, payload, frame_at_time)"""
    pos = 8
    nbits = raw[pos] >> 3
    pos += (5 + nbits * 4 + 7) // 8 + 4
    frame = 1
    while pos < len(raw) - 2:
        cl = struct.unpack('<H', raw[pos:pos+2])[0]
        code, length = cl >> 6, cl & 0x3F
        pos += 2
        if length == 0x3F:
            length = struct.unpack('<I', raw[pos:pos+4])[0]; pos += 4
        if pos + length > len(raw):
            break
        yield code, raw[pos:pos+length], frame
        if code == 1:
            frame += 1
        pos += length


def parse_matrix(payload: bytes):
    """从 PlaceObject payload 提取 translateX/Y(twip)。位级简化: 交由 XML 导出路线,此函数只处理字节级布局"""
    # 位级 MATRIX 太绕 — 由 swf2xml 路线处理
    return None


def placements_via_xml(swf: Path, workdir: Path):
    """用 JPEXS swf2xml 拿主时间轴 PlaceObject 的命名对象+矩阵"""
    workdir.mkdir(parents=True, exist_ok=True)
    xml_path = workdir / (swf.stem + '.xml')
    subprocess.run(['java', '-jar', str(FFDEC), '-swf2xml', swf.name, xml_path.resolve()],
                   cwd=swf.parent, check=True, capture_output=True)
    import xml.etree.ElementTree as ET
    root = ET.parse(xml_path).getroot()
    named = {}   # name -> (charId, tx, ty)
    for it in root.iter('item'):
        if it.get('type') in ('PlaceObject2Tag', 'PlaceObject3Tag') and it.get('name'):
            mx = None
            for c in it.iter():
                if c.get('type') == 'MATRIX':
                    mx = c
            if mx is not None:
                named[it.get('name')] = (int(it.get('characterId') or 0),
                                         int(mx.get('translateX') or 0),
                                         int(mx.get('translateY') or 0))
    # sprite 内部嵌套: zmt -> zm
    nested = {}  # (spriteId) -> list of (charId, tx, ty)
    cur = None
    for it in root.iter('item'):
        if it.get('type') == 'DefineSpriteTag':
            cur = it.get('spriteId')
        if cur and it.get('type') in ('PlaceObject2Tag', 'PlaceObject3Tag') and it.get('characterId'):
            mx = None
            for c in it.iter():
                if c.get('type') == 'MATRIX':
                    mx = c
            if mx is not None:
                nested.setdefault(cur, []).append(
                    (int(it.get('characterId')), int(mx.get('translateX') or 0),
                     int(mx.get('translateY') or 0)))
    return named, nested


def script_button_map(swf: Path, workdir: Path):
    """导出 AS 并解析 btN -> zm.gotoAndStop(k)"""
    as_dir = workdir / (swf.stem + '_as')
    subprocess.run(['java', '-jar', str(FFDEC), '-export', 'script', as_dir.resolve(), swf.name],
                   cwd=swf.parent, check=True, capture_output=True)
    mapping = {}
    for f in as_dir.rglob('*.as'):
        text = f.read_text(errors='replace')
        for m in re.finditer(r'bt(\d+)\.onRelease\s*=\s*function\(\)\s*\{(.*?)\};', text, re.S):
            bt_n = int(m.group(1))
            body = m.group(2)
            g = re.search(r'gotoAndStop\(\s*(\d+)\s*\)', body)
            if g:
                mapping[f'bt{bt_n}'] = int(g.group(1))
    return mapping


# ---------- 图像工具 ----------

def find_panel_position(bg_png: Path, panel_png: Path):
    """模板匹配: 面板在背景图中的位置"""
    from PIL import Image
    import numpy as np
    bg = np.array(Image.open(bg_png).convert('RGB')).astype(int)
    p_img = Image.open(panel_png)
    panel = np.array(p_img.convert('RGB')).astype(int)
    h, w = panel.shape[:2]
    if p_img.mode == 'RGBA':
        mask = np.array(p_img)[:, :, 3] > 128
    else:
        mask = np.ones((h, w), bool)
    best, best_score = None, 1e18
    for y in range(0, bg.shape[0] - h + 1, 2):
        for x in range(0, bg.shape[1] - w + 1, 2):
            d = np.abs(bg[y:y+h, x:x+w] - panel)[mask]
            s = d.mean()
            if s < best_score:
                best_score, best = s, (x, y)
    return best, best_score


# ---------- 主流程 ----------

def convert(swf: Path, outdir: Path):
    name = swf.stem
    slug = re.sub(r'[^\w\-]', '_', name)
    print(f"\n== {name} ==")
    raw = decompress(swf)
    xmin, xmax, ymin, ymax = stage_size(raw)
    W, H = (xmax - xmin) / 20, (ymax - ymin) / 20
    print(f"  舞台 {W:.0f}x{H:.0f}px")

    card_dir = outdir / slug
    card_dir.mkdir(parents=True, exist_ok=True)
    tmp = card_dir / '_tmp'

    # 1. 主画面(首帧) / 内容精灵帧 / 声音
    subprocess.run(['java', '-jar', str(FFDEC), '-export', 'frame', (tmp / 'main').resolve(), swf.name],
                   cwd=swf.parent, check=True, capture_output=True)
    subprocess.run(['java', '-jar', str(FFDEC), '-export', 'sprite', (tmp / 'sprites').resolve(), swf.name],
                   cwd=swf.parent, check=True, capture_output=True)
    subprocess.run(['java', '-jar', str(FFDEC), '-export', 'sound', (tmp / 'snd').resolve(), swf.name],
                   cwd=swf.parent, check=True, capture_output=True)
    bg = next((tmp / 'main').glob('*.png'))

    # 2. AS 映射 btN -> 面板帧号
    bmap = script_button_map(swf, tmp)
    print(f"  按钮映射: {bmap}")
    if not bmap:
        raise RuntimeError("未识别出 btN->gotoAndStop 结构,可能不是识字卡型 SWF")

    # 3. 定位内容精灵: zmt(外) -> zm(内)。取按钮映射里最大帧号对应的精灵
    named, nested = placements_via_xml(swf, tmp)
    if 'zmt' not in named:
        raise RuntimeError("主时间轴未找到 zmt 精灵")
    zmt_id, zmt_x, zmt_y = named['zmt']
    inner = nested.get(str(zmt_id), [])
    zm = next(((cid, tx, ty) for cid, tx, ty in inner if cid != 0), None)
    zm_id = zm[0] if zm else zmt_id

    # 4. 面板帧目录: 找 DefineSprite_{zm_id}
    panel_dir = None
    for d in (tmp / 'sprites').iterdir():
        if d.is_dir() and d.name.endswith(f'_{zm_id}'):
            panel_dir = d
            break
    if panel_dir is None:
        # 退化: 取帧数最多的 sprite 目录
        cands = [d for d in (tmp / 'sprites').iterdir() if d.is_dir()]
        panel_dir = max(cands, key=lambda d: len(list(d.glob('*.png'))))
    panels = sorted(panel_dir.glob('*.png'), key=lambda p: int(p.stem))
    print(f"  内容面板: {panel_dir.name} {len(panels)}帧")

    # 5. 模板匹配定位面板(用第2张面板,即bt1对应内容,与首帧背景必有差异大区域)
    from PIL import Image
    probe_panel = panels[1] if len(panels) > 1 else panels[0]
    (px, py), score = find_panel_position(bg, probe_panel)
    probe_w = Image.open(probe_panel).size[0]
    print(f"  面板位置: ({px},{py}) 匹配差={score:.2f}")

    # 6. 整理素材
    (card_dir / 'bg.png').write_bytes(bg.read_bytes())
    labels = ['pinyin', 'bushou', 'jiegou', 'ziyi', 'cixing', 'zuci']
    for bt, k in sorted(bmap.items(), key=lambda x: x[1]):
        n = int(bt[2:]) - 1
        lab = labels[n] if n < len(labels) else f'panel{n}'
        (card_dir / f'panel_{lab}.png').write_bytes(panels[k-1].read_bytes())
    # 首帧背景里已含面板1状态 -> 面板1(默认)通常=panels[0]

    # 音频: 自动播放的长音轨(-1.wav/-1.mp3)转mp3; 无流式轨时选最大的具名mp3(如72.mp3讲解)
    audio_html = ''
    snd_dir = tmp / 'snd'
    src_audio = None
    for cand in ('-1.wav', '-1.mp3'):
        a = snd_dir / cand
        if a.exists() and a.stat().st_size > 1000:
            src_audio = a
            break
    if src_audio is None and snd_dir.exists():
        mp3s = sorted(snd_dir.glob('*.mp3'), key=lambda p: p.stat().st_size, reverse=True)
        if mp3s:
            src_audio = mp3s[0]
    if src_audio:
        out = card_dir / 'narrate.mp3'
        subprocess.run(['ffmpeg', '-y', '-i', str(src_audio), '-ar', '44100',
                        '-c:a', 'libmp3lame', '-q:a', '2', str(out)],
                       check=True, capture_output=True)
        audio_html = '<audio id="narr" src="narrate.mp3" preload="auto"></audio>'

    # 7. 生成 HTML(热区用 SWF 按钮坐标)
    hots = []
    for bt in sorted(bmap, key=lambda b: int(b[2:])):
        n = int(bt[2:]) - 1
        cid, tx, ty = named.get(bt, (0, 0, 0))
        bx, by = tx / 20 / W * 100, ty / 20 / H * 100
        lab = labels[n] if n < len(labels) else f'panel{n}'
        hots.append(
            f'  <div class="hot" data-panel="panel_{lab}.png" '
            f'style="left:{bx:.1f}%;top:{by:.1f}%;width:14%;height:13%" title="{lab}"></div>')
    panel_left = px / W * 100
    panel_top = py / H * 100
    panel_w = probe_w / W * 100

    html = HTML_TMPL.replace('__TITLE__', f'{name} · 汉字学习卡') \
                    .replace('__HOTS__', '\n'.join(hots)) \
                    .replace('__PANEL_STYLE__',
                             f'left:{panel_left:.2f}%;top:{panel_top:.2f}%;width:{panel_w:.2f}%') \
                    .replace('__AUDIO__', audio_html)
    (card_dir / 'index.html').write_text(html, encoding='utf8')

    # 清理临时
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)
    print(f"  ✓ 输出: {card_dir}/")


HTML_TMPL = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, user-scalable=no">
<title>__TITLE__</title>
<style>
  * { margin:0; padding:0; box-sizing:border-box; }
  body { font-family:"PingFang SC","Microsoft YaHei",sans-serif;
         background:linear-gradient(180deg,#dff1ff,#f6fcff);
         min-height:100vh; display:flex; flex-direction:column;
         align-items:center; justify-content:center; }
  .card { position:relative; width:min(96vw,720px); user-select:none; }
  .card img.bg { display:block; width:100%; }
  .panel { position:absolute; __PANEL_STYLE__; pointer-events:none; }
  .panel img { width:100%; display:block; }
  .hot { position:absolute; cursor:pointer; border-radius:50%; transition:transform .12s, filter .12s; }
  .hot:hover { transform:scale(1.07); filter:brightness(1.12); }
  .hot.active { filter:brightness(1.2) drop-shadow(0 0 8px rgba(255,170,60,.9)); }
  .btnbar { display:flex; gap:10px; padding-bottom:10px; }
  .btnbar button { padding:8px 18px; border-radius:18px; border:2px solid #ffd9b0;
                   background:#fff7ef; color:#d97a1f; font-size:15px; cursor:pointer; }
  .hint { color:#5b8fb5; font-size:14px; padding:14px 0 20px; text-align:center; }
</style>
</head>
<body>
<div class="card">
  <img class="bg" src="bg.png" alt="">
  <div class="panel" id="panel"><img id="panelImg" src="" alt=""></div>
__HOTS__
</div>
<div class="btnbar">
  <button id="replay">↻ 重播讲解</button>
  <button id="reset">清空面板</button>
</div>
<div class="hint">点击画面中的按钮查看对应内容</div>
__AUDIO__
<script>
const narr = document.getElementById('narr');
const panelImg = document.getElementById('panelImg');
const hots = document.querySelectorAll('.hot');
panelImg.parentElement.style.display = 'none';
hots.forEach(h => h.addEventListener('click', () => {
  hots.forEach(x => x.classList.remove('active'));
  h.classList.add('active');
  panelImg.src = h.dataset.panel;
  panelImg.parentElement.style.display = 'block';
}));
document.getElementById('replay').onclick = () => { narr.currentTime = 0; narr.play(); };
document.getElementById('reset').onclick = () => {
  hots.forEach(x => x.classList.remove('active'));
  panelImg.parentElement.style.display = 'none';
};
if (narr) narr.play().catch(() => {});
const idx = ['pinyin','bushou','jiegou','ziyi','cixing','zuci'].indexOf(location.hash.slice(1));
if (idx >= 0 && hots[idx]) hots[idx].click();
</script>
</body>
</html>
"""


def main():
    ap = argparse.ArgumentParser(description='识字卡型 SWF → H5 交互页')
    ap.add_argument('swfs', nargs='+', type=Path)
    ap.add_argument('-o', '--out', type=Path, default=Path('h5card'))
    args = ap.parse_args()
    for swf in args.swfs:
        if not swf.exists():
            print(f"跳过(不存在): {swf}"); continue
        try:
            convert(swf, args.out.resolve())
        except Exception as e:
            print(f"✗ {swf}: {e}")
            # 失败兜底: 清理该卡片的临时提取目录(成品目录里的_tmp)
            import shutil as _sh
            slug = re.sub(r'[^\w\-]', '_', swf.stem)
            _sh.rmtree(args.out.resolve() / slug / '_tmp', ignore_errors=True)
            _sh.rmtree(args.out.resolve() / swf.stem / '_tmp', ignore_errors=True)


if __name__ == '__main__':
    main()
