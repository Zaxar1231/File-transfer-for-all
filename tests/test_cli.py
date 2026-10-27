from fta.cli import main


def test_cp_ls_stat_mkdir_rm(tmp_path, capsys):
    src = tmp_path / "src"
    (src / "sub").mkdir(parents=True)
    (src / "sub" / "f.txt").write_bytes(b"12345")
    dst = tmp_path / "dst"

    assert main(["mkdir", "-p", str(dst)]) == 0
    assert main(["cp", "-r", str(src), str(dst)]) == 0
    assert (dst / "src" / "sub" / "f.txt").read_bytes() == b"12345"

    capsys.readouterr()
    assert main(["ls", str(dst / "src")]) == 0
    assert "sub" in capsys.readouterr().out

    assert main(["stat", str(dst / "src" / "sub" / "f.txt")]) == 0
    assert "5" in capsys.readouterr().out

    assert main(["rm", str(dst / "src" / "sub" / "f.txt")]) == 0
    assert not (dst / "src" / "sub" / "f.txt").exists()


def test_errors_are_reported_without_traceback(tmp_path, capsys):
    assert main(["stat", str(tmp_path / "missing")]) == 1
    assert "fta: error:" in capsys.readouterr().err


def test_cp_resume_verify_and_history(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("FTA_DATA_DIR", str(tmp_path / "data"))
    payload = b"0123456789" * 30_000
    src = tmp_path / "big.bin"
    src.write_bytes(payload)
    out = tmp_path / "out"
    out.mkdir()
    (out / "big.bin").write_bytes(payload[:100_000])  # an earlier, interrupted copy

    assert main(["cp", "--resume", "--verify", str(src), str(out)]) == 0
    assert (out / "big.bin").read_bytes() == payload
    assert "verified sha256:" in capsys.readouterr().out

    assert main(["history", "--csv", str(tmp_path / "journal.csv")]) == 0
    printed = capsys.readouterr().out
    assert "done" in printed and "big.bin" in printed
    assert "resumed_from" in (tmp_path / "journal.csv").read_text().splitlines()[0]


def test_cp_failure_lands_in_the_journal(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("FTA_DATA_DIR", str(tmp_path / "data"))
    assert main(["cp", "--retries", "2", str(tmp_path / "missing"), str(tmp_path)]) == 1  # not retried
    assert main(["history"]) == 0
    assert "failed" in capsys.readouterr().out
