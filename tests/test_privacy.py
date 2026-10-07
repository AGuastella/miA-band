"""Guards against committing personal data.

1. Always: no data files, databases or archives are tracked, and no email addresses appear.
2. If a git-ignored `.privacy-denylist` exists (one term per line, `#` for comments; put your
   account id, name, city, device serials... in it), no tracked file may contain any of its
   terms. The list stays local, so the terms themselves are never published.
"""
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DATA_SUFFIXES = {".csv", ".sqlite", ".db", ".duckdb", ".zip", ".gz", ".parquet", ".json"}
VENDORED = {"miaband/web/static/chart.umd.js"}
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
ALLOWED_EMAILS = re.compile(r"@(example\.(com|org)|users\.noreply\.github\.com|anthropic\.com)$")


def tracked() -> list[str]:
    try:
        out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not a git checkout")
    return [p for p in out.splitlines() if p]


def text_files():
    for rel in tracked():
        p = ROOT / rel
        if rel in VENDORED or p.suffix in {".png", ".jpg", ".ico"} or not p.is_file():
            continue
        try:
            yield rel, p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue


def test_no_data_files_tracked():
    bad = [p for p in tracked() if Path(p).suffix in DATA_SUFFIXES or p.startswith("data/")
           or Path(p).name == "Gadgetbridge"]
    assert not bad, f"data files must never be committed: {bad}"


def test_no_email_addresses():
    hits = [f"{rel}: {m}" for rel, txt in text_files() for m in EMAIL.findall(txt)
            if not ALLOWED_EMAILS.search(m)]
    assert not hits, "email addresses in tracked files:\n" + "\n".join(hits)


def test_no_denylisted_terms():
    deny = ROOT / ".privacy-denylist"
    if not deny.exists():
        pytest.skip("no .privacy-denylist (create one locally to enable this check)")
    terms = [t.strip() for t in deny.read_text(encoding="utf-8").splitlines()
             if t.strip() and not t.strip().startswith("#")]
    hits = []
    for rel, txt in text_files():
        low = txt.lower()
        for t in terms:
            if t.lower() in low:
                line = low[:low.index(t.lower())].count("\n") + 1
                hits.append(f"{rel}:{line}: contains a denylisted term")
    assert not hits, "\n".join(hits)
