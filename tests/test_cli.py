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


def test_encrypt_decrypt_and_keygen(tmp_path, capsys, monkeypatch):
    from fta.crypto import age

    monkeypatch.setenv("FTA_DATA_DIR", str(tmp_path / "data"))
    secret = tmp_path / "plans.txt"
    secret.write_text("meet at noon")

    assert main(["keygen"]) == 0
    mine = capsys.readouterr().out.strip()
    assert mine.startswith("age1") and main(["keygen"]) == 0 and capsys.readouterr().out.strip() == mine

    assert main(["encrypt", "-r", mine, str(secret)]) == 0
    sealed = tmp_path / "plans.txt.age"
    assert age.is_encrypted(sealed)
    secret.unlink()
    assert main(["decrypt", str(sealed)]) == 0  # this device's own key opens it
    assert secret.read_text() == "meet at noon"

    answers = iter(["hunter22", "hunter22", "hunter22"])
    monkeypatch.setattr("getpass.getpass", lambda prompt="": next(answers))
    other = tmp_path / "other.txt"
    other.write_text("by password")
    assert main(["encrypt", str(other)]) == 0
    other.unlink()
    assert main(["decrypt", str(tmp_path / "other.txt.age")]) == 0
    assert other.read_text() == "by password"

    monkeypatch.setattr("getpass.getpass", lambda prompt="": "wrong-password")
    assert main(["decrypt", str(tmp_path / "other.txt.age")]) == 1
    assert "wrong passphrase" in capsys.readouterr().err


def test_share_command_serves_one_download(tmp_path, monkeypatch):
    import os
    import re
    import signal
    import ssl
    import subprocess
    import sys
    import urllib.request

    payload = tmp_path / "photo.jpg"
    payload.write_bytes(os.urandom(50_000))
    env = {**os.environ, "FTA_DATA_DIR": str(tmp_path / "data")}
    process = subprocess.Popen(
        [sys.executable, "-m", "fta", "share", str(payload), "--ttl", "10m"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
    )
    try:
        url = ""
        for _ in range(5):
            line = process.stdout.readline()
            if line.startswith("https://"):
                url = re.sub(r"https://[^/:]+", "https://127.0.0.1", line.strip())
                break
        assert url, "no link printed"
        context = ssl.create_default_context()
        context.check_hostname, context.verify_mode = False, ssl.CERT_NONE
        with urllib.request.urlopen(url, context=context, timeout=10) as response:
            assert response.read() == payload.read_bytes()
        assert process.wait(15) == 0  # a one-time link ends the command after its download
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGINT)
            process.wait(10)
