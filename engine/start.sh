#!/data/data/com.termux/files/usr/bin/bash
# -*- coding: utf-8 -*-
# 一键启动 NovelKit 写作引擎 · 手机 Termux 专用
# 用法：在项目目录里敲  ./start.sh   （或 bash start.sh）
# 功能：傻瓜式菜单 —— 写默认书 / 写新书 / 建新书 / 预览

cd "$(dirname "$0")"

# ---- 找 python3（Termux 的 or 系统）----
PY=""
for c in python3 python; do
  if command -v "$c" >/dev/null 2>&1; then PY="$c"; break; fi
done
if [ -z "$PY" ]; then
  echo "❌ 没找到 Python3。请先安装："
  echo "   pkg update && pkg install python"
  read -r -p "回车退出"
  exit 1
fi

# ---- 是否已有 key（.env 或环境变量）----
HAS_KEY=0
grep -q "DEEPSEEK_API_KEY=sk-" .env 2>/dev/null && HAS_KEY=1
[ -n "$DEEPSEEK_API_KEY" ] && HAS_KEY=1

echo ""
echo "╔══════════════════════════════════════════╗"
echo "║   📖 NovelKit 写作引擎 · 手机版          ║"
echo "╚══════════════════════════════════════════╝"
echo ""
if [ "$HAS_KEY" = "0" ]; then
  echo "⚠️  还没配置 API Key（写一章需要 DeepSeek key）"
  echo "   首次运行会让你输入并保存到 .env，一次搞定"
fi

echo ""
echo "  1) 写默认书下一章（傻瓜模式）"
echo "  2) 写 新书 下一章（需先建书）"
echo "  3) 建新书（问答式 + AI 生成设定）"
echo "  4) 预览下一章 prompt（零成本，不发 API）"
echo "  5) 直接写 N 章（如：5）"
echo "  6) 🚀 网页版写作台（手机浏览器点按，家人朋友也能连）"
echo ""
read -r -p "  选一个 [回车=1]: " M
M="${M:-1}"

case "$M" in
  6)
    PORT="${WEB_PORT:-8080}"
    echo "→ 启动网页版写作台（http://本机IP:$PORT）…"
    "$PY" web_writer.py --port "$PORT"
    ;;
  2)
    echo ""
    echo "现有新书："
    ls -d books/*/ 2>/dev/null | sed 's/^/    /' || echo "    （还没有，请先选 3 建书）"
    read -r -p "  输入书名（目录名）: " BK
    if [ -d "books/$BK" ]; then
      echo "→ 写《$BK》下一章…"
      NOVEL_DIR="books/$BK" "$PY" write_chapter.py
    else
      echo "❌ 没找到 books/$BK"
    fi
    ;;
  3)
    "$PY" new_novel.py
    ;;
  4)
    if [ -n "$NOVEL_DIR" ]; then
      NOVEL_DIR="$NOVEL_DIR" "$PY" write_chapter.py --preview
    else
      read -r -p "预览哪个？ [1=默认书 / 2=新书]: " P
      if [ "$P" = "2" ]; then
        ls -d books/*/ 2>/dev/null | sed 's/^/    /'
        read -r -p "  书名: " BK
        [ -d "books/$BK" ] && NOVEL_DIR="books/$BK" "$PY" write_chapter.py --preview || echo "❌ 没找到"
      else
        "$PY" write_chapter.py --preview
      fi
    fi
    ;;
  5)
    read -r -p "  写几章？ [回车=1]: " N
    N="${N:-1}"
    echo "→ 写默认书 $N 章…"
    "$PY" write_chapter.py --write "$N"
    ;;
  *)
    echo "→ 写默认书下一章（傻瓜模式）…"
    "$PY" write_chapter.py
    ;;
esac

echo ""
echo "✅ 完成。按回车退出。"
read -r -p ""
