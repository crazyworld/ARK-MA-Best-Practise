"""引擎按名字取 handler：注册名必须指向同名的 (node, state, ctx) 函数。

job_436842fb74da n16：`_limitation_note` 被插在 @handler("write_report") 与
write_report 之间，装饰器挂到了 `_limitation_note(stats)` 上，引擎调用报
「takes 1 positional argument but 3 were given」。直接调 handlers.write_report 的
测试/预跑都绕过了注册表，发现不了。
"""
import inspect
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import handlers  # noqa: E402


class HandlerRegistryTest(unittest.TestCase):
    def test_every_handler_takes_node_state_ctx(self):
        bad = {}
        for name, fn in handlers._REGISTRY.items():
            params = list(inspect.signature(fn).parameters)
            if params[:3] != ["node", "state", "ctx"]:
                bad[name] = f"{fn.__name__}{inspect.signature(fn)}"
        self.assertEqual(bad, {})

    def test_registered_name_matches_function(self):
        # 注册名 = 函数名（或明确别名），防止装饰器挂错函数
        mismatched = {name: fn.__name__ for name, fn in handlers._REGISTRY.items()
                      if fn.__name__.lstrip("_") != name and not name.startswith(fn.__name__.lstrip("_"))}
        self.assertNotIn("write_report", mismatched)
        self.assertIs(handlers.get("write_report"), handlers.write_report)

    def test_contract_handlers_all_registered(self):
        import yaml  # noqa: F401  # 若无 yaml 则跳过
        path = os.path.join(os.path.dirname(handlers.__file__), "..", "..", "io",
                            "contracts", "pipeline.v0.2.yaml")
        if not os.path.isfile(path):
            self.skipTest("contract not found")
        import re
        names = set(re.findall(r"^\s+handler:\s*(\w+)", open(path, encoding="utf-8").read(), re.M))
        self.assertEqual(sorted(names - set(handlers._REGISTRY)), [])


if __name__ == "__main__":
    unittest.main()
