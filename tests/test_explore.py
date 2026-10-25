"""iu.explore 引用核实的单元测试"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from iu import explore  # noqa: E402


class TestVerifyRefs(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.root, "server", "src", "lib"))
        with open(os.path.join(self.root, "server", "src", "lib", "config.ts"), "w") as f:
            f.write("\n".join(f"line {i}" for i in range(1, 101)))
        os.makedirs(os.path.join(self.root, "node_modules", "x"))
        with open(os.path.join(self.root, "node_modules", "x", "fake.ts"), "w") as f:
            f.write("x\n" * 500)

    def test_full_path_and_basename(self):
        ok, bad = explore.verify_refs("see server/src/lib/config.ts:72 and config.ts:100", self.root)
        self.assertEqual(sorted(ok), ["config.ts:100", "server/src/lib/config.ts:72"])
        self.assertEqual(bad, [])

    def test_out_of_range_and_missing(self):
        ok, bad = explore.verify_refs("config.ts:101, nope.ts:3, fake.ts:10", self.root)
        self.assertEqual(ok, [])
        self.assertEqual(sorted(bad), ["config.ts:101", "fake.ts:10", "nope.ts:3"])  # node_modules 里的不算


if __name__ == "__main__":
    unittest.main()
