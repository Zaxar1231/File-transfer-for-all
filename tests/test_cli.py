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
