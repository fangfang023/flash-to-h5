#!/usr/bin/env python3
"""
swf2video.py — Flash 课件 → H5 分页视频 批量转化工具

适用对象: "分页交互课件" 型 SWF（特征: AS 脚本含 stop() + gotoAndStop("P1".."PN") 按钮导航，
页内有连续动画+配音）。纯线性无交互的 SWF 也会被处理成单页视频。

流程: 结构分析(找分页边界) → JPEXS导帧/导音频 → 按页切片 → ffmpeg合成MP4 → 生成index.html

用法:
  python3 swf2video.py <a.swf> [b.swf ...] [-o 输出目录]

依赖: java (JPEXS), ffmpeg, Python3 标准库
环境变量: FFDEC_JAR 指定 ffdec.jar 路径(默认 ./ffdec/ffdec.jar)
"""
import argparse, json, re, shutil, struct, subprocess, sys, zlib
from pathlib import Path

FFDEC = Path(__file__).parent / "ffdec/ffdec.jar"
FPS = 25  # SWF 头里实际帧率会被解析覆盖


# ---------- SWF 解析 ----------

def decompress(path: Path) -> bytes:
    """CWS(zlib)→FWS; FWS 原样返回"""
    data = path.read_bytes()
    if data[:3] == b'CWS':
        return data[:8] + zlib.decompress(data[8:])
    if data[:3] == b'FWS':
        return data
    raise ValueError(f"{path}: 不是 SWF 文件")


def parse_header(raw: bytes):
    """返回 (rect边界twip, fps, frame_count)"""
    nbits = raw[8] >> 3
    pos = 8 + (5 + nbits * 4 + 7) // 8
    fps = struct.unpack('<H', raw[pos:pos+2])[0] / 256
    frame_count = struct.unpack('<H', raw[pos+2:pos+4])[0]
    return fps, frame_count


def scan_timeline(raw: bytes):
    """扫描时间轴: 每帧音频块字节 / 帧标签 / DoAction(stop) 帧

    返回 dict(fps, frames, audio={frame:bytes}, stops=[frame...], labels={frame:name})
    """
    fps, total = parse_header(raw)
    pos = 8
    nbits = raw[pos] >> 3
    pos += (5 + nbits * 4 + 7) // 8 + 4
    frame = 1
    audio, stops, labels = {}, [], {}
    pending = b''
    while pos < len(raw) - 2:
        cl = struct.unpack('<H', raw[pos:pos+2])[0]
        code, length = cl >> 6, cl & 0x3F
        pos += 2
        if length == 0x3F:
            length = struct.unpack('<I', raw[pos:pos+4])[0]; pos += 4
        if pos + length > len(raw):
            break
        if code == 1:                      # ShowFrame
            if pending:
                audio[frame] = pending; pending = b''
            frame += 1
        elif code == 19:                   # SoundStreamBlock
            pending = raw[pos:pos+length]
        elif code == 43:                   # FrameLabel: STRING name + \0 + namedAnchor
            labels[frame] = raw[pos:pos+length].split(b'\x00')[0].decode('utf8', 'replace')
        elif code == 12:                   # DoAction —— 只找 stop()
            # ActionStop = 0x07 + End = 0x00 -> 字节签名 0700
            if raw[pos:pos+length].startswith(b'\x07\x00'):
                stops.append(frame)
        pos += length
    return dict(fps=fps, frames=total, audio=audio, stops=stops, labels=labels)


def scan_buttons(swf: Path) -> dict:
    """导出 AS 并解析按钮跳转映射: {buttonId: 'P2'} (on release → gotoAndStop)"""
    work = Path('swf2video_work/_btn')
    work.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(['java', '-jar', str(FFDEC), '-export', 'script',
                        work.resolve(), swf.name], cwd=swf.parent, capture_output=True)
    mapping = {}
    if r.returncode == 0:
        for f in work.rglob('*.as'):
            src = f.read_text(errors='replace')
            if 'gotoAndStop' not in src:
                continue
            m_bid = re.search(r'DefineButton2_(\d+)', str(f))
            m_tgt = re.search(r'gotoAndStop\("(\w+)"\)', src)
            if m_bid and m_tgt:
                mapping[m_bid.group(1)] = m_tgt.group(1)
    return mapping


def button_hotspots(swf: Path, swf_xml: Path) -> list:
    """按钮画面内热区: 按钮元件bounds × 放置矩阵 → 世界矩形(px)。
    与 swf2card 的热区逻辑同源。返回 [{cid, x, y, w, h}]"""
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        from swf2card import SwfModel, matrix_from_xml, transform_bbox
        model = SwfModel(swf_xml)
        out = []
        for el in model.tags:
            cid = int(el.get('characterId') or 0)
            if not ((el.get('type') or '').startswith('PlaceObject') and cid in model.buttons):
                continue
            M = matrix_from_xml(el)
            bb = model.union_bounds(cid)
            if not bb:
                continue
            w = transform_bbox(M, bb)
            out.append(dict(cid=cid, x=w[0]/20, y=w[2]/20,
                            w=(w[1]-w[0])/20, h=(w[3]-w[2])/20))
        return out
    except Exception as e:
        print(f"  热区提取失败({e}) — 按钮退化为文字按钮")
        return []


def detect_pages(tl) -> list:
    """推断分页边界。优先帧标签(P1..)；否则用 stop() 帧；否则整条为 1 页。

    返回 [(start_frame, end_frame_inclusive), ...]
    """
    # frame 1 上的 stop() 是"首页加载即停"的初始化,不算页界
    stops = sorted(s for s in tl['stops'] if 1 < s < tl['frames'])
    page_labels = {f: n for f, n in tl['labels'].items()
                   if re.fullmatch(r'P\d+', n, re.I)}
    if page_labels:
        starts = sorted(page_labels)
        starts = [f for f in starts if f > 1] or starts
        bounds = starts + [tl['frames'] + 1]
        return [(bounds[i], bounds[i+1] - 1) for i in range(len(bounds) - 1)]
    if stops:
        # stop() 所在帧是每页的最后一帧 -> 页边界
        bounds = [1] + [s + 1 for s in stops if s + 1 <= tl['frames']]
        merged = []
        for b in bounds:
            if not merged or b > merged[-1]:
                merged.append(b)
        if merged[-1] <= tl['frames']:  # 末页从最后 stop+1 到末帧
            merged.append(tl['frames'] + 1)
        pages = [(merged[i], merged[i+1] - 1) for i in range(len(merged) - 1)]
        # 尾巴页(末 stop 后的残帧,<1s)并入前一页
        if len(pages) >= 2 and pages[-1][1] - pages[-1][0] + 1 < tl['fps']:
            pages[-2] = (pages[-2][0], pages[-1][1])
            pages.pop()
        return pages
    return [(1, tl['frames'])]


# ---------- 工具调用 ----------

def ffdec(args: list, cwd: Path, swf_abs: Path):
    """JPEXS 要求相对路径在同一目录下,统一在 SWF 所在目录执行"""
    subprocess.run(['java', '-jar', str(FFDEC), *args],
                   cwd=swf_abs.parent, check=True, capture_output=True)


def export_frames(swf: Path, outdir: Path):
    outdir.mkdir(parents=True, exist_ok=True)
    ffdec(['-export', 'frame', outdir.resolve(), swf.name], outdir.parent, swf)
    n = len(list(outdir.glob('*.png')))
    if n == 0:
        raise RuntimeError(f"{swf}: JPEXS 未导出任何帧")
    return n


def export_audio(swf: Path, outdir: Path) -> Path | None:
    """导出流式音轨(-1.mp3/-1.wav)。纯音效 SWF 返回 None"""
    outdir.mkdir(parents=True, exist_ok=True)
    ffdec(['-export', 'sound', outdir.resolve(), swf.name], outdir.parent, swf)
    for cand in ('-1.mp3', '-1.wav'):
        p = outdir / cand
        if p.exists() and p.stat().st_size > 0:
            return p
    return None


def slice_audio(track: Path, t0: float, dur: float, out: Path):
    """从干净音轨按时间切片,统一升采样 44.1kHz(规避 AAC 低采样率伪声)。
    请求区间超出音轨长度时只切到音轨尾(剩余部分由 apad/静音兜底)。"""
    probe = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                            '-of', 'csv=p=0', str(track)], capture_output=True, text=True)
    try:
        total = float(probe.stdout.strip())
    except ValueError:
        total = 0.0
    if t0 >= total:            # 区间完全在音轨外 -> 空文件,走静音分支
        out.write_bytes(b'')
        return
    real_dur = min(dur, total - t0)
    subprocess.run(['ffmpeg', '-y', '-ss', f'{t0:.3f}', '-t', f'{real_dur:.3f}',
                    '-i', str(track), '-ar', '44100',
                    '-c:a', 'libmp3lame', '-q:a', '0', str(out)],
                   check=True, capture_output=True)


def encode_segment(frames_dir: Path, n_frames: int, audio: Path | None,
                   out: Path, fps: float):
    dur = n_frames / fps
    cmd = ['ffmpeg', '-y', '-framerate', str(fps), '-i', str(frames_dir / '%d.png')]
    if audio and audio.exists() and audio.stat().st_size > 0:
        # MP3 直通(不经 AAC 重编码,规避低采样率伪声);音频不足视频长时尾部自然静音
        cmd += ['-i', str(audio), '-map', '0:v', '-map', '1:a', '-c:a', 'copy']
    else:
        cmd += ['-f', 'lavfi', '-i', 'anullsrc=r=44100:cl=stereo',
                '-map', '0:v', '-map', '1:a', '-c:a', 'aac', '-b:a', '96k']
    cmd += ['-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-crf', '20', '-preset', 'medium',
            '-t', f'{dur:.3f}', '-movflags', '+faststart', str(out)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg 失败: {r.stderr[-500:]}")


def _cleanup(workdir: Path, stem: str):
    """流水线磁盘保护: 删除单个文件的帧目录/音频中间件(成品MP4不受影响)"""
    import shutil
    for d in (workdir / f"frames_{stem}", workdir / f"audio_{stem}"):
        shutil.rmtree(d, ignore_errors=True)
    for f in workdir.glob(f"{stem}*"):
        f.unlink(missing_ok=True)


# ---------- 播放页 ----------

ANIM_TMPL = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, user-scalable=no">
<title>__TITLE__</title>
<style>
  * { margin:0; padding:0; box-sizing:border-box; }
  body { font-family:"PingFang SC","Microsoft YaHei",sans-serif;
         background:linear-gradient(180deg,#aee3ff,#e8f7ff 60%,#cdeffd);
         display:flex; flex-direction:column; align-items:center; justify-content:center;
         min-height:100vh; }
  .stage { position:relative; width:min(96vw,__W__px); user-select:none; overflow:hidden;
            border-radius:8px; box-shadow:0 6px 24px rgba(60,120,180,.25); }
  .stage img.frame { display:block; width:100%; }
  /* 画面内按钮热区: 位置/大小来自SWF放置矩阵, 按钮 Up 态已印在帧里 */
  .hotspot { position:absolute; cursor:pointer; z-index:2;
              transition:transform .15s ease; }
  .hotspot:hover { transform:scale(1.12); filter:drop-shadow(0 0 6px rgba(255,180,60,.9)); }
  .hotspot:active { transform:scale(.96); }
</style>
</head>
<body>
<div class="stage" id="stage">
  <img class="frame" id="frame" src="__FIRST_FRAME__" alt="">
</div>
<script>
// ===== 忠实还原原SWF: 页内帧动画(25fps帧驱动, 字幕含在帧中) + 按钮(热区/跳转/音效) =====
const PAGES = __PAGES__;      // [{frames:[帧文件名], audio:'p1.mp3', dur:秒}]
const HOTSPOTS = __HOTSPOTS__; // [{x,y,w,h(px,舞台系), seg:目标页}]
const FPS = __FPS__;
const W = __W__, H = __H__;
let page = 1, frameIdx = 0, playing = false, raf = 0, audio = null, t0 = 0;
const img = document.getElementById('frame');
const stage = document.getElementById('stage');
const clickSnd = new Audio('click.mp3');
function playClick(){ clickSnd.currentTime = 0; clickSnd.play().catch(()=>{}); }

function renderHotspots() {
  stage.querySelectorAll('.hotspot').forEach(e => e.remove());
  HOTSPOTS.forEach(h => {
    const d = document.createElement('div');
    d.className = 'hotspot';
    d.style.left = (h.x / W * 100) + '%';
    d.style.top = (h.y / H * 100) + '%';
    d.style.width = (h.w / W * 100) + '%';
    d.style.height = (h.h / H * 100) + '%';
    d.onclick = () => goto(h.seg);
    stage.appendChild(d);
  });
}
function tick(now) {
  if (!playing) return;
  const el = (now - t0) / 1000 * FPS;
  const n = PAGES[page-1].frames.length;
  frameIdx = Math.min(n - 1, Math.floor(el));
  img.src = PAGES[page-1].frames[frameIdx];
  if (frameIdx >= n - 1) {          // 页尾停住 = 原SWF的 stop()
    playing = false;
    if (audio) audio.pause();
    return;
  }
  raf = requestAnimationFrame(tick);
}
function playPage(seg) {
  page = seg; frameIdx = 0;
  const pd = PAGES[page-1];
  img.src = pd.frames[0];
  if (audio) { audio.pause(); audio = null; }
  if (pd.audio) {
    audio = new Audio(pd.audio);
    audio.play().catch(()=>{});
  }
  playing = true;
  t0 = performance.now();
  cancelAnimationFrame(raf);
  raf = requestAnimationFrame(tick);
}
function goto(seg) {
  if (seg < 1 || seg > PAGES.length) return;
  playClick();
  playPage(seg);
}
// 预加载: 首页帧立即载, 其余页帧后台预热(点按钮零等待)
const preloadPool = {};
PAGES.forEach((pd, i) => pd.frames.forEach(f => {
  if (i === 0) { const im = new Image(); im.src = f; }
  else { preloadPool[f] = () => { const im = new Image(); im.src = f; delete preloadPool[f]; }; }
}));
setTimeout(() => Object.values(preloadPool).forEach(fn => fn()), 3000);
renderHotspots();
playPage(1);   // 打开即播第1页(与原SWF一致)
</script>
</body>
</html>
"""


# ---------- 主流程 ----------

def export_click_sound(swf: Path, outdir: Path) -> str | None:
    """提取按钮点击音效(DefineButtonSoundChar2=Down态 → DefineSound),
    转码到输出目录 click.mp3。返回文件名或 None。忠实还原原 Flash
    "点按钮有声"的行为(paged H5 翻页时播放)。"""
    import xml.etree.ElementTree as ET
    work = outdir / "_meta"
    work.mkdir(parents=True, exist_ok=True)
    xmlp = work / f"{swf.stem}.xml"
    r = subprocess.run(['java', '-jar', str(FFDEC), '-swf2xml', swf.name, xmlp.resolve()],
                       cwd=swf.parent, capture_output=True)
    if r.returncode != 0:
        return None
    root = ET.parse(xmlp).getroot()
    ids = set()
    for it in root.iter('item'):
        if (it.get('type') or '') == 'DefineButtonSoundTag':
            cid = it.get('buttonSoundChar2')   # Char2 = Down 态(按下发声)
            if cid and cid != '0':
                ids.add(int(cid))
    if not ids:
        return None
    # 导出这些 DefineSound
    id_arg = ','.join(str(i) for i in sorted(ids))
    snd_dir = work / "snd"
    subprocess.run(['java', '-jar', str(FFDEC), '-selectid', id_arg,
                    '-export', 'sound', snd_dir.resolve(), swf.name],
                   cwd=swf.parent, capture_output=True)
    # 取第一个成功的转成 mp3
    for f in sorted(snd_dir.glob('*')):
        out = outdir / "click.mp3"
        r = subprocess.run(['ffmpeg', '-y', '-i', str(f), '-ar', '44100',
                            '-c:a', 'libmp3lame', '-q:a', '4', str(out)],
                           capture_output=True)
        if r.returncode == 0:
            shutil.rmtree(work, ignore_errors=True)
            return "click.mp3"
    shutil.rmtree(work, ignore_errors=True)
    return None


def convert(swf: Path, workdir: Path, outdir: Path, results: dict,
            full: bool = True, keep_work: bool = False):
    """转化单个 SWF。keep_work=True 保留中间产物(调试用); 默认转完即清(批量磁盘保护)"""
    name = swf.stem
    stem = re.sub(r'[^\w\-]', '_', name)  # 文件名安全化
    print(f"\n== {name} ==")
    raw = decompress(swf)
    tl = scan_timeline(raw)
    pages = detect_pages(tl)
    # 按钮跳转映射(忠实还原原Flash导航语义): {buttonId: 'P2'} → 第几页
    btn_map = scan_buttons(swf) if not full else {}
    # 按钮画面内热区(坐标来自SWF放置矩阵 — 按钮已印在视频帧里, 只需叠透明点击区)
    hotspots = []
    if btn_map and not full:
        r2 = subprocess.run(['java', '-jar', str(FFDEC), '-swf2xml', swf.name,
                             (workdir / 'hot.xml').resolve()], cwd=swf.parent, capture_output=True)
        if r2.returncode == 0:
            hotspots = button_hotspots(swf, workdir / 'hot.xml')
        if hotspots:
            print(f"  按钮热区: {len(hotspots)}个 {[(h['x'], h['y']) for h in hotspots]}")
    # 页号 → 段索引: 标签P1在帧f → 页i = 含f的段
    label_to_seg = {}
    for lf, lname in tl['labels'].items():
        m = re.fullmatch(r'P(\d+)', lname, re.I)
        if m:
            for i, (f0, f1) in enumerate(pages, 1):
                if f0 <= lf <= f1:
                    label_to_seg[lname.upper()] = i
                    break
    print(f"  时间轴: {tl['frames']}帧 @{tl['fps']:.0f}fps, 页边界: {pages}(整条模式不切片), "
          f"stop帧: {tl['stops']}, 标签: {tl['labels']}")
    if btn_map:
        nav = {b: label_to_seg.get(t) for b, t in btn_map.items()}
        print(f"  按钮导航: {btn_map} → 段 {nav}")

    frames_dir = workdir / f"frames_{stem}"
    export_frames(swf, frames_dir)
    track = export_audio(swf, workdir / f"audio_{stem}")
    print(f"  导帧完成, 音轨: {track.name if track else '无(全程静音)'}")

    total_audio = tl['audio']
    seg_outdir = outdir / "videos"
    seg_outdir.mkdir(parents=True, exist_ok=True)

    if full:
        # 整条模式: 全时间轴一条视频。
        # 音频对齐: SWF流式MP3按40ms帧窗口切碎, 一帧=一个SoundStreamBlock,
        # 但MP3帧边界与SWF帧边界不对齐(块间有跨帧碎片)。正确算法:
        #   1. 找出连续有块的帧"段" — 段内所有块的MP3数据顺序拼接成一个完整流再解码
        #   2. 段与段之间的无块帧区间 = 真静音, 填静音PCM
        #   3. 段音频从段的起始帧时间戳开始铺放
        import io as _io, wave as _wave
        SR = 44100
        fps = tl['fps']
        blocks = tl['audio']
        frames_sorted = sorted(blocks)
        # 分段: 帧连续(间隔<=2帧容差)归为一段
        segments = []
        seg_start = frames_sorted[0]
        prev = frames_sorted[0]
        for f in frames_sorted[1:] + [10**9]:
            if f - prev > 2:
                segments.append((seg_start, prev))
                seg_start = f
            prev = f
        total_samples = int(tl['frames'] / fps * SR) + SR
        canvas = bytearray(total_samples * 4)  # stereo 16bit
        seg_report = []
        for s0, s1 in segments:
            stream = b''.join((blocks[f][4:] if len(blocks[f]) > 8 else blocks[f])
                              for f in range(s0, s1 + 1) if f in blocks)
            bf = workdir / f"{stem}_seg.mp3"
            bf.write_bytes(stream)
            wavf = workdir / f"{stem}_seg.wav"
            r = subprocess.run(['ffmpeg','-y','-i',str(bf),'-ar',str(SR),'-ac','2',
                                '-c:a','pcm_s16le',str(wavf)], capture_output=True)
            if r.returncode != 0:
                bf.unlink(missing_ok=True)
                continue
            w = _wave.open(str(wavf))
            data = w.readframes(w.getnframes())
            w.close()
            wavf.unlink()
            bf.unlink(missing_ok=True)
            pos = int((s0 - 1) / fps * SR) * 4
            endpos = min(pos + len(data), len(canvas))
            canvas[pos:endpos] = data[:endpos - pos]
            seg_report.append(f"段{s0}-{s1}帧({s0/fps:.1f}-{(s1+1)/fps:.1f}s) 音频{len(data)/4/SR:.2f}s")
        outwav = workdir / f"{stem}_full.wav"
        w = _wave.open(str(outwav), 'wb')
        w.setnchannels(2); w.setsampwidth(2); w.setframerate(SR)
        w.writeframes(bytes(canvas))
        w.close()
        out_mp4 = seg_outdir / f"{stem}.mp4"
        encode_segment(frames_dir, tl['frames'], outwav, out_mp4, tl['fps'])
        dur = tl['frames'] / fps
        for line in seg_report:
            print(f"  {line}")
        print(f"  ✓ 整条输出: {out_mp4.name} ({dur:.2f}s)")
        results[stem] = dict(title=name, pages=1)
        if not keep_work:
            _cleanup(workdir, stem)
        return 1

    n_pages = len(pages)
    for i, (f0, f1) in enumerate(pages, 1):
        nf = f1 - f0 + 1
        dur = nf / tl['fps']
        seg_audio = workdir / f"{stem}_p{i}.mp3"
        has_sound = any(f0 <= f <= f1 for f in total_audio)
        if track and has_sound:
            t0 = (f0 - 1) / tl['fps']
            slice_audio(track, t0, dur, seg_audio)
        else:
            seg_audio = None
        encode_segment(frames_dir, nf, seg_audio,
                       seg_outdir / f"{stem}_p{i}.mp4", tl['fps'])
        print(f"  p{i}: 帧{f0}-{f1} ({nf}帧={dur:.2f}s) 音频{'✓' if seg_audio else '静音'}")

    results[stem] = dict(title=name, pages=n_pages,
                         nav={b: label_to_seg.get(t) for b, t in btn_map.items()},
                         hotspots=[{k: round(v, 1) for k, v in h.items()} for h in hotspots])
    if not keep_work:
        _cleanup(workdir, stem)
    return n_pages


def convert_animated(swf: Path, workdir: Path, outdir: Path, keep_work: bool = False):
    """忠实还原产物形态: 一个SWF → 一个独立H5动画页。
    页内动画 = 逐帧PNG序列 + JS帧驱动(字幕/按钮Up态已印在帧里);
    旁白 = 每页一个mp3, 与帧同步播放; 按钮 = 画面内热区(hover放大,
    点击播原按钮音效); 页尾停住 = 原SWF的 stop()。"""
    import shutil
    name = swf.stem
    stem = re.sub(r'[^\w\-]', '_', name)
    print(f"\n== {name} (动画页模式) ==")
    raw = decompress(swf)
    tl = scan_timeline(raw)
    pages = detect_pages(tl)
    btn_map = scan_buttons(swf)
    # 页号→段索引
    label_to_seg = {}
    for lf, lname in tl['labels'].items():
        m = re.fullmatch(r'P(\d+)', lname, re.I)
        if m:
            for i, (f0, f1) in enumerate(pages, 1):
                if f0 <= lf <= f1:
                    label_to_seg[lname.upper()] = i
                    break
    print(f"  时间轴: {tl['frames']}帧 @{tl['fps']:.0f}fps, 页: {pages}, 按钮: {btn_map}")

    # 帧序列(整条导出一次)
    frames_dir = workdir / f"frames_{stem}"
    n_exported = export_frames(swf, frames_dir)
    track = export_audio(swf, workdir / f"audio_{stem}")
    # 舞台尺寸: 从第1帧PNG读
    from PIL import Image
    first_png = frames_dir / '1.png'
    W, H = Image.open(first_png).size

    # 输出结构: outdir/<stem>/frames/*.jpg + p*.mp3 + <stem>.html + click.mp3
    out = outdir / stem
    out.mkdir(parents=True, exist_ok=True)
    fr_out = out / 'frames'
    fr_out.mkdir(exist_ok=True)

    page_defs = []
    for i, (f0, f1) in enumerate(pages, 1):
        # 帧转webp q80(比同质量jpg小40%, 字幕/木牌文字细节验证无损)
        names = []
        for f in range(f0, f1 + 1):
            src = frames_dir / f'{f}.png'
            if not src.exists():
                continue
            dst = fr_out / f'p{i}_{f - f0 + 1:03d}.webp'
            subprocess.run(['ffmpeg', '-y', '-i', str(src), '-quality', '80', str(dst)],
                           capture_output=True)
            names.append(f'frames/{dst.name}')
        # 页旁白
        aud_name = None
        has_sound = any(f0 <= f <= f1 for f in tl['audio'])
        if track and has_sound:
            seg = out / f'p{i}.mp3'
            slice_audio(track, (f0 - 1) / tl['fps'], (f1 - f0 + 1) / tl['fps'], seg)
            aud_name = seg.name
        page_defs.append(dict(frames=names, audio=aud_name,
                              dur=round((f1 - f0 + 1) / tl['fps'], 2)))
        print(f"  p{i}: {len(names)}帧 {(f1-f0+1)/tl['fps']:.2f}s 音频{'✓' if aud_name else '无'}")

    # 按钮热区 + 跳转
    hotspots = []
    r2 = subprocess.run(['java', '-jar', str(FFDEC), '-swf2xml', swf.name,
                         (workdir / 'hot.xml').resolve()], cwd=swf.parent, capture_output=True)
    if r2.returncode == 0:
        sys.path.insert(0, str(Path(__file__).parent))
        try:
            from swf2card import SwfModel, matrix_from_xml, transform_bbox
            model = SwfModel(workdir / 'hot.xml')
            for el in model.tags:
                cid = int(el.get('characterId') or 0)
                if not ((el.get('type') or '').startswith('PlaceObject') and cid in model.buttons):
                    continue
                M = matrix_from_xml(el)
                bb = model.union_bounds(cid)
                if not bb:
                    continue
                w = transform_bbox(M, bb)
                seg = label_to_seg.get(btn_map.get(str(cid), ''), None)
                hotspots.append(dict(x=round(w[0]/20, 1), y=round(w[2]/20, 1),
                                     w=round((w[1]-w[0])/20, 1), h=round((w[3]-w[2])/20, 1),
                                     seg=seg))
            print(f"  热区: {[(h['x'], h['y'], h['seg']) for h in hotspots]}")
        except Exception as e:
            print(f"  热区提取失败({e})")

    # 按钮点击音效
    click_snd = export_click_sound(swf, out)

    html = (ANIM_TMPL
            .replace('__TITLE__', f'{name}')
            .replace('__W__', str(W))
            .replace('__H__', str(H))
            .replace('__FPS__', str(tl['fps']))
            .replace('__FIRST_FRAME__', page_defs[0]['frames'][0] if page_defs and page_defs[0]['frames'] else '')
            .replace('__PAGES__', json.dumps(page_defs, ensure_ascii=False))
            .replace('__HOTSPOTS__', json.dumps(hotspots, ensure_ascii=False)))
    (out / f'{stem}.html').write_text(html, encoding='utf8')
    print(f"  ✓ 输出: {out}/{stem}.html ({len(page_defs)}页)")
    if not keep_work:
        _cleanup(workdir, stem)
    return len(page_defs)


def main():
    ap = argparse.ArgumentParser(description='Flash 分页课件 → H5 分页视频')
    ap.add_argument('swfs', nargs='+', type=Path)
    ap.add_argument('-o', '--out', type=Path, default=Path('h5video'),
                    help='输出目录 (默认 ./h5video)')
    ap.add_argument('--paged', action='store_true',
                    help='按 Flash 翻页结构拆成多段(默认整条连播一条到底)')
    ap.add_argument('--anim', action='store_true',
                    help='忠实动画页模式: 每SWF一个独立H5(帧驱动动画+画面按钮热区+旁白)')
    ap.add_argument('--keep-work', action='store_true',
                    help='保留中间产物(帧PNG等,调试用;默认转完即删,防止批量时磁盘爆满)')
    ap.add_argument('-w', '--work', type=Path, default=Path('swf2video_work'),
                    help='中间产物目录 (默认 ./swf2video_work)')
    args = ap.parse_args()

    if args.anim:
        for swf in args.swfs:
            if not swf.exists():
                print(f"跳过(不存在): {swf}"); continue
            try:
                convert_animated(swf, args.work.resolve(), args.out.resolve(),
                                 keep_work=args.keep_work)
            except Exception as e:
                import traceback; traceback.print_exc()
                print(f"✗ {swf}: {e}")
                if not args.keep_work:
                    _cleanup(args.work.resolve(), re.sub(r'[^\w\-]', '_', swf.stem))
        return

    results = {}
    click_snd = None
    for swf in args.swfs:
        if not swf.exists():
            print(f"跳过(不存在): {swf}"); continue
        try:
            n = convert(swf, args.work.resolve(), args.out.resolve(), results,
                        full=not args.paged, keep_work=args.keep_work)
            # paged 模式: 提取按钮点击音效(忠实还原原Flash"点按钮有声")
            if args.paged and n and click_snd is None:
                click_snd = export_click_sound(swf, args.out.resolve())
                if click_snd:
                    print(f"  点击音效: {click_snd}")
        except Exception as e:
            print(f"✗ {swf}: {e}")
            if not args.keep_work:
                _cleanup(args.work.resolve(), re.sub(r'[^\w\-]', '_', swf.stem))

    if results:
        snd_js = ("const CLICK = 'click.mp3';\nconst clickSnd = new Audio(CLICK);\n"
                  "function playClick(){ clickSnd.currentTime=0; clickSnd.play().catch(()=>{}); }\n"
                  if click_snd else "function playClick(){}\n")
        (args.out / 'index.html').write_text(
            INDEX_TMPL.replace('__COURSES__', json.dumps(results, ensure_ascii=False))
                      .replace('__CLICK_SOUND__', snd_js),
            encoding='utf8')
        print(f"\n✓ 完成 {len(results)} 个课件 -> {args.out}/index.html")


if __name__ == '__main__':
    main()
