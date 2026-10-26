"""codex_approval: the approval_mode = "prompt" tables sampan keeps in Codex's config.toml, edited as text."""
import ast
import codecs
import os
import stat
import subprocess
import sys
import tempfile
import tomllib
import unittest
from datetime import datetime
from pathlib import Path

from sampan import codex_approval as ca

NOW = datetime(2026, 10, 27, 1, 30, 0)
BACKUP = "config.toml.bak-sampan-20261027-013000"

OTHERS_BEFORE = """# my Codex settings
model = "m"   # keep this comment

[mcp_servers.sampan]
command = "/u/.local/bin/sampan"
args = ["mcp", "serve"]

[mcp_servers.other]
command = "x"
"""
SAMPAN_OLD = """
[mcp_servers.sampan.tools.send]
approval_mode = "approve"
"""
OTHERS_AFTER = """
# about the next table
[mcp_servers.other.tools.thing]
approval_mode = "approve"

[profiles.p]
model = "n"
"""
ORIGINAL = OTHERS_BEFORE + SAMPAN_OLD + OTHERS_AFTER
INSTALLED = '[mcp_servers.sampan]\ncommand = "/u/.local/bin/sampan"\n'


def modes(text):
    tools = tomllib.loads(text).get("mcp_servers", {}).get("sampan", {}).get("tools", {})
    return {t: v.get("approval_mode") for t, v in tools.items()}


class EditTest(unittest.TestCase):
    def test_with_prompt_appends_three_sections(self):
        out = ca.with_prompt(INSTALLED)
        self.assertEqual(modes(out), {t: "prompt" for t in ca.ACTING_TOOLS})
        self.assertTrue(out.startswith(INSTALLED))

    def test_edit_keeps_other_tables_byte_for_byte(self):
        out = ca.with_prompt(ORIGINAL)
        self.assertEqual(ca.strip_sections(out), ca.strip_sections(ORIGINAL))
        self.assertEqual(ca.strip_sections(ORIGINAL), OTHERS_BEFORE + OTHERS_AFTER)  # only sampan's lines went
        self.assertTrue(out.startswith(OTHERS_BEFORE + OTHERS_AFTER))
        data = tomllib.loads(out)
        self.assertEqual(data["mcp_servers"]["other"]["tools"]["thing"], {"approval_mode": "approve"})
        self.assertEqual(modes(out), {t: "prompt" for t in ca.ACTING_TOOLS})

    def test_edit_file_without_trailing_newline(self):
        text = INSTALLED + 'args = ["mcp", "serve"]'
        out = ca.with_prompt(text)
        self.assertTrue(out.startswith(text + "\n\n[mcp_servers.sampan.tools."))
        self.assertEqual(tomllib.loads(out)["mcp_servers"]["sampan"]["args"], ["mcp", "serve"])
        self.assertEqual(modes(out), {t: "prompt" for t in ca.ACTING_TOOLS})
        self.assertEqual(ca.strip_sections(out), text + "\n")
        last = INSTALLED + '\n[mcp_servers.sampan.tools.send]\napproval_mode = "approve"'  # sampan's table is last, no \n
        self.assertEqual(ca.strip_sections(last), INSTALLED)

    def test_strip_is_inverse_of_with_prompt(self):
        for t in (ORIGINAL, INSTALLED, "", "a = 1\n", OTHERS_BEFORE + OTHERS_AFTER):
            with self.subTest(t=t):
                self.assertEqual(ca.strip_sections(ca.with_prompt(t)), ca.strip_sections(t))
                self.assertEqual(ca.with_prompt(ca.with_prompt(t)), ca.with_prompt(t))

    def test_only_the_exact_header_is_sampans(self):
        text = INSTALLED + '\n[mcp_servers.sampan.tools.list_sessions]\napproval_mode = "approve"\n'
        self.assertEqual(ca.strip_sections(text), text)  # a read-only tool's table is not sampan's to touch

    def test_needs_fix(self):
        self.assertFalse(ca.needs_fix("", True))  # not installed
        self.assertFalse(ca.needs_fix("[x", True))  # broken: doctor reports it elsewhere
        self.assertTrue(ca.needs_fix(INSTALLED, True))
        self.assertTrue(ca.needs_fix(ORIGINAL, True))  # "approve"
        self.assertFalse(ca.needs_fix(ca.with_prompt(INSTALLED), True))
        self.assertFalse(ca.needs_fix(INSTALLED, False))
        self.assertTrue(ca.needs_fix(ca.with_prompt(INSTALLED), False))
        self.assertFalse(ca.needs_fix(ORIGINAL, False))  # the user's own "approve" is not sampan's

    def test_tool_prompts(self):
        d = Path(tempfile.mkdtemp())
        p = d / "config.toml"
        self.assertFalse(ca.tool_prompts(p, "send"))  # missing
        p.write_text("[x")
        self.assertFalse(ca.tool_prompts(p, "send"))
        p.write_text(ORIGINAL)
        self.assertFalse(ca.tool_prompts(p, "send"))
        p.write_text(ca.with_prompt(INSTALLED))
        self.assertTrue(all(ca.tool_prompts(p, t) for t in ca.ACTING_TOOLS))


class ApplyTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.path = self.dir / "config.toml"

    def backups(self):
        return sorted(p.name for p in self.dir.iterdir() if ".bak-sampan-" in p.name)

    def test_install_rewrites_approve_back_to_prompt(self):
        self.path.write_text(ORIGINAL)
        os.chmod(self.path, 0o640)
        self.assertEqual(ca.apply(self.path, True, now=NOW), f"已更新（備份：{BACKUP}）")
        self.assertEqual(modes(self.path.read_text()), {t: "prompt" for t in ca.ACTING_TOOLS})
        self.assertEqual(self.backups(), [BACKUP])
        self.assertEqual((self.dir / BACKUP).read_text(), ORIGINAL)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o640)
        self.assertEqual(stat.S_IMODE((self.dir / BACKUP).stat().st_mode), 0o640)

    def test_uninstall_strips_the_tables(self):
        self.path.write_text(ca.with_prompt(INSTALLED))
        self.assertTrue(ca.apply(self.path, False, now=NOW).startswith("已更新"))
        self.assertEqual(self.path.read_text(), INSTALLED)

    def test_broken_codex_config_is_not_written(self):
        self.path.write_text("[x")
        with self.assertRaises(ValueError) as e:
            ca.apply(self.path, True, now=NOW)
        self.assertIn("Codex config.toml 不是合法的 TOML", str(e.exception))
        self.assertEqual(self.path.read_text(), "[x")
        self.assertEqual(self.backups(), [])

    def test_apply_noop_when_already_right(self):
        self.path.write_text(ca.with_prompt(INSTALLED))
        self.assertEqual(ca.apply(self.path, True, now=NOW), "不用改")
        self.path.write_text(INSTALLED)
        self.assertEqual(ca.apply(self.path, False, now=NOW), "不用改")
        self.assertEqual(self.backups(), [])

    def test_apply_is_idempotent_when_prompt_tables_are_followed_by_another_server(self):
        text = ca.with_prompt(INSTALLED) + '\n[mcp_servers.other]\ncommand = "x"\n'
        self.assertFalse(ca.needs_fix(text, True))
        self.assertNotEqual(ca.with_prompt(text), text)  # the tables are not at the end: a rewrite would move them
        self.path.write_text(text)
        self.assertEqual(ca.apply(self.path, True, now=NOW, dry_run=True), "不用改")
        self.assertEqual(ca.apply(self.path, True, now=NOW), "不用改")
        self.assertEqual(ca.apply(self.path, True, now=NOW), "不用改")
        self.assertEqual(self.path.read_text(), text)
        self.assertEqual(self.backups(), [])

    def test_not_installed_or_missing_is_left_alone(self):
        self.assertEqual(ca.apply(self.path, True, now=NOW), "不用改")  # no file
        self.assertFalse(self.path.exists())
        self.path.write_text("a = 1\n")  # no [mcp_servers.sampan]: a tools table alone would break Codex's config
        self.assertEqual(ca.apply(self.path, True, now=NOW), "不用改")
        self.assertEqual(self.path.read_text(), "a = 1\n")

    def test_dry_run_writes_nothing(self):
        self.path.write_text(INSTALLED)
        self.assertEqual(ca.apply(self.path, True, now=NOW, dry_run=True), "會更新")
        self.assertEqual(ca.apply(self.path, False, now=NOW, dry_run=True), "不用改")
        self.assertEqual(self.path.read_text(), INSTALLED)
        self.assertEqual(self.backups(), [])

    def test_crlf_file_keeps_its_bytes(self):
        crlf = ORIGINAL.replace("\n", "\r\n").encode()
        self.path.write_bytes(crlf)
        self.assertTrue(ca.apply(self.path, True, now=NOW).startswith("已更新"))
        self.assertEqual((self.dir / BACKUP).read_bytes(), crlf)  # byte-identical backup
        new = self.path.read_bytes()
        kept = (OTHERS_BEFORE + OTHERS_AFTER).replace("\n", "\r\n").encode()
        self.assertTrue(new.startswith(kept), new)
        tail = new[len(kept):]
        self.assertEqual(tail, "".join(f'\r\n{ca.section(t)}\r\napproval_mode = "prompt"\r\n'
                                       for t in ca.ACTING_TOOLS).encode())
        self.assertEqual(modes(new.decode()), {t: "prompt" for t in ca.ACTING_TOOLS})
        self.assertTrue(ca.apply(self.path, False, now=NOW).startswith("已更新"))
        self.assertEqual(self.path.read_bytes(), kept)

    def test_unrecognised_sampan_tables_get_their_own_message(self):
        for text in ('mcp_servers = { sampan = { command = "x", tools = { send = { approval_mode = "approve" } } } }\n',
                     INSTALLED + '\n[mcp_servers."sampan".tools."send"]\napproval_mode = "approve"\n'):
            with self.subTest(text=text):
                self.path.write_text(text)
                with self.assertRaises(ValueError) as e:
                    ca.apply(self.path, True, now=NOW)
                self.assertEqual(str(e.exception), ca.NOT_TABLES)
                self.assertNotIn("不是合法的 TOML", str(e.exception))
                self.assertEqual(self.path.read_text(), text)
                self.assertEqual(self.backups(), [])

    def test_utf8_under_an_ascii_locale(self):
        text = ("# 我的設定\r\n" + ORIGINAL.replace("\n", "\r\n")).replace("keep this comment", "保留這行")
        raw = text.encode()
        self.path.write_bytes(raw)
        src = Path(ca.__file__).resolve().parents[1]
        # the child's code must be pure ASCII: under LC_ALL=C Linux cannot decode a non-ASCII -c argument
        code = ("import locale, sys; from datetime import datetime; from sampan import codex_approval as ca; "
                "print(locale.getpreferredencoding(False)); "
                f"print(ascii(ca.apply({str(self.path)!r}, True, now=datetime(2026, 10, 27, 1, 30))).replace(' ', '_')); "
                f"print(all(ca.tool_prompts({str(self.path)!r}, t) for t in ca.ACTING_TOOLS))")
        env = {"PATH": "/usr/bin:/bin", "LC_ALL": "C", "LANG": "C", "PYTHONUTF8": "0", "PYTHONCOERCECLOCALE": "0",
               "PYTHONPATH": str(src), "HOME": str(self.dir)}
        r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(r.returncode, 0, r.stderr)
        enc, applied, prompts = r.stdout.split()
        if codecs.lookup(enc).name != "ascii":  # Linux names it ANSI_X3.4-1968
            self.skipTest(f"C locale is {enc} here")
        self.assertTrue(ast.literal_eval(applied).startswith("已更新"), applied)  # printed with ascii(): the command line stays ASCII
        self.assertEqual(prompts, "True")
        self.assertEqual((self.dir / BACKUP).read_bytes(), raw)
        new = self.path.read_bytes()
        self.assertEqual(ca.strip_sections(new.decode("utf-8")).encode("utf-8"), ca.strip_sections(text).encode("utf-8"))
        self.assertTrue(new.startswith("# 我的設定\r\n".encode()))

    def test_unrecognised_tables_reported_when_removing_too(self):
        text = INSTALLED + '\n[mcp_servers."sampan".tools."send"]\napproval_mode = "prompt"\n'
        self.path.write_text(text)
        with self.assertRaises(ValueError) as e:
            ca.apply(self.path, False, now=NOW)
        self.assertEqual(str(e.exception), ca.NOT_TABLES)
        self.assertEqual(self.path.read_text(), text)
        self.assertEqual(self.backups(), [])

    def test_backup_in_the_same_second_keeps_the_first(self):
        self.path.write_text(INSTALLED)
        ca.apply(self.path, True, now=NOW)
        second = ca.apply(self.path, False, now=NOW)
        self.assertEqual(len(self.backups()), 2)
        self.assertEqual((self.dir / BACKUP).read_text(), INSTALLED)
        self.assertNotIn(BACKUP + "）", second)


if __name__ == "__main__":
    unittest.main()
