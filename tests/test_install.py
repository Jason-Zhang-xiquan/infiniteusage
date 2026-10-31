"""iu install / uninstall 的单元测试：全部在临时目录里，不碰真实的 ~/.claude"""

import argparse
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from iu import hook, install, universal  # noqa: E402

ORIGINAL = {"model": "opus", "env": {"FOO": "1"},
            "hooks": {"PreToolUse": [{"matcher": "Edit", "hooks": [{"type": "command", "command": "echo mine"}]}]},
            "permissions": {"allow": ["Bash(npm test:*)"]}}


class TestInstall(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.settings = os.path.join(self.tmp, "settings.json")
        json.dump(ORIGINAL, open(self.settings, "w"))
        self.patches = [mock.patch.object(install, "claude_settings_path", return_value=self.settings),
                        mock.patch.object(install, "STATE", os.path.join(self.tmp, "install.json")),
                        mock.patch.object(install, "DATA_DIR", self.tmp),
                        mock.patch.object(hook, "PILOT_FILE", os.path.join(self.tmp, "pilot.txt")),
                        mock.patch.object(install, "free_backend_ready", return_value=True),
                        mock.patch.object(install, "delegate_ready", return_value=False),
                        mock.patch.object(universal, "home", return_value=os.path.join(self.tmp, "home")),
                        mock.patch.object(universal, "app_dir", return_value=os.path.join(self.tmp, "app")),
                        mock.patch.object(universal, "codex_hook_supported", return_value=True)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def args(self, **kw):
        base = dict(agent="claude-code", compact_at=300_000, no_compact=False, no_offload=False, no_delegate=False,
                    no_notes=False, no_rules=False, dry_run=False)
        base.update(kw)
        return argparse.Namespace(**base)

    def test_delegate_hook_installed_and_removed(self):
        with mock.patch.object(install, "delegate_ready", return_value=True):
            install.cmd_install(self.args())
            install.cmd_install(self.args())  # 重装不重复
        s = json.load(open(self.settings))
        entries = [e for e in s["hooks"]["PreToolUse"] if any(install.is_ours(h["command"]) for h in e["hooks"])]
        self.assertEqual(sorted(e["matcher"] for e in entries), ["Agent|Task", "Bash"])
        self.assertTrue(all(h["command"].endswith("|| true") for e in entries for h in e["hooks"]))
        install.cmd_uninstall(argparse.Namespace(agent="claude-code", dry_run=False))
        self.assertEqual(json.load(open(self.settings)), ORIGINAL)

    def test_skills_installed_rendered_and_removed(self):
        os.makedirs(os.path.join(self.tmp, "home", ".codebuddy"))  # 有 CodeBuddy 才装它的目录
        install.cmd_install(self.args())
        home = os.path.join(self.tmp, "home")
        dirs = [os.path.join(home, ".agents", "skills", "infiniteusage"),
                os.path.join(home, ".claude", "skills", "infiniteusage"),
                os.path.join(home, ".codebuddy", "skills", "infiniteusage")]
        for d in dirs:
            text = open(os.path.join(d, "SKILL.md"), encoding="utf-8").read()
            self.assertNotIn("{{IU}}", text)
            self.assertIn(os.path.join(self.tmp, "app", "run_iu.py").replace("\\", "/"), text)
        self.assertFalse(os.path.exists(os.path.join(home, ".qwen")))  # 没有 Qwen Code 就不建它的目录
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "app", "iu", "delegate.py")))
        cmds = [h["command"] for e in json.load(open(self.settings))["hooks"]["PreToolUse"] for h in e["hooks"]]
        self.assertTrue(any(os.path.join(self.tmp, "app").replace("\\", "/") in c for c in cmds if install.is_ours(c)))
        install.cmd_uninstall(argparse.Namespace(agent="claude-code", dry_run=False))
        for d in dirs:
            self.assertFalse(os.path.exists(d))
        self.assertEqual(json.load(open(self.settings)), ORIGINAL)

    def test_skills_only_leaves_settings_alone(self):
        install.cmd_install(self.args(agent="skills"))
        self.assertEqual(json.load(open(self.settings)), ORIGINAL)
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "home", ".agents", "skills", "infiniteusage", "SKILL.md")))

    def test_dry_run_changes_nothing(self):
        install.cmd_install(self.args(dry_run=True))
        self.assertEqual(json.load(open(self.settings)), ORIGINAL)

    def test_window_is_compact_point_plus_buffer(self):
        for at, window in ((300_000, "333000"), (600_000, "633000"), (40_000, "100000"), (990_000, "1000000")):
            new, notes = install.plan_changes({}, at, False, "python")
            self.assertEqual(new["env"][install.ENV_KEY], window)
            self.assertIn(f"{(int(window) - install.TRIGGER_BUFFER) // 1000}K", notes[0])

    def test_install_keeps_existing_and_adds_ours(self):
        install.cmd_install(self.args())
        s = json.load(open(self.settings))
        self.assertEqual(s["model"], "opus")
        self.assertEqual(s["env"]["FOO"], "1")
        self.assertEqual(s["env"][install.ENV_KEY], "333000")  # 300K + 33K
        self.assertEqual(s["permissions"], ORIGINAL["permissions"])
        cmds = [h["command"] for e in s["hooks"]["PreToolUse"] for h in e["hooks"]]
        self.assertIn("echo mine", cmds)
        ours = [c for c in cmds if install.is_ours(c)]
        self.assertEqual(len(ours), 1)
        self.assertTrue(ours[0].endswith("|| true"))
        self.assertEqual(open(hook.PILOT_FILE).read().strip(), "*")
        backups = [f for f in os.listdir(self.tmp) if ".iu-backup-" in f]
        self.assertEqual(len(backups), 1)

    def test_reinstall_no_duplicates(self):
        install.cmd_install(self.args())
        install.cmd_install(self.args())
        s = json.load(open(self.settings))
        ours = [h for e in s["hooks"]["PreToolUse"] for h in e["hooks"] if install.is_ours(h["command"])]
        self.assertEqual(len(ours), 1)

    def test_uninstall_restores(self):
        install.cmd_install(self.args())
        install.cmd_uninstall(argparse.Namespace(agent="claude-code", dry_run=False))
        self.assertEqual(json.load(open(self.settings)), ORIGINAL)

    def test_uninstall_restores_previous_window(self):
        s = dict(ORIGINAL, env={"FOO": "1", install.ENV_KEY: "800000"})
        json.dump(s, open(self.settings, "w"))
        install.cmd_install(self.args())
        install.cmd_uninstall(argparse.Namespace(agent="claude-code", dry_run=False))
        self.assertEqual(json.load(open(self.settings))["env"][install.ENV_KEY], "800000")

    def test_no_backend_no_hook(self):
        with mock.patch.object(install, "free_backend_ready", return_value=False):
            install.cmd_install(self.args())
        s = json.load(open(self.settings))
        cmds = [h["command"] for e in s["hooks"]["PreToolUse"] for h in e["hooks"]]
        self.assertFalse(any(install.is_ours(c) for c in cmds))
        self.assertIn(install.ENV_KEY, s["env"])


    def test_compact_hook_installed_and_removed(self):
        mine = dict(ORIGINAL, hooks=dict(ORIGINAL["hooks"], SessionStart=[
            {"matcher": "startup", "hooks": [{"type": "command", "command": "echo hello"}]}]))
        json.dump(mine, open(self.settings, "w"))
        install.cmd_install(self.args())
        install.cmd_install(self.args())  # 重装不重复
        ss = json.load(open(self.settings))["hooks"]["SessionStart"]
        ours = [e for e in ss if any(install.is_ours(h["command"]) for h in e["hooks"])]
        self.assertEqual(sorted(e["matcher"] for e in ours), ["compact", "startup"])
        self.assertTrue(all(e["hooks"][0]["command"].endswith("hook-compact || true") for e in ours))
        self.assertIn({"matcher": "startup", "hooks": [{"type": "command", "command": "echo hello"}]}, ss)
        install.cmd_uninstall(argparse.Namespace(agent="claude-code", dry_run=False))
        self.assertEqual(json.load(open(self.settings)), mine)

    def test_compact_hook_alone_removed_cleanly(self):
        json.dump({"model": "opus"}, open(self.settings, "w"))
        with mock.patch.object(install, "free_backend_ready", return_value=False):
            install.cmd_install(self.args(no_compact=True))
        self.assertEqual(list(json.load(open(self.settings))["hooks"]), ["SessionStart"])
        install.cmd_uninstall(argparse.Namespace(agent="claude-code", dry_run=False))
        self.assertEqual(json.load(open(self.settings)), {"model": "opus"})

    def test_no_notes_flag(self):
        install.cmd_install(self.args(no_notes=True))
        self.assertNotIn("SessionStart", json.load(open(self.settings))["hooks"])

    def test_reinstall_with_no_notes_removes_old_compact_hook(self):
        install.cmd_install(self.args())
        install.cmd_install(self.args(no_notes=True))
        self.assertNotIn("SessionStart", json.load(open(self.settings))["hooks"])

    def test_rules_appended_once_and_removed_exactly(self):
        home = os.path.join(self.tmp, "home")
        for d in (".claude", ".codex", os.path.join(".config", "opencode")):
            os.makedirs(os.path.join(home, d))
        claude_md = os.path.join(home, ".claude", "CLAUDE.md")
        codex_md = os.path.join(home, ".codex", "AGENTS.md")
        oc_md = os.path.join(home, ".config", "opencode", "AGENTS.md")
        mine = "# 我的规则\n\n- 用中文\n"
        open(claude_md, "w", encoding="utf-8").write(mine)
        open(codex_md, "w", encoding="utf-8").write("no trailing newline")
        install.cmd_install(self.args())
        install.cmd_install(self.args())  # 重装不重复
        for p in (claude_md, codex_md):
            text = open(p, encoding="utf-8").read()
            self.assertEqual(text.count(universal.RULE_BEGIN), 1, p)
            self.assertIn("infiniteusage", text)
        self.assertFalse(os.path.exists(oc_md))  # OpenCode 只当免费工人，不写规则
        self.assertTrue(open(claude_md, encoding="utf-8").read().startswith(mine))
        install.cmd_uninstall(argparse.Namespace(agent="claude-code", dry_run=False))
        self.assertEqual(open(claude_md, encoding="utf-8").read(), mine)
        self.assertEqual(open(codex_md, encoding="utf-8").read(), "no trailing newline")

    def test_reinstall_removes_rule_and_plugin_from_old_versions(self):
        home = os.path.join(self.tmp, "home")
        oc = os.path.join(home, ".config", "opencode")
        os.makedirs(os.path.join(oc, "plugins"))
        oc_md = os.path.join(oc, "AGENTS.md")
        plugin = os.path.join(oc, "plugins", universal.OPENCODE_PLUGIN)
        mine = "# OpenCode 我自己的说明\n"
        sep = "\n"
        open(oc_md, "w", encoding="utf-8").write(mine + sep + universal.render_rule("iu"))  # 旧版写进去的规则
        open(plugin, "w").write("// old")
        json.dump({"rules": [{"path": oc_md, "created": False, "sep": sep}], "opencode_notes": plugin},
                  open(install.STATE, "w"))
        install.cmd_install(self.args())
        self.assertEqual(open(oc_md, encoding="utf-8").read(), mine)  # 只删我们那一段
        self.assertFalse(os.path.exists(plugin))

    def test_codex_notes_installed_and_removed(self):
        home = os.path.join(self.tmp, "home")
        for d in (".codex", os.path.join(".config", "opencode")):
            os.makedirs(os.path.join(home, d))
        hooks_json = os.path.join(home, ".codex", "hooks.json")
        mine = {"hooks": {"SessionStart": [{"matcher": "startup", "hooks": [{"type": "command", "command": "echo mine"}]}],
                          "Stop": [{"hooks": [{"type": "command", "command": "echo stop"}]}]}}
        json.dump(mine, open(hooks_json, "w"))
        plugin = os.path.join(home, ".config", "opencode", "plugins", universal.OPENCODE_PLUGIN)
        with mock.patch.dict(os.environ, {"CODEX_HOME": ""}):
            install.cmd_install(self.args())
            install.cmd_install(self.args())  # 重装不重复
            cfg = json.load(open(hooks_json))
            ours = [e for e in cfg["hooks"]["SessionStart"] if "hook-compact" in e["hooks"][0]["command"]]
            self.assertEqual(len(ours), 1)
            self.assertEqual(ours[0]["matcher"], "startup|compact")
            self.assertIn("run_iu.py", ours[0]["hooks"][0]["command"])
            self.assertFalse(os.path.exists(plugin))  # OpenCode 不再装插件
            install.cmd_uninstall(argparse.Namespace(agent="claude-code", dry_run=False))
        self.assertEqual(json.load(open(hooks_json)), mine)  # 用户自己的 hook 原样留着
        self.assertFalse(os.path.exists(plugin))

    def test_codex_hooks_file_created_then_deleted(self):
        os.makedirs(os.path.join(self.tmp, "home", ".codex"))
        hooks_json = os.path.join(self.tmp, "home", ".codex", "hooks.json")
        with mock.patch.dict(os.environ, {"CODEX_HOME": ""}):
            install.cmd_install(self.args())
            self.assertTrue(os.path.exists(hooks_json))
            install.cmd_uninstall(argparse.Namespace(agent="claude-code", dry_run=False))
        self.assertFalse(os.path.exists(hooks_json))

    def test_no_notes_skips_codex_and_opencode(self):
        home = os.path.join(self.tmp, "home")
        for d in (".codex", os.path.join(".config", "opencode")):
            os.makedirs(os.path.join(home, d))
        with mock.patch.dict(os.environ, {"CODEX_HOME": ""}):
            install.cmd_install(self.args(no_notes=True))
        self.assertFalse(os.path.exists(os.path.join(home, ".codex", "hooks.json")))
        self.assertFalse(os.path.exists(os.path.join(home, ".config", "opencode", "plugins")))

    def need_tomllib(self):
        try:
            import tomllib
        except ImportError:  # Python 3.10 及以下（Mac 自带的 /usr/bin/python3 是 3.9）
            self.skipTest("没有 tomllib")
        return tomllib

    def codex_toml(self, content=None):
        d = os.path.join(self.tmp, "home", ".codex")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, "config.toml")
        if content is not None:
            open(path, "wb").write(content)
        return path

    def install_uninstall(self, path, **kw):
        """装、重装、卸载；返回装好后的内容"""
        with mock.patch.dict(os.environ, {"CODEX_HOME": ""}):
            install.cmd_install(self.args(**kw))
            install.cmd_install(self.args(**kw))  # 重装不重复、不改原值
            installed = open(path, "rb").read()
            install.cmd_uninstall(argparse.Namespace(agent="claude-code", dry_run=False))
        return installed

    def test_codex_compact_inserted_before_tables_crlf(self):
        orig = b'model = "gpt-6-astra"\r\nservice_tier = "default"\r\n\r\n[windows]\r\nsandbox = "elevated"\r\n'
        path = self.codex_toml(orig)
        installed = self.install_uninstall(path)
        tomllib = self.need_tomllib()
        cfg = tomllib.loads(installed.decode())
        self.assertEqual(cfg["model_auto_compact_token_limit"], install.CODEX_COMPACT_AT)
        self.assertEqual(cfg["windows"], {"sandbox": "elevated"})  # 没跑进表里
        self.assertEqual(installed.count(b"model_auto_compact_token_limit"), 1)
        self.assertNotIn(b"\n", installed.replace(b"\r\n", b""))  # 没混进单独的 LF
        self.assertEqual(open(path, "rb").read(), orig)  # 一字节不差地还原
        self.assertTrue([f for f in os.listdir(os.path.dirname(path)) if ".iu-backup-" in f])

    def test_codex_compact_file_starting_with_table(self):
        orig = b'[projects."/Users/me/x"]\ntrust_level = "trusted"\n'
        path = self.codex_toml(orig)
        installed = self.install_uninstall(path)
        self.assertTrue(installed.startswith(b"model_auto_compact_token_limit = 150000\n\n[projects"))
        self.assertEqual(open(path, "rb").read(), orig)

    def test_codex_compact_replaces_and_restores_existing_value(self):
        orig = b'model = "x"\nmodel_auto_compact_token_limit = 200000  # mine\n[a]\nb = 1\n'
        path = self.codex_toml(orig)
        installed = self.install_uninstall(path, codex_compact_at=120_000)
        self.assertIn(b"model_auto_compact_token_limit = 120000\n[a]", installed)
        self.assertEqual(open(path, "rb").read(), orig)
        self.assertEqual(install.load_state()["codex_compact"]["previous"], "model_auto_compact_token_limit = 200000  # mine\n")

    def test_codex_compact_ignores_same_key_inside_table(self):
        orig = b'[profiles.fast]\nmodel_auto_compact_token_limit = 90000\n'
        path = self.codex_toml(orig)
        installed = self.install_uninstall(path)
        tomllib = self.need_tomllib()
        cfg = tomllib.loads(installed.decode())
        self.assertEqual(cfg["model_auto_compact_token_limit"], 150000)
        self.assertEqual(cfg["profiles"]["fast"]["model_auto_compact_token_limit"], 90000)
        self.assertEqual(open(path, "rb").read(), orig)

    def test_codex_compact_created_then_deleted(self):
        path = self.codex_toml()
        installed = self.install_uninstall(path)
        self.assertEqual(installed, b"model_auto_compact_token_limit = 150000\n")
        self.assertFalse(os.path.exists(path))

    def test_codex_compact_user_change_kept_on_uninstall(self):
        path = self.codex_toml(b'model = "x"\n')
        with mock.patch.dict(os.environ, {"CODEX_HOME": ""}):
            install.cmd_install(self.args())
            changed = open(path, "rb").read().replace(b"150000", b"170000")
            open(path, "wb").write(changed)
            install.cmd_uninstall(argparse.Namespace(agent="claude-code", dry_run=False))
        self.assertIn(b"model_auto_compact_token_limit = 170000", open(path, "rb").read())

    def test_codex_compact_skipped(self):
        orig = b'model = "x"\n'
        path = self.codex_toml(orig)
        with mock.patch.dict(os.environ, {"CODEX_HOME": ""}):
            install.cmd_install(self.args(no_compact=True))
            self.assertEqual(open(path, "rb").read(), orig)
            install.cmd_install(self.args(dry_run=True))
            self.assertEqual(open(path, "rb").read(), orig)
            install.cmd_install(self.args())
            install.cmd_install(self.args(codex_compact_at=0))  # 重装时不要了：还原
        self.assertEqual(open(path, "rb").read(), orig)

    def test_codex_compact_broken_toml_untouched(self):
        orig = b'model = "x\n[[[\n'
        path = self.codex_toml(orig)
        self.install_uninstall(path)
        self.assertEqual(open(path, "rb").read(), orig)

    def test_codex_offload_hook_installed_and_removed(self):
        home = os.path.join(self.tmp, "home")
        os.makedirs(os.path.join(home, ".codex"))
        hooks_json = os.path.join(home, ".codex", "hooks.json")
        mine = {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "echo mine"}]}]}}
        json.dump(mine, open(hooks_json, "w"))
        with mock.patch.dict(os.environ, {"CODEX_HOME": ""}):
            install.cmd_install(self.args(codex_offload=True))
            install.cmd_install(self.args(codex_offload=True))  # 重装不重复
            cfg = json.load(open(hooks_json))
            ours = [e for e in cfg["hooks"]["PreToolUse"] if "run_iu.py" in e["hooks"][0]["command"]]
            self.assertEqual(len(ours), 1)
            self.assertTrue(ours[0]["hooks"][0]["command"].endswith("hook-pre --codex || true"))
            self.assertEqual(len(cfg["hooks"]["SessionStart"]), 1)
            install.cmd_install(self.args())  # 不加 --codex-offload 重装：PreToolUse 去掉，NOTES.md hook 留着
            cfg = json.load(open(hooks_json))
            self.assertEqual(cfg["hooks"]["PreToolUse"], mine["hooks"]["PreToolUse"])
            self.assertEqual(len(cfg["hooks"]["SessionStart"]), 1)
            install.cmd_install(self.args(codex_offload=True))
            install.cmd_uninstall(argparse.Namespace(agent="claude-code", dry_run=False))
        self.assertEqual(json.load(open(hooks_json)), mine)

    def test_codex_offload_only_file_created_then_deleted(self):
        os.makedirs(os.path.join(self.tmp, "home", ".codex"))
        hooks_json = os.path.join(self.tmp, "home", ".codex", "hooks.json")
        with mock.patch.dict(os.environ, {"CODEX_HOME": ""}):
            install.cmd_install(self.args(codex_offload=True))  # NOTES.md hook 新建文件，PreToolUse 加在后面
            self.assertEqual(sorted(json.load(open(hooks_json))["hooks"]), ["PreToolUse", "SessionStart"])
            install.cmd_uninstall(argparse.Namespace(agent="claude-code", dry_run=False))
        self.assertFalse(os.path.exists(hooks_json))

    def test_no_codex_dir_no_config(self):
        with mock.patch.dict(os.environ, {"CODEX_HOME": ""}):
            install.cmd_install(self.args())
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "home", ".codex")))

    def test_rule_says_read_notes_at_session_start(self):
        from iu import i18n
        for lang, start, block in (("zh", "每个新会话开始时", "阻塞运行"), ("en", "At the start of every new session", "foreground")):
            with mock.patch.dict(os.environ, {"IU_LANG": lang}):
                i18n.lang.cache_clear()
                rule = universal.render_rule("x")
            self.assertIn(start, rule)
            self.assertIn(block, rule)
            self.assertTrue(rule.startswith(universal.RULE_BEGIN) and rule.rstrip().endswith(universal.RULE_END))
        i18n.lang.cache_clear()

    def test_no_rules_flag(self):
        os.makedirs(os.path.join(self.tmp, "home", ".codex"))
        install.cmd_install(self.args(no_rules=True))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "home", ".codex", "AGENTS.md")))

    def sim(self, compact_at, reason="sim"):
        from iu import compactsim
        res = {"compact_at": compact_at, "reason": reason, "requests": 5000, "max_ctx": 900_000,
               "rows": [{"compact_at": 300_000, "saving": 0.3, "pessimistic": 0.25, "compactions": 9}]}
        return [mock.patch.object(compactsim, "load_sessions", return_value=[]),
                mock.patch.object(compactsim, "choose", return_value=res)]

    def test_auto_compact_picks_from_simulation(self):
        ps = self.sim(400_000)
        for p in ps:
            p.start()
        try:
            install.cmd_install(self.args(compact_at=None))
        finally:
            for p in ps:
                p.stop()
        self.assertEqual(json.load(open(self.settings))["env"][install.ENV_KEY], "433000")
        state = json.load(open(os.path.join(self.tmp, "install.json")))
        self.assertEqual((state["compact_at"], state["compact_sim"]["reason"]), (400_000, "sim"))

    def test_auto_compact_short_sessions_leaves_env_alone(self):
        ps = self.sim(0, "short")
        for p in ps:
            p.start()
        try:
            install.cmd_install(self.args(compact_at=None))
        finally:
            for p in ps:
                p.stop()
        self.assertEqual(json.load(open(self.settings))["env"], {"FOO": "1"})
        install.cmd_uninstall(argparse.Namespace(agent="claude-code", dry_run=False))
        self.assertEqual(json.load(open(self.settings)), ORIGINAL)

    def test_reinstall_keeps_previous_point_without_simulating(self):
        install.cmd_install(self.args())  # 第一次装：300K
        from iu import compactsim
        with mock.patch.object(compactsim, "choose", side_effect=AssertionError("不该再模拟")):
            install.cmd_install(self.args(compact_at=None))
        self.assertEqual(json.load(open(self.settings))["env"][install.ENV_KEY], "333000")

    def test_reinstall_auto_dropping_compaction_restores_env(self):
        install.cmd_install(self.args())
        ps = self.sim(0, "no_gain")
        for p in ps:
            p.start()
        try:
            install.cmd_install(self.args(compact_at="auto"))
        finally:
            for p in ps:
                p.stop()
        self.assertNotIn(install.ENV_KEY, json.load(open(self.settings))["env"])
        install.cmd_uninstall(argparse.Namespace(agent="claude-code", dry_run=False))
        self.assertEqual(json.load(open(self.settings)), ORIGINAL)


class TestPilotStar(unittest.TestCase):
    def test_star_means_all(self):
        self.assertTrue(hook.in_pilot("/any/where", ["*"]))


if __name__ == "__main__":
    unittest.main()
