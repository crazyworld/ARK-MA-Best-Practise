"""恢复包随带兄弟 Skill 脚本（job_436842fb74da n14 封面仍跑旧脚本）。

进行中的 Session 挂的是创建时的 Skill；只换编排器，兄弟 Skill（封面/词云/图表）的
修复永远到不了。pack_skills 把它们的脚本放进 bundled_skills/，/continue 解包时覆盖
同名脚本、其余文件（字体、依赖）仍软链到只读挂载。
"""
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

CASE = Path(__file__).resolve().parent.parent


def _runner_link_script() -> str:
    src = (CASE / "topic5_runner.py").read_text(encoding="utf-8")
    start = src.index('"for skill in /mnt/skills/*')
    end = src.index('done\\n"', start) + len('done\\n"')
    return eval("(" + src[start:end] + ")")


@unittest.skipUnless(shutil.which("bash"), "needs bash")
class BundledSkillLinkTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(self._cleanup)
        mnt = self.root / "mnt" / "skills"
        cover = mnt / "report-cover-skill"
        (cover / "scripts").mkdir(parents=True)
        (cover / "assets").mkdir()
        (cover / "scripts" / "generate_cover_gpt.py").write_text("OLD")
        (cover / "scripts" / "other.py").write_text("KEEP")
        (cover / "assets" / "f.ttf").write_text("FONT")
        (mnt / "ssjh").mkdir()
        target = self.root / "T"
        bundled = target / "pipeline-orchestrator" / "bundled_skills" / "report-cover-skill" / "scripts"
        bundled.mkdir(parents=True)
        (bundled / "generate_cover_gpt.py").write_text("NEW")
        for path in mnt.rglob("*"):
            path.chmod(path.stat().st_mode & ~0o222)
        self.mnt, self.target = mnt, target

    def _cleanup(self):
        for path in self.root.rglob("*"):
            try:
                path.chmod(path.stat().st_mode | stat.S_IWUSR)
            except OSError:
                pass
        shutil.rmtree(self.root, ignore_errors=True)

    def _run(self):
        script = _runner_link_script().replace("/mnt/skills/*", f"{self.mnt}/*")
        done = subprocess.run(["bash", "-c", script], env={**os.environ, "TARGET": str(self.target)},
                              capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_bundled_script_overrides_readonly_mount(self):
        self._run()
        cover = self.target / "report-cover-skill"
        self.assertEqual((cover / "scripts" / "generate_cover_gpt.py").read_text(), "NEW")
        self.assertEqual((cover / "scripts" / "other.py").read_text(), "KEEP")
        self.assertEqual((cover / "assets" / "f.ttf").read_text(), "FONT")
        self.assertEqual((self.mnt / "report-cover-skill" / "scripts" / "generate_cover_gpt.py").read_text(), "OLD")

    def test_unbundled_skill_stays_symlink(self):
        self._run()
        self.assertTrue((self.target / "ssjh").is_symlink())


class PackBundlesSiblingsTest(unittest.TestCase):
    def test_pack_includes_sibling_scripts(self):
        src = (CASE / "tools" / "pack_skills.sh").read_text(encoding="utf-8")
        self.assertIn("bundled_skills", src)
        for name in ("report-cover-skill", "wordcloud-insight", "blueai-yuqing-echarts-skill-new"):
            self.assertIn(name, src[src.index("bundled_skills"):])


if __name__ == "__main__":
    unittest.main()
