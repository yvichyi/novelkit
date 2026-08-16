#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TXT 导出器（纯标准库）
用法: python3 tools_gen_txt.py <正文md> <输出txt>
产出: 纯文本全文，章节标题保留（第X章 标题），去掉 markdown 符号，适合手机阅读/归档
"""
import re, sys

def build(src, dst):
    text = open(src, encoding='utf-8').read()
    out = []
    for line in text.split('\n'):
        s = line.rstrip()
        if not s:
            continue
        # 章节标题：## 第X章 标题 -> 空行 + 第X章 标题
        m = re.match(r'^##\s+(第\d+章\s*.*)$', s)
        if m:
            out.append('')
            out.append(m.group(1).strip())
            out.append('')
            continue
        # 其他 markdown 残留清理
        s = re.sub(r'^\s*[-*]\s+', '', s)
        s = s.replace('**', '').replace('`', '')
        out.append(s)
    txt = '\n'.join(out).strip() + '\n'
    open(dst, 'w', encoding='utf-8').write(txt)
    n = len(re.findall(r'^第\d+章', txt, re.M))
    print(f'✅ 已导出 {dst}（{len(txt)} 字符，{n} 章）')

if __name__ == '__main__':
    if len(sys.argv) < 3:
        print('用法: python3 tools_gen_txt.py <正文md> <输出txt>')
        sys.exit(1)
    build(sys.argv[1], sys.argv[2])
