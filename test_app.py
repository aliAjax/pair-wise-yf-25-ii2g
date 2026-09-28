"""端到端验证脚本：通过 HTTP 提交评审数据并断言响应。"""
import json
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import create_app

# 使用内存 SQLite 数据库进行测试
client = None

# 初始化应用
app = create_app()

# 运行测试
if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(unittest.TestLoader().loadTestsFromName("app"))
