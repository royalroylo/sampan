"""The repo must not name the author's machine, mail domain or clients.

The forbidden words are kept only as sha256 hashes, so this file does not ship them in plain text.
Method: lowercase each tracked file (and its path), take every run of [a-z0-9] joined by dots
(`foo`, `a.b.c`), and hash every token and every contiguous dot-joined slice of a run (`a.b.c` →
`a`, `b`, `c`, `a.b`, `b.c`, `a.b.c`). A forbidden word or domain shows up as one of those pieces
whenever it is written as its own word, path segment or domain; a word glued into a longer one
(`xfoo`) is not caught. To add a word: append sha256(word.lower()).
"""
import hashlib
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FORBIDDEN_SHA256 = frozenset({
    "22564f29b8930ff5c85a36e0bb5389dfd8de0a288af8a4902ed7e1ca11a65886",
    "adbe7e50ced1f133743ffb4cc52aa7de03989c37b1726bc01c9c57b7d939c46c",
    "489d7a38ff7431e9c8e8f2155f23b776e5b79c235c25c70b6c5e975e1b0fb6cd",
    "b86debb6516566ae58e6ba3750e7e7fc82534634ee85ad0a5c159e3ae9fff183",
    "17449f163d02d432e2f127b389f9f586b3458733f1eeac93253aea83758e1dc0",
    "48a91a363d070f88eb0c6ea6d774daa639de21d6d8b1c69c80a57b36d4efc911",
    "7dfd82141b234ccc6c20383d55e093af0fd17918f0173ecaf61106b206c3bb32",
    "fd93b42396823623306f45c536e2fc1ff233e7a2b3ef4a8443e0eeeba129fa01",
    "0a99f935decf19c1782062aacd15df027aea90cc5658f9d520f65689b6a3de26",
    "49a16a79d6770d320c128d46f7ecee23dee8577f223b6670a522cbad0ad182c2",
    "4f38751d027d09c4001cdd8e5f02a4ac8372f531d3187d602fb12d45e96fafc7",
    "9b26c65fe33bf161eea7edd9f1e361783a2ee985b8d5036ddc5b6029f957cd0d",
    "a59b1da0739609cfd823420304bebb268693eade4ff3e1269fce24548c301240",
    "5f0974ee455c4cd57c58dfb04f3d070b1f365d0ed4401dbf28089b308b019a67",
    "2db76f21528566a3f39cdaa9f9c8794ae3193e62c8262fd1a476d9cc335f45b5",
    "a4c449f24d80ea154b09b0687155fb9cf04f03adc7dd186b748bbd3bb00a9cf2",
    "96a4bc2602655473120fcc571ee3d8cfe5f8801f8038ccc06323d305e323331c",
    "ba0059640590b09fb88692c4b81330f774597ea610076cbd99eb6df7d53477aa",
    "795b104abe3e4134960ca245ded0e617f162c751209347b7f003bc35e062f43e",
    "0572a13f04dcbfae923408ecb9cf0bcadb4f9b277c1cb13a34a924066597503b",
    "267cd9d4d871076982f37fe96fc5f2ee3ecc3f70744eafcea67924c33e897a06",
})
RUN_RE = re.compile(r"[a-z0-9]+(?:\.[a-z0-9]+)*")


def _sha(s):
    return hashlib.sha256(s.encode()).hexdigest()


def pieces(text):
    for run in set(RUN_RE.findall(text.lower())):
        parts = run.split(".")
        for i in range(len(parts)):
            for j in range(i + 1, len(parts) + 1):
                yield ".".join(parts[i:j])


def hits(root, files, hashes=FORBIDDEN_SHA256):
    """`file:line` of every line holding a piece whose hash is forbidden (line 0 = the path itself)."""
    found = []
    for f in files:
        path = Path(root) / f
        if not path.is_file():
            continue
        lines = [f] + path.read_text(errors="ignore").splitlines()
        found += [f"{f}:{n}" for n, line in enumerate(lines) if any(_sha(p) in hashes for p in pieces(line))]
    return found


class Scrub(unittest.TestCase):
    def test_no_personal_strings(self):
        r = subprocess.run(["git", "ls-files", "-co", "--exclude-standard"], cwd=ROOT, capture_output=True, text=True)
        if r.returncode == 0:
            files = r.stdout.splitlines()
        else:  # not a git checkout (a source archive, as in scripts/linux-smoke): scan every file
            skip = {"__pycache__", ".venv", ".git"}
            files = [str(p.relative_to(ROOT)) for p in ROOT.rglob("*")
                     if p.is_file() and not skip & set(p.relative_to(ROOT).parts)]
        self.assertTrue(files)
        self.assertEqual(hits(ROOT, files), [])

    def test_catches_words_and_domains_but_not_neighbours(self):
        hashes = {_sha("zzforbidden"), _sha("zz.example.test")}
        with tempfile.TemporaryDirectory() as d:
            files = {"a.txt": "ok\nsee /Users/ZZForbidden/x\n", "b.txt": "mail bob@mx.zz.example.test\n",
                     "c.txt": "zzforbiddenx zz.example.org example.test\n", "zzforbidden/d.txt": "fine\n"}
            for name, body in files.items():
                (Path(d) / name).parent.mkdir(parents=True, exist_ok=True)
                (Path(d) / name).write_text(body)
            self.assertEqual(hits(d, list(files), hashes), ["a.txt:2", "b.txt:1", "zzforbidden/d.txt:0"])


if __name__ == "__main__":
    unittest.main()
