#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""EPUB 生成器（纯标准库，无第三方依赖）
用法: python3 tools_gen_epub.py <正文md> <输出epub>
产出: EPUB3 单文件，手机阅读 App（微信读书/静读天下/Kindle）直接导入
"""
import re, sys, html, datetime, zipfile, uuid

def esc(s):
    return html.escape(s, quote=True)

def build(src, dst, book_title="本书", author="AI 写作引擎"):
    text = open(src, encoding='utf-8').read()
    # 按章拆分（支持分卷：# 卷X：标题 作为卷标记，## 第X章 为章）
    # 先按卷切：卷之间插入卷标题；无卷标记则整本一卷
    # 按卷切（# 卷X：…），再每卷内按章切（## 第X章）
    # 卷标记与章标记独立：先按「# 卷」分行处理
    chs = []
    cur_vol = None
    for seg in re.split(r'^(?=#\s*卷)', text, flags=re.M):
        seg = seg.strip()
        if not seg:
            continue
        vm = re.match(r'^#\s*(卷[^\n]*)', seg)
        if vm:
            cur_vol = vm.group(1).strip()
            seg = seg.split('\n', 1)[1] if '\n' in seg else ""   # 去掉卷标题行
        for p in re.split(r'^(?=##\s+第\d+章)', seg, flags=re.M):
            p = p.strip()
            if not p:
                continue
            m = re.match(r'##\s+(第\d+章[^\n]*)', p)
            if not m:
                continue
            title = m.group(1).strip()
            body_lines = p.split('\n')[1:]
            paras = []
            buf = []
            for ln in body_lines:
                if ln.strip() == '':
                    if buf:
                        paras.append(' '.join(x.strip() for x in buf).strip())
                        buf = []
                else:
                    buf.append(ln)
            if buf:
                paras.append(' '.join(x.strip() for x in buf).strip())
            chs.append((title, paras, cur_vol))

    book_id = 'urn:uuid:' + str(uuid.uuid4())
    now = datetime.date.today().isoformat()

    # --- 生成各章 xhtml ---
    files = []
    toc_nav = []  # (id, title, vol)
    for i, (title, paras, vol) in enumerate(chs, 1):
        fid = f'chap{i:03d}'
        toc_nav.append((fid, title, vol))
        body = '\n'.join(f'<p>{esc(p)}</p>' for p in paras if p)
        xhtml = f"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
<head><title>{esc(title)}</title>
<link rel="stylesheet" type="text/css" href="style.css"/></head>
<body>
<h2>{esc(title)}</h2>
{body}
</body></html>"""
        files.append((f'OEBPS/{fid}.xhtml', xhtml.encode('utf-8')))

    # --- 封面页 ---
    cover_xhtml = f"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>封面</title><link rel="stylesheet" type="text/css" href="style.css"/></head>
<body class="cover">
<h1>《{esc(book_title)}》</h1>
<p class="sub">共 {len(chs)} 章 · 生成于 {now}</p>
</body></html>"""
    files.append(('OEBPS/cover.xhtml', cover_xhtml.encode('utf-8')))

    # --- 样式 ---
    css = """body{font-family:Georgia,"Songti SC","Noto Serif SC",serif;line-height:1.9;margin:5%;}
h2{text-align:center;margin:1.5em 0;font-size:1.3em;letter-spacing:.15em;}
p{text-indent:2em;margin:0 0 .8em;text-align:justify;}
.cover{text-align:center;padding-top:30%;}
.cover h1{font-size:2em;letter-spacing:.3em;}
.cover .sub{text-indent:0;color:#666;margin:.5em 0;}"""
    files.append(('OEBPS/style.css', css.encode('utf-8')))

    # --- content.opf ---
    manifest = '\n'.join(
        f'<item id="{fid}" href="{fid}.xhtml" media-type="application/xhtml+xml"/>'
        for fid, _, _ in toc_nav)
    manifest = (f'<item id="cover" href="cover.xhtml" media-type="application/xhtml+xml"/>'
                f'<item id="css" href="style.css" media-type="text/css"/>' + manifest)
    spine = '\n'.join(f'<itemref idref="{fid}"/>' for fid, _, _ in toc_nav)
    spine = '<itemref idref="cover"/>' + spine

    opf = f"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
  <dc:identifier id="bookid">{book_id}</dc:identifier>
  <dc:title>《{esc(book_title)}》</dc:title>
  <dc:creator>{esc(author)}</dc:creator>
  <dc:language>zh-CN</dc:language>
  <dc:date>{now}</dc:date>
  <meta property="dcterms:modified">{now}T00:00:00Z</meta>
</metadata>
<manifest>
{manifest}
</manifest>
<spine>
{spine}
</spine>
</package>"""

    # --- nav (EPUB3，支持分卷嵌套目录) ---
    # 按卷分组：vol 相同的一组；无卷标记整本一卷（vol=None → 全部平铺）
    vol_groups = {}   # vol_name -> [(fid, title)]
    vol_order = []
    for fid, t, vol in toc_nav:
        if vol not in vol_groups:
            vol_groups[vol] = []
            vol_order.append(vol)
        vol_groups[vol].append((fid, t))
    has_vol = len(vol_order) > 1 or (vol_order and vol_order[0] is not None)
    if has_vol:
        nav_parts = []
        for vol in vol_order:
            items = vol_groups[vol]
            li = '\n'.join(f'<li><a href="{fid}.xhtml">{esc(t)}</a></li>' for fid, t in items)
            nav_parts.append(f'<li>{esc(vol or "正文")}<ol>{li}</ol></li>')
        nav_li = '\n'.join(nav_parts)
    else:
        nav_li = '\n'.join(
            f'<li><a href="{fid}.xhtml">{esc(t)}</a></li>' for fid, t, _ in toc_nav)
    nav = f"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
<head><title>目录</title></head>
<body>
<nav epub:type="toc"><h1>目录</h1><ol>{nav_li}</ol></nav>
</body></html>"""
    files.append(('OEBPS/nav.xhtml', nav.encode('utf-8')))
    # nav 也要进 manifest
    manifest += '\n<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'
    # content.opf 本体 + nav 都打包
    files.append(('OEBPS/content.opf', opf.encode('utf-8')))

    # --- container.xml ---
    container = """<?xml version="1.0" encoding="utf-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>"""
    files.append(('META-INF/container.xml', container.encode('utf-8')))

    # --- 打包 ---
    with zipfile.ZipFile(dst, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('mimetype', 'application/epub+zip', compress_type=zipfile.ZIP_STORED)
        for name, data in files:
            z.writestr(name, data)
    print(f"✅ EPUB 生成: {dst} ({len(chs)}章)")

if __name__ == '__main__':
    build(sys.argv[1], sys.argv[2])
