import dataclasses
import json
import tempfile
import unittest
from pathlib import Path

from sampan.marketplace_view import (
    MarketChange,
    build_market_rows,
    market_card,
    market_choices,
    market_preview,
    plan_market,
)
from sampan.plugin_view import snapshot
from sampan.plugins import read_state
from tests.helpers import km_source, local_tz, make_env
from tests.test_plugin_view import write_mkt


def write_known(cfg, profile, known):
    """known: {name: (source or None, lastUpdated or None, autoUpdate or None)}"""
    pd = cfg.profiles[profile].config_dir / "plugins"
    pd.mkdir(parents=True, exist_ok=True)
    km = {}
    for n, (src, upd, auto) in known.items():
        km[n] = {"source": km_source(src), **({"lastUpdated": upd} if upd else {}), **({} if auto is None else {"autoUpdate": auto})}
    (pd / "known_marketplaces.json").write_text(json.dumps(km))


def set_plugins(cfg, profile, plugins):
    cfg.profiles[profile] = dataclasses.replace(cfg.profiles[profile], plugins=tuple(plugins))


class MarketTest(unittest.TestCase):
    def setUp(self):
        self.cfg = make_env(Path(tempfile.mkdtemp()))

    def rows(self):
        return {r.name: r for r in build_market_rows(self.cfg, snapshot(self.cfg))}

    def test_market_info_read(self):
        write_known(self.cfg, "alice", {"my-plugins": ("owner/my-plugins", "2026-10-23T02:30:00Z", True), "x": (None, None, None)})
        info = read_state(self.cfg.profiles["alice"]).market_info
        self.assertEqual(info["my-plugins"], {"updated": "2026-10-23T02:30:00Z", "auto": True})
        self.assertEqual(info["x"], {"updated": None, "auto": None})

    def test_market_rows_states(self):
        write_known(self.cfg, "alice", {"my-plugins": ("owner/my-plugins", None, None),
                                        "claude-plugins-official": ("someone/else", None, None),
                                        "stray": ("o/stray", None, None)})
        write_known(self.cfg, "work", {"my-plugins": ("owner/my-plugins", None, None)})
        r = self.rows()
        self.assertEqual(list(r), ["claude-plugins-official", "my-plugins", "stray"])
        self.assertEqual(r["stray"].state, "清單外")
        self.assertEqual(r["claude-plugins-official"].state, "來源不同")
        self.assertEqual(r["my-plugins"].state, "一致")
        self.assertEqual(r["my-plugins"].registered, {"alice": "owner/my-plugins", "work": "owner/my-plugins"})
        self.assertEqual(r["my-plugins"].total, 2)
        self.assertEqual(r["my-plugins"].used, {"alice": ["tools@my-plugins"], "work": ["tools@my-plugins"]})
        set_plugins(self.cfg, "alice", ["superpowers@claude-plugins-official"])
        set_plugins(self.cfg, "work", [])
        self.assertEqual(self.rows()["my-plugins"].state, "沒用到")

    def test_market_card_exact(self):
        write_known(self.cfg, "alice", {"my-plugins": ("owner/my-plugins", "2026-10-23T02:30:00+00:00", True)})
        write_known(self.cfg, "work", {"my-plugins": ("other/fork", None, False)})
        write_mkt(self.cfg, "alice", "my-plugins", [{"name": "tools"}, {"name": "more"}])
        write_mkt(self.cfg, "alice", "claude-plugins-official", [{"name": "zzz"}])
        with local_tz("Asia/Taipei"):
            snap = snapshot(self.cfg)
            row = {r.name: r for r in build_market_rows(self.cfg, snap)}["my-plugins"]
            lines = market_card(self.cfg, snap, row)
        self.assertEqual(lines, [
            "my-plugins", "─" * 30, "來源 owner/my-plugins",
            "alice       已加入  更新於 10-23 10:30  自動更新 開",
            "work        已加入  更新於 —  自動更新 關  來源 other/fork",
            "目錄有 2 個 plugin"])

    def test_market_card_unregistered_unknown_auto_and_outside_config(self):
        write_known(self.cfg, "alice", {"stray": ("o/stray", "2026-10-23T02:30:00Z", None)})
        snap = snapshot(self.cfg)
        row = {r.name: r for r in build_market_rows(self.cfg, snap)}["stray"]
        with local_tz("Asia/Tokyo"):
            lines = market_card(self.cfg, snap, row)
        self.assertEqual(lines[2], "來源 （不在設定檔）")
        self.assertEqual(lines[3], "alice       已加入  更新於 10-23 11:30  自動更新 ？")
        self.assertEqual(lines[4], "work        —")
        self.assertEqual(lines[5], "目錄有 0 個 plugin")

    def test_market_choices_disabled_when_listed_plugins_use_it(self):
        write_known(self.cfg, "alice", {"my-plugins": ("owner/my-plugins", None, None)})
        write_known(self.cfg, "work", {"my-plugins": ("other/fork", None, None)})
        ch = {c.profile: c for c in market_choices(self.cfg, snapshot(self.cfg), "my-plugins", "owner/my-plugins")}
        self.assertEqual(ch["alice"].installed, "owner/my-plugins")
        self.assertTrue(ch["alice"].listed)
        self.assertEqual(ch["alice"].disabled, "清單還有 1 個 plugin 用它（先在 plugin 頁移掉）")
        self.assertEqual(ch["work"].disabled, "來源不同：other/fork")
        set_plugins(self.cfg, "alice", [])
        ch = {c.profile: c for c in market_choices(self.cfg, snapshot(self.cfg), "my-plugins", "owner/my-plugins")}
        self.assertIsNone(ch["alice"].disabled)
        self.assertFalse(ch["alice"].listed)

    def test_market_choices_missing_dir_and_unregistered(self):
        import shutil
        shutil.rmtree(self.cfg.profiles["work"].config_dir)
        ch = {c.profile: c for c in market_choices(self.cfg, snapshot(self.cfg), "my-plugins", "owner/my-plugins")}
        self.assertEqual(ch["work"].disabled, "目錄不存在")
        self.assertIsNone(ch["alice"].installed)
        self.assertIsNone(ch["alice"].disabled)

    def test_plan_market_add_remove_with_drops_and_skip_disabled(self):
        cfg = self.cfg
        set_plugins(cfg, "work", [])
        write_known(cfg, "work", {"my-plugins": ("owner/my-plugins", None, None)})
        pd = cfg.profiles["work"].config_dir / "plugins"
        (pd / "installed_plugins.json").write_text(json.dumps({"version": 2, "plugins": {
            "b@my-plugins": [{"scope": "user", "version": "1"}], "a@my-plugins": [{"scope": "user", "version": "1"}],
            "o@other": [{"scope": "user", "version": "1"}]}}))
        snap = snapshot(cfg)
        ch = market_choices(cfg, snap, "my-plugins", "owner/my-plugins")
        # alice: listed + unregistered -> addable; work: registered, unlisted -> removable
        self.assertEqual(plan_market(snap, ch, {"alice": True, "work": False}, "my-plugins"),
                         [MarketChange("alice", True, ()), MarketChange("work", False, ("a@my-plugins", "b@my-plugins"))])
        self.assertEqual(plan_market(snap, ch, {"alice": False, "work": True}, "my-plugins"), [])
        # a disabled choice is skipped even when toggled
        write_known(cfg, "alice", {"my-plugins": ("owner/my-plugins", None, None)})
        snap = snapshot(cfg)
        ch = market_choices(cfg, snap, "my-plugins", "owner/my-plugins")
        self.assertIsNotNone(ch[0].disabled)
        self.assertEqual(plan_market(snap, ch, {"alice": False, "work": True}, "my-plugins"), [])

    def test_market_preview_exact(self):
        changes = [MarketChange("alice", True, ()), MarketChange("work", False, ("a@m", "b@m")),
                   MarketChange("alice", False, ())]
        cfg = self.cfg
        self.assertEqual(market_preview(cfg, "m", changes, new=True), [
            "將會：",
            "  + 加入到 alice（alice@example.com）",
            "  − 從 work（work@example.com） 移除（連同已裝的 a、b 一起解除安裝）",
            "  − 從 alice（alice@example.com） 移除",
            "  [marketplaces] 會加上這個來源（名稱加入後才知道）",
            f"設定檔 {cfg.path} 會一起更新（先備份）"])
        self.assertEqual(market_preview(cfg, "m", changes[:1], new=False)[-2:],
                         ["  + 加入到 alice（alice@example.com）", f"設定檔 {cfg.path} 會一起更新（先備份）"])

    def test_market_choices_outside_config_accepts_any_registered_source(self):
        # R7: a marketplace not in [marketplaces] (source None) stays removable whatever source the profile has
        set_plugins(self.cfg, "alice", [])
        write_known(self.cfg, "alice", {"stray": ("other/fork", None, None)})
        write_known(self.cfg, "work", {"stray": (None, None, None)})
        ch = {c.profile: c for c in market_choices(self.cfg, snapshot(self.cfg), "stray", None)}
        self.assertIsNone(ch["alice"].disabled)
        self.assertIsNone(ch["work"].disabled)
        self.assertEqual(ch["alice"].installed, "other/fork")
        snap = snapshot(self.cfg)
        self.assertEqual(plan_market(snap, list(ch.values()), {"alice": False, "work": False}, "stray"),
                         [MarketChange("alice", False, ()), MarketChange("work", False, ())])


if __name__ == "__main__":
    unittest.main()
