"""不用安装也能运行：python run_iu.py [命令]（Claude Code 的 statusline 也用这个入口）"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from iu.cli import main  # noqa: E402

main()
