"""Windows-specific logic that can be exercised on any OS."""
import subprocess
import sys

import pytest

from clipper import system
from clipper.config import load_config


def test_job_object_struct_sizes_match_win64():
    import ctypes
    assert ctypes.sizeof(system._BasicLimit) == 64
    assert ctypes.sizeof(system._ExtendedLimit) == 144
    assert ctypes.sizeof(system._CpuRate) == 8


@pytest.mark.parametrize("cores,ram,expected", [(4, 8, 1), (6, 8, 2), (8, 16, 3), (16, 32, 6), (2, 4, 1)])
def test_auto_max_streams(cores, ram, expected):
    assert system.auto_max_streams(cores, ram) == expected


def test_max_streams_auto_and_cap():
    cfg = load_config(overrides={"discovery": {"max_streams": "auto", "max_streams_cap": 1}})
    assert cfg.max_streams == 1
    assert load_config(overrides={"discovery": {"max_streams": 2}}).max_streams == 2


def test_windows_filter_path_escaping():
    assert system.ffmpeg_filter_path(r"C:\Users\A B\data\x.srt") == "C\\:/Users/A B/data/x.srt"


def test_windows_fonts_and_priority_flags(monkeypatch, tmp_path):
    monkeypatch.setattr(system, "IS_WINDOWS", True)
    (tmp_path / "Fonts").mkdir()
    (tmp_path / "Fonts" / "arialbd.ttf").write_bytes(b"x")
    monkeypatch.setenv("WINDIR", str(tmp_path))
    assert system.caption_font() == "Arial"
    assert system.drawtext_font_arg().startswith("fontfile='") and "arialbd.ttf" in system.drawtext_font_arg()


def test_resolve_limits():
    assert system.resolve_limits({"max_memory_gb": "auto", "max_cpu_percent": 60}, {"ram_gb": 16, "logical_cores": 8}) == (6.4, 60)
    assert system.resolve_limits({"max_memory_gb": "auto"}, {"ram_gb": 4, "logical_cores": 4}) == (3.0, 75)
    assert system.apply_resource_limits({}) .startswith("skipped") if not system.IS_WINDOWS else True


def test_utf8_bom_config_and_env(tmp_path, monkeypatch):
    from clipper import config as c
    env = tmp_path / ".env"
    env.write_text("# c\nCLIPPER_TEST_VAR=ok\n", encoding="utf-8-sig")
    monkeypatch.delenv("CLIPPER_TEST_VAR", raising=False)
    c.load_dotenv(env)
    import os
    assert os.environ["CLIPPER_TEST_VAR"] == "ok"
    y = tmp_path / "c.yaml"
    y.write_text("detection:\n  hype_terms: ['😂']\n", encoding="utf-8-sig")
    assert c.load_config(y)["detection"]["hype_terms"] == ["😂"]


def test_streamlink_runs_as_module():
    out = subprocess.run([*system.streamlink_cmd(), "--version"], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0 and "streamlink" in out.stdout.lower()


def test_cleanup_skips_locked_files(tmp_path, monkeypatch):
    from pathlib import Path
    from clipper import storage
    f = tmp_path / "x.mp4"
    f.write_bytes(b"x")
    def locked(self, *a, **k):
        raise PermissionError("in use")
    monkeypatch.setattr(Path, "unlink", locked)
    assert storage._unlink(f) == 0
