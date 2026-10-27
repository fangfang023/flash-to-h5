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


# ---------- 矩阵几何(2026-10-27 重写: 定位主源从模板匹配改为放置矩阵) ----------
# SWF MATRIX 语义(Flash 文档): 顶点变换 v' = M·v
#   | a b tx |     a = scaleX      b = rotateSkew0
#   | c d ty |     c = rotateSkew1 d = scaleY
#   | 0 0 1  |
# hasScale=false / hasRotate=false 表示"无该位"= 单位元(1 或 0), JPEXS XML 里
# 此时字段值打印为 0.0, 不能当真实缩放用。

def matrix_from_xml(el) -> tuple:
    """PlaceObject 元素下的 MATRIX → (a,b,c,d,e,f), twips。无矩阵时返回单位阵"""
    for cc in el.iter():
        if cc.get('type') == 'MATRIX':
            hs = cc.get('hasScale') == 'true'
            hr = cc.get('hasRotate') == 'true'
            a = float(cc.get('scaleX') or 0) if hs else 1.0
            d = float(cc.get('scaleY') or 0) if hs else 1.0
            b = float(cc.get('rotateSkew0') or 0) if hr else 0.0
            c = float(cc.get('rotateSkew1') or 0) if hr else 0.0
            return (a, b, c, d, int(cc.get('translateX') or 0), int(cc.get('translateY') or 0))
    return (1.0, 0.0, 0.0, 1.0, 0, 0)


def mul_m(A: tuple, B: tuple) -> tuple:
    """矩阵复合 C = A∘B (先应用 B 再应用 A)。世界矩阵 = mul_m(M_父, M_子)"""
    a1, b1, c1, d1, e1, f1 = A
    a2, b2, c2, d2, e2, f2 = B
    return (a1*a2 + c1*b2, b1*a2 + d1*b2,
            a1*c2 + c1*d2, b1*c2 + d1*d2,
            a1*e2 + c1*f2 + e1, b1*e2 + d1*f2 + f1)


def apply_m(M: tuple, x: float, y: float) -> tuple:
    a, b, c, d, e, f = M
    return (a*x + c*y + e, b*x + d*y + f)


def transform_bbox(M: tuple, bbox: tuple) -> tuple:
    """bbox=(x0,x1,y0,y1) 四角变换后的轴对齐包围盒 (均为 twips)"""
    x0, x1, y0, y1 = bbox
    pts = [apply_m(M, x, y) for x, y in ((x0,y0),(x1,y0),(x0,y1),(x1,y1))]
    xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
    return (min(xs), max(xs), min(ys), max(ys))


class SwfModel:
    """swf2xml 输出的结构化视图: 形状bounds + sprite定义 + 实例树"""

    def __init__(self, xml_path: Path):
        import xml.etree.ElementTree as ET
        self.root = ET.parse(xml_path).getroot()
        self.tags = self.root.find('tags')
        # shape/位图 bounds 库: charId -> (x0,x1,y0,y1) twips
        self.char_bounds = {}
        for it in self.tags:
            t = it.get('type') or ''
            cid = it.get('shapeId') or it.get('characterId')
            if t.startswith('DefineShape') and cid:
                for c in it.iter():
                    if c.get('type') == 'RECT':
                        self.char_bounds[int(cid)] = tuple(
                            int(c.get(k)) for k in ('Xmin', 'Xmax', 'Ymin', 'Ymax'))
                        break
            elif t.startswith('DefineBits') and cid:
                # 位图: JPEXS XML 不带宽高, 由导出PNG尺寸反填(fill_bitmap_bounds)
                pass
        # sprite 定义: spriteId -> subTags 元素
        self.sprites = {}
        for it in self.tags:
            if it.get('type') == 'DefineSpriteTag':
                self.sprites[int(it.get('spriteId'))] = it.find('subTags')
        # 按钮定义: buttonId -> 全部 BUTTONRECORD 的 (characterId, matrix) 列表
        # 注意: 只匹配 DefineButton/DefineButton2 — DefineButtonSoundTag 也前缀
        # 匹配 'DefineButton' 但无 BUTTONRECORD, 会用空列表覆盖真按钮(已踩坑)
        self.buttons = {}
        for it in self.tags:
            if (it.get('type') or '') in ('DefineButtonTag', 'DefineButton2Tag'):
                recs = []
                for c in it.iter('item'):
                    if c.get('type') == 'BUTTONRECORD':
                        pm = next((m for m in c.iter() if m.get('type') == 'MATRIX'), None)
                        M = (1.0, 0.0, 0.0, 1.0, 0, 0) if pm is None else matrix_from_xml(c)
                        recs.append((int(c.get('characterId') or 0), M))
                self.buttons[int(it.get('buttonId'))] = recs

    def fill_bitmap_bounds(self, sprites_dir: Path):
        """用导出的 DefineBits PNG 尺寸补位图 bounds(原点=位图左上(0,0))"""
        from PIL import Image
        for it in self.tags:
            t = it.get('type') or ''
            cid = it.get('characterId')
            if t.startswith('DefineBits') and cid:
                cid = int(cid)
                if cid in self.char_bounds:
                    continue
                # JPEXS 导出图像目录里按 charId 命名
                for f in sprites_dir.parent.glob(f'**/{cid}.*'):
                    try:
                        w, h = Image.open(f).size
                        self.char_bounds[cid] = (0, w*20, 0, h*20)
                        break
                    except Exception:
                        pass

    def sprite_placements(self, sprite_id: int):
        """sprite 内全部 PlaceObject 元素列表(文档序, 含逐帧 move)"""
        sub = self.sprites.get(sprite_id)
        return [el for el in (sub or [])
                if (el.get('type') or '').startswith('PlaceObject')]

    def union_bounds(self, sid: int, _depth: int = 0) -> tuple:
        """全帧并集 bounds (twips, 相对注册点)。支持 sprite / button。
        对内部每个放置的全部矩阵(逐帧 move 会产生多条)×内容bounds
        取四角包络, 递归嵌套 sprite。已验证与 JPEXS 导出 canvas 吻合到亚像素。
        """
        if _depth > 8:
            return None
        if sid in self.buttons:      # 按钮: BUTTONRECORD 的字符bounds并集
            env = []
            for cid, M in self.buttons[sid]:
                b = self.char_bounds.get(cid) or self.union_bounds(cid, _depth + 1)
                if b:
                    env.append(transform_bbox(M, b))
            if not env:
                return None
            return (min(b[0] for b in env), max(b[1] for b in env),
                    min(b[2] for b in env), max(b[3] for b in env))
        if sid not in self.sprites:
            return None
        env = []
        for el in self.sprite_placements(sid):
            cid = int(el.get('characterId') or 0)
            if not cid:
                continue
            b = self.char_bounds.get(cid)
            if b is None:
                b = self.union_bounds(cid, _depth + 1)
            if not b:
                continue
            for m in self._all_matrices(el):
                env.append(transform_bbox(m, b))
        if not env:
            return None
        return (min(b[0] for b in env), max(b[1] for b in env),
                min(b[2] for b in env), max(b[3] for b in env))

    def _all_matrices(self, el) -> list:
        """该 PlaceObject 的全部矩阵。注意: 逐帧动画以多个 PlaceObject(move=true)
        出现, 每个 PO 只带一个 MATRIX — 同层多 PO 由调用方(sprite_placements)枚举,
        此处只处理单个 PO 内嵌套多重矩阵的罕见情况。"""
        out = []
        for c in el.iter():
            if c.get('type') == 'MATRIX':
                out.append(matrix_from_xml(el))
                break
        return out or [(1.0, 0.0, 0.0, 1.0, 0, 0)]

    def frame1_instances(self, sprite_id: int, parent_M: tuple, stage_W: int, stage_H: int):
        """递归枚举 sprite 首帧显示列表里的"叶子实例"(多帧装饰动画元件)。
        返回 [{cid, depth, name, M(世界), anim(bool: frameCount>1)}]
        只走首帧(首个 ShowFrameTag 之前)的放置。"""
        out = []
        sub = self.sprites.get(sprite_id)
        if sub is None:
            return out
        for el in sub:
            t = el.get('type') or ''
            if t == 'ShowFrameTag':
                break
            if not t.startswith('PlaceObject'):
                continue
            cid = int(el.get('characterId') or 0)
            if not cid:
                continue
            M = mul_m(parent_M, matrix_from_xml(el))
            child = self.sprites.get(cid)
            if child is None:
                continue  # shape/位图/按钮 — 叶子, 非动画实例
            fc = self._sprite_frame_count(cid)
            name = el.get('name') or ''
            if fc > 1:
                out.append(dict(cid=cid, depth=int(el.get('depth') or 0),
                                name=name, M=M, frame_count=fc))
            else:
                # 单帧容器 sprite: 递归下钻(场景容器如 sprite34)
                out.extend(self.frame1_instances(cid, M, stage_W, stage_H))
        return out

    def main_instances(self):
        """主时间轴首帧显示列表里的动画实例清单(递归下钻单帧容器如场景sprite34)。
        返回 [{cid, depth, name, M(世界矩阵), frame_count}]"""
        MAIN = (1.0, 0.0, 0.0, 1.0, 0, 0)
        out = []
        for el in self.tags:
            t = el.get('type') or ''
            if t == 'ShowFrameTag':
                break
            if not t.startswith('PlaceObject'):
                continue
            cid = int(el.get('characterId') or 0)
            if not cid or cid not in self.sprites:
                continue
            M = mul_m(MAIN, matrix_from_xml(el))
            fc = self._sprite_frame_count(cid)
            if fc > 1:
                out.append(dict(cid=cid, depth=int(el.get('depth') or 0),
                                name=el.get('name') or '', M=M, frame_count=fc))
            else:
                out.extend(self.frame1_instances(cid, M, 0, 0))
        return out

    def _sprite_frame_count(self, sid: int) -> int:
        for it in self.tags:
            if it.get('type') == 'DefineSpriteTag' and int(it.get('spriteId')) == sid:
                return int(it.get('frameCount') or 1)
        return 1


def placements_via_xml(swf: Path, workdir: Path):
    """主时间轴命名对象(按钮/zmt)的放置矩阵 — 旧接口保留给热区定位。
    返回 (named: {name: (cid, tx, ty)}, model: SwfModel)"""
    workdir.mkdir(parents=True, exist_ok=True)
    xml_path = workdir / (swf.stem + '.xml')
    subprocess.run(['java', '-jar', str(FFDEC), '-swf2xml', swf.name, xml_path.resolve()],
                   cwd=swf.parent, check=True, capture_output=True)
    model = SwfModel(xml_path)
    named = {}
    for el in model.tags:
        if (el.get('type') or '').startswith('PlaceObject') and el.get('name'):
            a, b, c, d, e, f = matrix_from_xml(el)
            named[el.get('name')] = (int(el.get('characterId') or 0), e, f)
    return named, model


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


# ---------- 动画层与干净底图 ----------

def find_anim_sprites(workdir: Path):
    """识别多帧动画精灵(内部时间轴循环): [(spriteId, frameCount)]"""
    import xml.etree.ElementTree as ET
    xml_path = workdir / (workdir.parent / 'card.xml').name
    # swf2xml输出到上一轮已有路径; 此处重新生成
    return None


def make_clean_bg(swf: Path, tmp: Path, anim_ids: set) -> Path:
    """XML回写法生成无动画元件的干净底图:
    swf2xml -> 删除动画元件的PlaceObject -> xml2swf -> 渲染首帧"""
    import re as _re
    xml = tmp / 'card.xml'
    subprocess.run(['java', '-jar', str(FFDEC), '-swf2xml', swf.name, xml.resolve()],
                   cwd=swf.parent, check=True, capture_output=True)
    src = xml.read_text(encoding='utf8')
    ids_alt = '|'.join(str(i) for i in anim_ids)
    pat = _re.compile(
        r'<item type="PlaceObject[23]Tag"[^>]*characterId="(' + ids_alt + r')"[^>]*>.*?</item>', _re.S)
    n = len(pat.findall(src))
    src = pat.sub('', src)
    xml.write_text(src, encoding='utf8')
    clean_swf = tmp / 'card_clean.swf'
    subprocess.run(['java', '-jar', str(FFDEC), '-xml2swf', xml.name, clean_swf.resolve()],
                   cwd=tmp, check=True, capture_output=True)
    out = tmp / 'frames_clean'
    subprocess.run(['java', '-jar', str(FFDEC), '-export', 'frame', out.resolve(),
                    clean_swf.name], cwd=tmp, check=True, capture_output=True)
    print(f"  干净底图: 已删除{n}条动画放置")
    return next(out.glob('*.png'))


def build_anim_layer(swf: Path, tmp: Path, out_dir: Path, fps: float, exclude: set = None):
    """完整动画层管线: 识别动画精灵 -> 网格sheet(帧率精确) -> 模板匹配定位 -> anims.json"""
    from PIL import Image
    import numpy as np
    # 动画精灵识别: 用XML的DefineSprite frameCount>1 且被放置过
    xml = tmp / 'card2.xml'
    subprocess.run(['java', '-jar', str(FFDEC), '-swf2xml', swf.name, xml.resolve()],
                   cwd=swf.parent, check=True, capture_output=True)
    import xml.etree.ElementTree as ET
    root = ET.parse(xml).getroot()
    placed = set()
    for it in root.iter('item'):
        if it.get('type') in ('PlaceObject2Tag', 'PlaceObject3Tag') and it.get('characterId'):
            placed.add(int(it.get('characterId')))
    anims_def = {}
    for it in root.iter('item'):
        if it.get('type') == 'DefineSpriteTag':
            fc = int(it.get('frameCount') or 1)
            sid = int(it.get('spriteId'))
            if fc > 1 and sid in placed and fc > 2 and sid not in (exclude or set()):
                anims_def[sid] = fc
    # 内容精灵zm(由按钮控制)不算装饰动画 — 排除被AS引用gotoAndStop的
    print(f"  动画精灵: {anims_def}")

    # 导出sprite帧
    sp_dir = tmp / 'sprites'
    subprocess.run(['java', '-jar', str(FFDEC), '-export', 'sprite', sp_dir.resolve(), swf.name],
                   cwd=swf.parent, check=True, capture_output=True)

    # 生成网格sheet + hash命名
    import hashlib, os as _os
    _os.makedirs(out_dir / 'anim', exist_ok=True)
    sheet_names = {}
    for sid, n in anims_def.items():
        frames = []
        d = sp_dir / f'DefineSprite_{sid}'
        if not d.exists():
            continue
        for i in range(1, n+1):
            f = d / f'{i}.png'
            if f.exists():
                frames.append(Image.open(f).convert('RGBA'))
        if len(frames) < 3:
            continue
        w = max(im.width for im in frames)
        h = max(im.height for im in frames)
        cols = min(10, len(frames))
        rows = (len(frames) + cols - 1) // cols
        sheet = Image.new('RGBA', (w*cols, h*rows), (0,0,0,0))
        for i, im in enumerate(frames):
            sheet.paste(im, ((i%cols)*w, (i//cols)*h), im)
        hsh = hashlib.md5(sheet.tobytes()).hexdigest()[:8]
        name = f'anim_{sid}_{hsh}.png'
        sheet.save(out_dir / 'anim' / name, optimize=True)
        sheet_names[sid] = name
        globals().setdefault('_SHEET_META', {})[sid] = dict(
            n=len(frames), cols=cols, rows=rows, fw=w, fh=h,
            dur=round(len(frames)/fps, 3))
    # 清理不在sheet_names里的旧sheet
    import os as _os2
    for f in (out_dir / 'anim').iterdir():
        keep = f.name in sheet_names.values()
        if not keep and f.name.startswith('anim_'):
            f.unlink()
    return sheet_names, globals().get('_SHEET_META', {})


def bake_rotated_sheet(base_sheet: Path, angle_deg: float, out_dir: Path,
                       cid: int, depth: int, n: int, cols: int, rows: int,
                       fps: float) -> str:
    """旋转实例预烘焙: 把基础sheet每帧按 angle_deg(PIL逆时针为正) 旋转,
    生成该实例专属 sheet。div 用旋转后的世界包围盒定位, sheet 旋转后恰好填满。
    返回文件名。"""
    from PIL import Image
    import hashlib
    im = Image.open(base_sheet).convert('RGBA')
    fw, fh = im.width // cols, im.height // rows
    # 每帧旋转后取最大包围尺寸, 画布放大居中
    rw = int(fw * abs(_cos(angle_deg)) + fh * abs(_sin(angle_deg))) + 2
    rh = int(fw * abs(_sin(angle_deg)) + fh * abs(_cos(angle_deg))) + 2
    sheet = Image.new('RGBA', (rw * cols, rh * rows), (0, 0, 0, 0))
    for i in range(n):
        c, r = i % cols, i // cols
        cell = im.crop((c * fw, r * fh, (c + 1) * fw, (r + 1) * fh))
        rot = cell.rotate(angle_deg, resample=Image.BICUBIC, expand=True)
        # 居中贴入
        ox = (rw - rot.width) // 2
        oy = (rh - rot.height) // 2
        sheet.paste(rot, (c * rw + ox, r * rh + oy), rot)
    hsh = hashlib.md5(sheet.tobytes()).hexdigest()[:8]
    name = f'anim_{cid}d{depth}_{hsh}.png'
    sheet.save(out_dir / 'anim' / name, optimize=True)
    return name


def _cos(x):
    import math
    return math.cos(math.radians(x))


def _sin(x):
    import math
    return math.sin(math.radians(x))


def _pixel_align_baked(card_dir: Path, baked_name: str, tmp: Path, rec: dict,
                       radius: int = 40):
    """旋转实例烘焙后的最终对齐: 烘焙帧0(缩放到div尺寸)作为模板,
    在原SWF首帧渲染图的 (x,y)±radius 邻域内找差异最小的偏移。
    返回 (x, y) 或 None。原渲染图来自 tmp/main/*.png(首帧含全部动画)。"""
    from PIL import Image
    import numpy as np
    main_dir = tmp / 'main'
    pngs = list(main_dir.glob('*.png'))
    if not pngs:
        return None
    bg = np.array(Image.open(pngs[0]).convert('RGB')).astype(int)
    sheet = Image.open(card_dir / 'anim' / baked_name)
    cw = sheet.width // rec['cols']
    ch = sheet.height // rec['rows']
    cell0 = sheet.crop((0, 0, cw, ch)).convert('RGBA').resize(
        (max(1, int(round(rec['w']))), max(1, int(round(rec['h'])))))
    arr = np.array(cell0)
    mask = arr[:, :, 3] > 200
    if mask.sum() < 30:
        return None
    rgb = arr[:, :, :3].astype(int)
    h, w = mask.shape
    best = (1e18, None)
    X0, Y0 = int(rec['x']), int(rec['y'])
    for Y in range(max(0, Y0 - radius), min(bg.shape[0] - h, Y0 + radius)):
        for X in range(max(0, X0 - radius), min(bg.shape[1] - w, X0 + radius)):
            s = np.abs(bg[Y:Y + h, X:X + w] - rgb)[mask].mean()
            if s < best[0]:
                best = (s, (X, Y))
    if best[1] is None or best[0] > 40:   # 匹配太差视作失败
        return None
    return best[1]


def locate_anims(bg_png: Path, tmp: Path, sheet_names: dict, meta: dict) -> dict:
    """[已废弃 2026-10-27] 模板匹配定位 — 精度差(实测偏差最大55px)、丢多实例、
    镜像靠猜。保留仅作矩阵路线的最后兜底。新代码走 locate_anims_by_matrix()。"""
    from PIL import Image
    import numpy as np
    import xml.etree.ElementTree as ET
    bg = np.array(Image.open(bg_png).convert('RGB')).astype(int)
    BH, BW = bg.shape[:2]
    positions = {}
    sp_dir = tmp / 'sprites'
    for sid in sheet_names:
        f1 = sp_dir / f'DefineSprite_{sid}' / '1.png'
        if not f1.exists():
            continue
        im = Image.open(f1).convert('RGBA')
        # 尝试多个scale(放置矩阵解析不可靠,实测匹配更稳)
        best = None
        for sx in [1.0, 0.7, 0.5, 0.3, 0.25, 0.2, 0.15]:
            w2, h2 = int(im.width*sx), int(im.height*sx)
            if w2 >= BW or h2 >= BH or w2 < 8:
                continue
            im2 = im.resize((w2, h2))
            arr = np.array(im2)
            mask = arr[:,:,3] > 200
            if mask.sum() < 50:
                continue
            rgb = arr[:,:,:3].astype(int)
            for y in range(0, BH-h2, 2):
                for x in range(0, BW-w2, 2):
                    s = np.abs(bg[y:y+h2, x:x+w2] - rgb)[mask].mean()
                    if best is None or s < best[0]:
                        best = (s, x, y, w2, h2)
            if best and best[0] < 8:   # 足够好就停(省时)
                break
        if best:
            s, x, y, w, h = best
            positions[sid] = dict(x=x, y=y, w=w, h=h)
            print(f"  动画{sid}: ({x},{y}) {w}x{h} 差异{s:.1f}")
    return positions


# ---------- 矩阵定位 + solo渲染对账(2026-10-27 新增) ----------

def locate_anims_by_matrix(model: 'SwfModel', swf_xml: Path, tmp: Path,
                           anim_cids: set, sprites_dir: Path,
                           verify: bool = True, card_dir: Path = None,
                           sheet_names: dict = None, meta: dict = None,
                           fps: float = 25.0) -> tuple:
    """矩阵链定位每个动画实例(多实例全量还原)。
    返回 (instances, issues): instances = [{cid, depth, x, y, w, h, flip, rotate,
    img, n, cols, rows, dur}] — 旋转实例img为预烘焙专属sheet。
    坐标px(舞台系); issues = 验证告警列表。
    verify=True 时对每个实例做 solo 渲染对账(无旋转阈值3px, 旋转实例直接
    用渲染值标定 — JPEXS对旋转内容的canvas取整语义未完全对齐,渲染即真值)。"""
    from PIL import Image
    import numpy as np
    insts = []
    issues = []
    paths = _instance_paths(model)   # (cid,depth) -> 容器路径(供solo渲染剪枝)
    for inst in model.main_instances():
        cid = inst['cid']
        if cid not in anim_cids:
            continue
        M = inst['M']
        ub = model.union_bounds(cid)
        if not ub:
            issues.append(f"cid{cid} 无法计算bounds(无shape?) — 跳过")
            continue
        bb = transform_bbox(M, ub)   # twips, 舞台系
        x, y = bb[0] / 20, bb[2] / 20
        w, h = (bb[1] - bb[0]) / 20, (bb[3] - bb[2]) / 20
        flip = M[0] < 0
        rotate = (M[1] != 0 or M[2] != 0)
        rec = dict(cid=cid, depth=inst['depth'], x=x, y=y, w=w, h=h,
                   flip=flip, rotate=rotate)
        # sheet信息(默认共享基础sheet)
        m = (meta or {}).get(cid) or {}
        rec.update(img=(sheet_names or {}).get(cid), n=m.get('n'),
                   cols=m.get('cols'), rows=m.get('rows'), dur=m.get('dur'))
        # solo渲染对账(带缓存, 批量重跑不重复渲染)
        if verify:
            tag = f'{cid}_{inst["depth"]}'
            truth = _solo_cache_load(tmp, tag)
            if truth is None:
                truth = _render_with_path(model, swf_xml, tmp, tag,
                                          paths.get((cid, inst['depth'])))
                if truth:
                    _solo_cache_save(tmp, tag, truth)
            if truth:
                f1 = sprites_dir / f'DefineSprite_{cid}' / '1.png'
                if f1.exists():
                    im_s = Image.open(f1).convert('RGBA')
                    ys2, xs2 = np.where(np.array(im_s)[:, :, 3] > 10)
                    sxa, sya = abs(M[0]) or 1.0, abs(M[3]) or 1.0
                    cx = truth['x'] - xs2.min() * sxa
                    cy = truth['y'] - ys2.min() * sya
                    dx, dy = cx - x, cy - y
                    if rotate and (abs(dx) > 3 or abs(dy) > 3):
                        # 旋转实例: 渲染值即真值(canvas含JPEXS取整语义)
                        rec['x'], rec['y'] = cx, cy
                        issues.append(f"cid{cid}d{inst['depth']} 旋转实例用渲染值标定(Δ{dx:+.0f},{dy:+.0f}px)")
                    elif abs(dx) > 3 or abs(dy) > 3:
                        issues.append(f"⚠ cid{cid}d{inst['depth']} 矩阵({x:.0f},{y:.0f}) vs 渲染({cx:.0f},{cy:.0f}) Δ=({dx:+.0f},{dy:+.0f})")
        # 旋转实例: 预烘焙专属sheet(帧内容旋转后恰好填满div包围盒)
        if rotate and card_dir is not None and rec.get('img') and rec.get('n'):
            import math
            # PIL逆时针为正; Flash y向下 → 视觉角 = -atan2(b, a)
            angle = math.degrees(math.atan2(M[1], M[0]))
            base = card_dir / 'anim' / rec['img']
            baked = bake_rotated_sheet(
                base, -angle, card_dir, cid, inst['depth'],
                rec['n'], rec['cols'], rec['rows'], fps)
            rec['img'] = baked
            # 烘焙后像素对齐: 用烘焙帧0(缩放到div尺寸)在SWF首帧渲染图
            # 的理论位置±40px邻域找最优offset — 消除旋转canvas取整误差
            aligned = _pixel_align_baked(card_dir, baked, tmp, rec)
            if aligned:
                rec['x'], rec['y'] = aligned
                issues.append(f"cid{cid}d{inst['depth']} 旋转sheet {baked} ({angle:.1f}°) 像素对齐→({aligned[0]:.0f},{aligned[1]:.0f})")
            else:
                issues.append(f"cid{cid}d{inst['depth']} 预烘焙旋转sheet {baked} ({angle:.1f}°) 对齐失败,保留标定值")
        insts.append(rec)
        print(f"  实例 cid{cid} d{inst['depth']}: ({rec['x']:.1f},{rec['y']:.1f}) {rec['w']:.1f}x{rec['h']:.1f}"
              + (" [镜像]" if flip else "") + (" [旋转]" if rotate else ""))
    # 合理性断言(原R1保留; R2启发式已随模板匹配一起废弃)
    for r in insts:
        if r['x'] < -2 or r['y'] < -2 or r['x'] + r['w'] > 722 or r['y'] + r['h'] > 578:
            issues.append(f"R1实例cid{r['cid']}d{r['depth']}越界: ({r['x']:.0f},{r['y']:.0f})+{r['w']:.0f}x{r['h']:.0f}")
    return insts, issues


def _instance_paths(model: 'SwfModel') -> dict:
    """(cid, depth) -> [(spriteId, depth), ...] 主时间轴到实例的容器路径"""
    out = {}

    def walk(sprite_id, M, prefix):
        sub = model.sprites.get(sprite_id)
        if sub is None:
            return
        for el in sub:
            t = el.get('type') or ''
            if t == 'ShowFrameTag':
                break
            if not t.startswith('PlaceObject'):
                continue
            cid = int(el.get('characterId') or 0)
            if not cid:
                continue
            depth = int(el.get('depth') or 0)
            if cid in model.sprites:
                fc = model._sprite_frame_count(cid)
                if fc > 1:
                    out[(cid, depth)] = prefix + [(sprite_id, depth)]
                else:
                    walk(cid, None, prefix + [(sprite_id, depth)])

    # 主时间轴: 遍历 tags 首帧, 单帧容器下钻
    for el in model.tags:
        t = el.get('type') or ''
        if t == 'ShowFrameTag':
            break
        if not t.startswith('PlaceObject'):
            continue
        cid = int(el.get('characterId') or 0)
        if not cid or cid not in model.sprites:
            continue
        depth = int(el.get('depth') or 0)
        if model._sprite_frame_count(cid) > 1:
            out[(cid, depth)] = [('MAIN', depth)]
        else:
            walk(cid, None, [('MAIN', depth)])
    return out


def _render_with_path(model, swf_xml: Path, tmp: Path, tag: str, path):
    """按容器路径剪XML渲染solo实例。path=[('MAIN',d),(spriteId,d),...]
    思路: 每一层只保留"通往目标"的那一个放置, 其余全删 → SWF里只剩该实例链。"""
    import xml.etree.ElementTree as ET
    from PIL import Image
    import numpy as np
    if not path:
        return None
    work = tmp / f'solo_{tag}'
    work.mkdir(parents=True, exist_ok=True)
    tree = ET.parse(swf_xml)
    tags = tree.getroot().find('tags')
    # 1. 主时间轴: 删首帧全部PO + 首个ShowFrame后的所有tag(只留单帧)
    first_sf = next(i for i, el in enumerate(tags) if el.get('type') == 'ShowFrameTag')
    main_pos = [el for el in list(tags)[:first_sf]
                if (el.get('type') or '').startswith('PlaceObject')]
    for el in main_pos:
        tags.remove(el)
    for el in list(tags)[first_sf:]:
        tags.remove(el)
    # 残留的顶层 ShowFrame(首个SF本身)也要删, 否则PO会落到第2帧 → 渲染空帧
    for el in list(tags):
        if el.get('type') == 'ShowFrameTag':
            tags.remove(el)
    # 放回第一跳的目标PO(主时间轴 → 首容器)
    first_depth = str(path[0][1])
    first_po = next((el for el in main_pos if el.get('depth') == first_depth), None)
    if first_po is None:
        return None
    tags.append(first_po)
    tags.append(ET.Element('item', {'type': 'ShowFrameTag'}))
    # 2. 每个容器层: 只留通往下一跳的放置
    #    path 元素 = (所在容器, depth)。('MAIN',1)=主时间轴放sprite34(步骤1已放回);
    #    (34,118)=sprite34内放目标。对每个非MAIN容器, 剪掉其内部非目标depth的PO。
    for owner_sid, dep in path:
        if owner_sid == 'MAIN':
            continue
        sp = next((el for el in tags if el.get('type') == 'DefineSpriteTag'
                   and el.get('spriteId') == str(owner_sid)), None)
        if sp is None:
            return None
        sub = sp.find('subTags')
        for el in list(sub):
            if (el.get('type') or '').startswith('PlaceObject') and el.get('depth') != str(dep):
                sub.remove(el)
    # 3. 渲染
    xml_out = work / 'solo.xml'
    tree.write(xml_out, encoding='utf-8')
    swf_out = work / 'solo.swf'
    r = subprocess.run(['java', '-jar', str(FFDEC), '-xml2swf',
                        xml_out.name, swf_out.resolve()], cwd=work,
                       capture_output=True, text=True)
    if r.returncode != 0 or not swf_out.exists() or swf_out.stat().st_size < 100:
        return None
    fr = work / 'frames'
    subprocess.run(['java', '-jar', str(FFDEC), '-ignorebackground', '-export', 'frame',
                    fr.resolve(), swf_out.name], cwd=work, capture_output=True)
    pngs = list(fr.glob('*.png'))
    if not pngs:
        return None
    im = Image.open(pngs[0]).convert('RGBA')
    a = np.array(im)[:, :, 3]
    ys, xs = np.where(a > 10)
    if not len(xs):
        return None
    return dict(x=int(xs.min()), y=int(ys.min()),
                w=int(xs.max() - xs.min() + 1), h=int(ys.max() - ys.min() + 1))


def _solo_cache_load(tmp: Path, tag: str):
    import json
    f = tmp / f'solo_{tag}' / 'truth.json'
    if f.exists():
        import json as _j
        return _j.loads(f.read_text())
    return None


def _solo_cache_save(tmp: Path, tag: str, data: dict):
    import json
    d = tmp / f'solo_{tag}'
    d.mkdir(parents=True, exist_ok=True)
    (d / 'truth.json').write_text(json.dumps(data))


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

    # 3. 定位内容精灵: zmt(外) -> zm(内)。
    named, model = placements_via_xml(swf, tmp)
    if 'zmt' not in named:
        raise RuntimeError("主时间轴未找到 zmt 精灵")
    zmt_id = named['zmt'][0]
    # zm = zmt 内部首个放置的精灵(内容面板本体)
    zm_id = zmt_id
    for el in model.sprite_placements(zmt_id):
        cid = int(el.get('characterId') or 0)
        if cid and cid in model.sprites:
            zm_id = cid
            break

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

    # 5. 面板定位: 矩阵链(zmt世界矩阵 ∘ zm局部矩阵 × zm的bounds)。
    #    zmt在主时间轴named里(tx,ty); zm在zmt内部首帧放置。
    from PIL import Image
    zmt_M = None
    for el in model.tags:
        if (el.get('type') or '').startswith('PlaceObject') and el.get('name') == 'zmt':
            zmt_M = matrix_from_xml(el)
            break
    zm_M = zmt_M
    for el in model.sprite_placements(zmt_id):
        if int(el.get('characterId') or 0) == zm_id:
            zm_M = mul_m(zmt_M, matrix_from_xml(el))
            break
    zm_b = model.union_bounds(zm_id) or (0, 0, 0, 0)
    pbb = transform_bbox(zm_M, zm_b)
    px, py = pbb[0] / 20, pbb[2] / 20
    probe_w = (pbb[1] - pbb[0]) / 20
    print(f"  面板位置(矩阵): ({px:.1f},{py:.1f}) 宽{probe_w:.1f}px")

    # 6. 整理素材(底图=干净版: 删除动画元件的放置后重渲染; 动画层由JS叠加)
    try:
        import struct as _st
        # SWF真实帧率
        nbits = raw[8] >> 3
        hpos = 8 + (5 + nbits*4 + 7)//8
        fps = _st.unpack('<H', raw[hpos:hpos+2])[0] / 256
        # 识别动画精灵(多帧且被放置) — 排除内容面板系(zmt/zm)
        sheet_names, meta = build_anim_layer(swf, tmp, card_dir, fps, exclude={zm_id, zmt_id})
        # 动画实例定位: 矩阵链(多实例全量还原) + solo渲染对账 + 旋转预烘焙
        if sheet_names:
            xml_path = tmp / (swf.stem + '.xml')
            if not xml_path.exists():
                subprocess.run(['java', '-jar', str(FFDEC), '-swf2xml', swf.name,
                                xml_path.resolve()], cwd=swf.parent, check=True, capture_output=True)
            insts, issues = locate_anims_by_matrix(
                model, xml_path, tmp, set(sheet_names.keys()), tmp / 'sprites',
                verify=True, card_dir=card_dir, sheet_names=sheet_names,
                meta=meta, fps=fps)
            globals()['_ANIM_INSTANCES'] = insts
            for msg in issues:
                print(f"  · {msg}")
        if sheet_names:
            anim_ids = set(sheet_names.keys())
            clean_bg = make_clean_bg(swf, tmp, anim_ids)
            import hashlib as _hl
            h = _hl.md5(clean_bg.read_bytes()).hexdigest()[:8]
            (card_dir / f'bg_clean_{h}.png').write_bytes(clean_bg.read_bytes())
            globals()['_CLEAN_BG'] = f'bg_clean_{h}.png'
            globals()['_ANIM_META'] = meta
            globals()['_SHEET_NAMES'] = sheet_names
            globals()['_FPS'] = fps
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"  动画层生成跳过({e}) — 回退为静态bg")
        globals()['_CLEAN_BG'] = None
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

    # 7. 生成 HTML(热区 = 按钮元件bounds × 世界矩阵, 不再写死14%x13%)
    hots = []
    for bt in sorted(bmap, key=lambda b: int(b[2:])):
        n = int(bt[2:]) - 1
        lab = labels[n] if n < len(labels) else f'panel{n}'
        cid, tx, ty = named.get(bt, (0, 0, 0))
        # 找该按钮的放置元素取全矩阵, 用按钮bounds算真实热区矩形
        hot_style = None
        for el in model.tags:
            if (el.get('type') or '').startswith('PlaceObject') and el.get('name') == bt:
                M = matrix_from_xml(el)
                bb = model.union_bounds(int(el.get('characterId') or 0))
                if bb:
                    w = transform_bbox(M, bb)
                    hx, hy = w[0]/20/W*100, w[2]/20/H*100
                    hw, hh = (w[1]-w[0])/20/W*100, (w[3]-w[2])/20/H*100
                    hot_style = f'left:{hx:.1f}%;top:{hy:.1f}%;width:{hw:.1f}%;height:{hh:.1f}%'
                break
        if hot_style is None:
            bx, by = tx / 20 / W * 100, ty / 20 / H * 100
            hot_style = f'left:{bx:.1f}%;top:{by:.1f}%;width:14%;height:13%'
        hots.append(
            f'  <div class="hot" data-panel="panel_{lab}.png" '
            f'style="{hot_style}" title="{lab}"></div>')
    panel_left = px / W * 100
    panel_top = py / H * 100
    panel_w = probe_w / W * 100

    # 动画层JS(若有) — 实例清单版: 每实例自带sheet信息(旋转实例为预烘焙sheet)
    anim_js = ''
    clean_bg = globals().get('_CLEAN_BG')
    inst_recs = globals().get('_ANIM_INSTANCES') or []
    if clean_bg and inst_recs:
        import json as _json
        js_insts = []
        for rec in inst_recs:
            if not rec.get('img') or not rec.get('n'):
                continue
            js_insts.append(dict(
                img=rec['img'], n=rec['n'], cols=rec['cols'], rows=rec['rows'],
                dur=rec['dur'],
                x=round(rec['x'], 1), y=round(rec['y'], 1),
                w=round(rec['w'], 1), h=round(rec['h'], 1),
                flip=bool(rec.get('flip'))))
        anim_js = (
            "const ANIMS = " + _json.dumps(js_insts, ensure_ascii=False) + ";\n"
            "const layer = document.getElementById('animLayer');\n"
            "ANIMS.forEach(a => {\n"
            "  const d = document.createElement('div'); d.className = 'anim';\n"
            "  d.style.left = (a.x/" + f"{W:.0f}" + "*100)+'%'; d.style.top = (a.y/" + f"{H:.0f}" + "*100)+'%';\n"
            "  d.style.width = (a.w/" + f"{W:.0f}" + "*100)+'%'; d.style.height = (a.h/" + f"{H:.0f}" + "*100)+'%';\n"
            "  d.style.backgroundImage = `url(anim/${a.img})`;\n"
            "  if (a.flip) d.style.transform = 'scaleX(-1)';\n"
            "  const applySize = () => { const r = d.getBoundingClientRect();\n"
            "    if (r.width > 0) d.style.backgroundSize = `${r.width*a.cols}px ${r.height*a.rows}px`; };\n"
            "  applySize(); window.addEventListener('resize', applySize);\n"
            "  layer.appendChild(d);\n"
            "  let f = 0, last = performance.now();\n"
            "  const fd = a.dur*1000/a.n;\n"
            "  function tick(now){ if(now-last>=fd){\n"
            "    f=(f+Math.floor((now-last)/fd))%a.n; last=now;\n"
            "    const c=f%a.cols, r=Math.floor(f/a.cols);\n"
            "    const rr = d.getBoundingClientRect();\n"
            "    if (rr.width > 0) {\n"
            "      d.style.backgroundSize = `${rr.width*a.cols}px ${rr.height*a.rows}px`;\n"
            "      d.style.backgroundPosition = `${-c*rr.width}px ${-r*rr.height}px`;\n"
            "    }\n"
            "  } requestAnimationFrame(tick); }\n"
            "  requestAnimationFrame(tick);\n"
            "});\n"
            "document.querySelector('img.bg').src = '" + clean_bg + "';")

    html = HTML_TMPL.replace('__TITLE__', f'{name} · 汉字学习卡') \
                    .replace('__HOTS__', '\n'.join(hots)) \
                    .replace('__PANEL_STYLE__',
                             f'left:{panel_left:.2f}%;top:{panel_top:.2f}%;width:{panel_w:.2f}%') \
                    .replace('__AUDIO__', audio_html) \
                    .replace('__ANIM_LAYER__', anim_js)
    (card_dir / 'index.html').write_text(html, encoding='utf8')
    # anims.json 也同步一份(调试/验收用)
    if inst_recs:
        import json as _j2
        (card_dir / 'anims.json').write_text(
            _j2.dumps([{k: v for k, v in r.items()
                        if k in ('cid', 'depth', 'img', 'x', 'y', 'w', 'h',
                                 'flip', 'rotate', 'n', 'cols', 'rows', 'dur')}
                       for r in inst_recs],
                      ensure_ascii=False, indent=1),
            encoding='utf8')

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
  .card { position:relative; width:min(96vw,720px); user-select:none; overflow:hidden; }
  .card img.bg { display:block; width:100%; }
  .anim { position:absolute; background-repeat:no-repeat; pointer-events:none;
          z-index:1; animation: play linear infinite; }
  .hot { position:absolute; cursor:pointer; z-index:2; border-radius:50%;
         transition:transform .12s, filter .12s; }
  .hot:hover { transform:scale(1.07); filter:brightness(1.12); }
  .panel { position:absolute; __PANEL_STYLE__; pointer-events:none; }
  .panel img { width:100%; display:block; }
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
  <div class="panel" id="panel" style="z-index:3"><img id="panelImg" src="" alt=""></div>
  <div id="animLayer"></div>
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
// ===== 装饰动画层(自动生成) =====
__ANIM_LAYER__
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
