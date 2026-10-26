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
        elif code == 43:                   # FrameLabel
            labels[frame] = raw[pos+2:pos+length].split(b'\x00')[0].decode('utf8', 'replace')
        elif code == 12:                   # DoAction —— 只找 stop()
            # ActionStop = 0x07 + End = 0x00 -> 字节签名 0700
            if raw[pos:pos+length].startswith(b'\x07\x00'):
                stops.append(frame)
        pos += length
    return dict(fps=fps, frames=total, audio=audio, stops=stops, labels=labels)


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

INDEX_TMPL = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, user-scalable=no">
<title>Flash 课件 H5 版</title>
<style>
  * { margin:0; padding:0; box-sizing:border-box; }
  body { font-family:"PingFang SC","Microsoft YaHei",sans-serif;
         background:linear-gradient(180deg,#aee3ff,#e8f7ff 60%,#cdeffd);
         display:flex; flex-direction:column; align-items:center; min-height:100%; }
  .tabs { display:flex; gap:10px; padding:12px; flex-wrap:wrap; justify-content:center; }
  .tab { padding:8px 22px; border-radius:20px; border:2px solid #fff;
          background:rgba(255,255,255,.65); color:#2a6fa8; font-size:16px; cursor:pointer; }
  .tab.active { background:#ff9a3c; color:#fff; }
  .stage { width:min(92vw,720px); background:#fff; border-radius:14px; overflow:hidden;
            box-shadow:0 6px 24px rgba(60,120,180,.25); }
  video { display:block; width:100%; }
  .bar { display:flex; align-items:center; justify-content:center; gap:14px; padding:14px 0 4px; }
  .nav { width:46px; height:46px; border-radius:50%; border:none; cursor:pointer;
          background:#2a86c8; color:#fff; font-size:22px; }
  .nav:disabled { background:#b9d4e6; }
  .dots { display:flex; gap:10px; }
  .dot { width:14px; height:14px; border-radius:50%; background:#c3dcec; cursor:pointer; }
  .dot.active { background:#ff9a3c; transform:scale(1.25); }
  .replay { padding-top:6px; }
  .replay button { padding:8px 20px; border-radius:18px; border:2px solid #ffd9b0;
                    background:#fff7ef; color:#d97a1f; font-size:15px; cursor:pointer; }
  .hint { color:#6a97b5; font-size:13px; padding:8px 0 18px; }
</style>
</head>
<body>
<div class="tabs" id="tabs"></div>
<div class="stage"><video id="player" playsinline preload="auto"></video></div>
<div class="bar">
  <button class="nav" id="prev">‹</button>
  <div class="dots" id="dots"></div>
  <button class="nav" id="next">›</button>
</div>
<div class="replay"><button onclick="p.currentTime=0;p.play()">↻ 重播本页</button></div>
<div class="hint">每页播完后点 › 下一页（与原 Flash 翻页一致）</div>
<script>
const COURSES = __COURSES__;
let course = Object.keys(COURSES)[0], page = 1;
const p = document.getElementById('player');
function load() {
  p.src = `videos/${course}_p${page}.mp4`;
  const n = COURSES[course];
  document.getElementById('dots').innerHTML =
    Array.from({length:n}, (_,i) =>
      `<span class="dot" onclick="page=${i+1};load()" style="${i+1===page?'background:#ff9a3c;transform:scale(1.25)':''}"></span>`).join('');
  document.getElementById('prev').disabled = page === 1;
  document.getElementById('next').disabled = page === n;
}
document.getElementById('prev').onclick = () => { if (page>1) {page--;load();} };
document.getElementById('next').onclick = () => { if (page<COURSES[course]) {page++;load();} };
document.getElementById('tabs').innerHTML = Object.entries(COURSES).map(([k,v],i) =>
  `<button class="tab" onclick="course='${k}';page=1;document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));this.classList.add('active');load()">${v.title}</button>`).join('');
document.querySelector('.tab').classList.add('active');
load();
</script>
</body>
</html>
"""


# ---------- 主流程 ----------

def convert(swf: Path, workdir: Path, outdir: Path, results: dict,
            full: bool = True, keep_work: bool = False):
    """转化单个 SWF。keep_work=True 保留中间产物(调试用); 默认转完即清(批量磁盘保护)"""
    name = swf.stem
    stem = re.sub(r'[^\w\-]', '_', name)  # 文件名安全化
    print(f"\n== {name} ==")
    raw = decompress(swf)
    tl = scan_timeline(raw)
    pages = detect_pages(tl)
    print(f"  时间轴: {tl['frames']}帧 @{tl['fps']:.0f}fps, 页边界: {pages}(整条模式不切片), "
          f"stop帧: {tl['stops']}, 标签: {tl['labels']}")

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

    results[stem] = dict(title=name, pages=n_pages)
    if not keep_work:
        _cleanup(workdir, stem)
    return n_pages


def main():
    ap = argparse.ArgumentParser(description='Flash 分页课件 → H5 分页视频')
    ap.add_argument('swfs', nargs='+', type=Path)
    ap.add_argument('-o', '--out', type=Path, default=Path('h5video'),
                    help='输出目录 (默认 ./h5video)')
    ap.add_argument('--paged', action='store_true',
                    help='按 Flash 翻页结构拆成多段(默认整条连播一条到底)')
    ap.add_argument('--keep-work', action='store_true',
                    help='保留中间产物(帧PNG等,调试用;默认转完即删,防止批量时磁盘爆满)')
    ap.add_argument('-w', '--work', type=Path, default=Path('swf2video_work'),
                    help='中间产物目录 (默认 ./swf2video_work)')
    args = ap.parse_args()

    results = {}
    for swf in args.swfs:
        if not swf.exists():
            print(f"跳过(不存在): {swf}"); continue
        try:
            convert(swf, args.work.resolve(), args.out.resolve(), results,
                    full=not args.paged, keep_work=args.keep_work)
        except Exception as e:
            print(f"✗ {swf}: {e}")
            if not args.keep_work:
                _cleanup(args.work.resolve(), re.sub(r'[^\w\-]', '_', swf.stem))

    if results:
        (args.out / 'index.html').write_text(
            INDEX_TMPL.replace('__COURSES__', json.dumps(results, ensure_ascii=False)),
            encoding='utf8')
        print(f"\n✓ 完成 {len(results)} 个课件 -> {args.out}/index.html")


if __name__ == '__main__':
    main()
