"""The rehearsal folder (``scripts/hosted_rehearsal_folder.py``) taken through the whole cutover in
the documented order, as the operator runs it on a preview database: three reviewer rows and the
test identity on the owner's address, parity PASS without --accept-unlisted-lines, verify PASS,
and each identity's line as the script predicted. INVENTED data on a real Postgres."""

from __future__ import annotations

import importlib.util
import os

from hosted_pg import REPO, dsn  # noqa: F401 - fixtures
from test_hosted_admin import FORBIDDEN, cli  # noqa: F401 - fixtures

from tradingbot.runtime import adjudication as A


def test_the_rehearsal_folder_goes_through_the_cutover_as_documented(
        cli, dsn, tmp_path, monkeypatch, capsys):  # noqa: F811
    spec = importlib.util.spec_from_file_location("hosted_rehearsal_folder_test",
                                                  REPO / "scripts" / "hosted_rehearsal_folder.py")
    folder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(folder)
    base = tmp_path / "adjudication"
    expected = folder.write(base)
    keys = tmp_path / "keys"
    keys.mkdir(mode=0o700)
    (keys / "owner-address").write_text("owner.rehearsal@example.invalid\n")
    os.chmod(keys / "owner-address", 0o600)
    monkeypatch.setenv(cli.URL_ENV, dsn)
    monkeypatch.delenv(cli.UNPOOLED_ENV, raising=False)
    monkeypatch.setenv(A.DIR_ENV, str(base))
    monkeypatch.setenv(cli.OWNER_ADDRESS_ENV, str(keys / "owner-address"))

    def run(*argv, code=0) -> str:
        capsys.readouterr()
        got = cli.main([str(a) for a in argv])
        printed = "".join(capsys.readouterr())
        assert not [w for w in (*FORBIDDEN, "Owner.Reviewer", "Approver.Local", "rehearsal")
                    if w in printed], printed
        assert got == code, printed
        return printed

    run("init-db", "--preview")
    run("publish-bundle", "--demo", base)  # the rehearsal's stand-in for the real bundle
    out = run("sync-reviewers", "--demo")
    assert [line.split(":")[0] for line in out.splitlines() if ": id " in line] == [
        "REMOTE-1", "REMOTE-2", "OWNER-REVIEWER"]
    assert "OWNER-REVIEWER: closed until import-log runs" in out
    out = run("sync-reviewers", "--test-identity", "TEST-1")
    assert "its sign-in address opens 2 identities" in out
    out = run("import-log", "--cutover")
    assert "local lines of reviewers that no row holds: 0" in out
    assert "marked in the adjudication folder: 3 (cutover" in out
    out = run("push-returns")
    assert "pushed 1 new return(s): REMOTE-1 1" in out
    out = run("parity")
    assert out.splitlines()[-1] == "parity of the local log and the hosted store: PASS, 3 " \
        "reviewer(s)"
    for line in expected.values():
        assert line in out.splitlines(), (line, out)
    assert "verify: PASS" in run("verify")
    assert expected["REMOTE-2"].startswith("REMOTE-2: completed 0 of ")
    assert expected["REMOTE-1"].endswith("next s02a RETURNED")
    assert expected["OWNER-REVIEWER"].startswith("OWNER-REVIEWER: completed 2 of ")
