#!/usr/bin/env python3
"""
swf2quiz.py — 判题/填空课件型 SWF → H5 交互页 批量转化工具

适用对象: "答题课件" 型 SWF（特征: AS3 + 同目录 data.xml(GBK) 题目数据 + pic/ 题图。
两种题型:
  - judge  判断题: data.xml <top txt=".." answer="right|wrong">
  - fill   填空题: data.xml <top txt="..(_).."><answer>文本</answer></top>

判别: 解析 data.xml —— 有 answer 属性=judge; 有 <answer> 子节点=fill。

流程: 解析data.xml → JPEXS导出UI素材(背景/按钮/对错标记) → 生成 questions.js + index.html

用法:
  python3 swf2quiz.py <课件目录或swf路径> ... [-o 输出目录]
  课件目录结构(与原Flash一致): xxx.swf data.xml pic/

依赖: java+JPEXS(../tools/ffdec), Pillow(裁剪验证可省)
"""
import argparse, json, re, shutil, subprocess, sys, tempfile, xml.etree.ElementTree as ET
from pathlib import Path

FFDEC = Path(__file__).resolve().parent / "ffdec/ffdec.jar"
TOOLDIR = Path(__file__).resolve().parent
TPL_DIR = TOOLDIR.parent / "output/h5quiz"   # 模板目录(我是小法官/填空乐园)

# 两个模板的关键差异片段,批量生成时按题型套用
def detect_type(xml_path: Path):
    data = xml_path.read_bytes().decode('gbk', errors='replace')
    root = ET.fromstring(data)
    tops = root.findall('top')
    if not tops:
        return None, None
    if tops[0].get('answer') in ('right', 'wrong'):
        return 'judge', root
    if tops[0].find('answer') is not None:
        return 'fill', root
    return None, root


def parse_questions(root):
    qtype = 'judge' if root.findall('top')[0].get('answer') else 'fill'
    qs = []
    for top in root.findall('top'):
        if qtype == 'judge':
            qs.append({'txt': top.get('txt', '').strip(),
                       'answer': top.get('answer', '')})
        else:
            qs.append({'txt': top.get('txt', '').strip(),
                       'answer': (top.findtext('answer') or '').strip()})
    return qtype, {'name': root.get('name', ''), 'questions': qs}


def ffdec_export(args, cwd):
    subprocess.run(['java', '-jar', str(FFDEC), *args],
                   cwd=cwd, check=True, capture_output=True)


def extract_assets(swf: Path, tmp: Path):
    """导出判题课件通用UI素材,返回 dict"""
    ffdec_export(['-export', 'frame', str(tmp / 'frames'), swf.name], swf.parent)
    ffdec_export(['-export', 'button', str(tmp / 'buttons'), swf.name], swf.parent)
    ffdec_export(['-export', 'sprite', str(tmp / 'sprites'), swf.name], swf.parent)
    assets = {}
    assets['bg'] = next((tmp / 'frames').glob('*.png'))
    # 按钮: 按宽高特征找"提交/查看答案"(60-80x25) —— 具体id因文件而异,用图像特征匹配
    # 稳妥做法: 直接复用模板目录里已确认的按钮图(它们是同一套UI库做的)
    return assets


def convert(quiz_dir: Path, outdir: Path):
    swf = next(quiz_dir.glob('*.swf'))
    xml_path = quiz_dir / 'data.xml'
    qtype, root = detect_type(xml_path)
    if not qtype:
        print(f"✗ {quiz_dir}: data.xml 题型无法识别(非judge/fill)")
        return
    qtype, cfg = parse_questions(root)
    name = cfg['name'] or quiz_dir.name
    print(f"== {name} [{qtype}] {len(cfg['questions'])}题")

    card = outdir / name
    (card / 'img').mkdir(parents=True, exist_ok=True)

    # 1) 生成 questions.js
    (card / 'questions.js').write_text(
        'const QUIZ_DATA = ' + json.dumps(cfg, ensure_ascii=False, indent=1) + ';', encoding='utf8')

    # 2) 复用模板素材+页面(同一UI库,跨文件通用); 仅背景图从本SWF导出
    tpl = TPL_DIR / ('我是小法官' if qtype == 'judge' else '填空乐园')
    for f in (tpl / 'img').iterdir():
        shutil.copy(f, card / 'img' / f.name)
    shutil.copy(tpl / 'index.html', card / 'index.html')

    tmp = Path(tempfile.mkdtemp(prefix='quiz_'))
    try:
        # 中文文件名坑: 复制到英文路径再交给 JPEXS
        tmp_swf = tmp / 'quiz.swf'
        shutil.copy(swf, tmp_swf)
        ffdec_export(['-export', 'frame', str(tmp / 'frames'), tmp_swf.name], tmp)
        bg = next((tmp / 'frames').glob('*.png'))
        shutil.copy(bg, card / 'img' / 'bg.png')
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # 3) 题图 pic/
    pic_dir = quiz_dir / 'pic'
    if pic_dir.exists():
        for p in sorted(pic_dir.glob('*.png')):
            shutil.copy(p, card / 'img' / f'pic_{p.stem}.png')
    if qtype == 'fill' and (card / 'img' / 'pic_01.png').exists():
        pass  # 模板里写死 pic_01.png, 多图场景待扩展
    print(f"  ✓ 输出: {card}/")


def main():
    ap = argparse.ArgumentParser(description='判题/填空课件 SWF → H5')
    ap.add_argument('paths', nargs='+', type=Path, help='课件目录(含swf+data.xml)或swf路径')
    ap.add_argument('-o', '--out', type=Path, default=Path('output/quiz_out'),
                    help='输出目录 (默认 output/quiz_out)')
    args = ap.parse_args()
    for p in args.paths:
        d = p.parent if p.suffix == '.swf' else p
        if not (d / 'data.xml').exists():
            print(f"跳过(无data.xml): {d}")
            continue
        try:
            convert(d, args.out.resolve())
        except Exception as e:
            print(f"✗ {d}: {e}")


if __name__ == '__main__':
    main()
