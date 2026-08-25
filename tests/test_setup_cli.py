"""설치 관문 CLI(app/setup.py) 단위테스트 — **종료코드가 곧 게이트다.**

CLI 는 얇은 껍데기이므로 테스트도 껍데기의 계약만 본다:
    - 검증 실패 → non-zero(그리고 render 는 파일을 만들지 않는다)
    - ``--json`` 은 기계가 파싱할 수 있는 출력을 낸다
    - 파괴적 동작(덮어쓰기)은 명시 플래그 없이는 일어나지 않는다
    - 사용 오류(깨진 JSON·없는 파일)와 게이트 실패는 **다른 종료코드**다
"""

from __future__ import annotations

import json
import os

import pytest

from app import setup as CLI

GOOD_ANSWERS = {
    "consent": {"full_permissions": True, "accepted_at": "2026-08-25T09:00:00+09:00"},
    "deploy": {"profile": "cloud_vm", "host_deploy_dir": "/srv/jad",
               "secrets_base_dir": "/run/secrets"},
    "forge": {"kind": "gitlab", "token_ref": "service/forge-token"},
    "jira": {"base_url": "https://acme.atlassian.net", "project": "ACME",
             "trigger_statuses": ["To Do"],
             "watcher_token_file": "service/jira-token",
             "watcher_email": "bot@acme.example"},
    "notifier": {"provider": "none"},
    "webhook": {"enabled": True, "secret_ref": "service/jira-webhook"},
    "run": {"dlc_meta_repo_url": "https://git.example.com/acme/dlc-meta.git"},
}

NO_CONSENT = {**GOOD_ANSWERS, "consent": {"full_permissions": False}}


@pytest.fixture(autouse=True)
def _no_ambient_env(monkeypatch):
    for name in ("HOST_DEPLOY_DIR", "SECRETS_DIR", "JIRA_WATCHER_EMAIL",
                 "DLC_META_DIR", "WORKER_SHARED_SECRET"):
        monkeypatch.delenv(name, raising=False)


def _render_args(tmp_path) -> list:
    """render 의 부수효과를 tmp 안에 가둔다.

    ⚠️ ``render`` 는 worker 공유 시크릿을 ``.env`` 에 만든다(app/setup_autofill.py). 기본값은
    **cwd 의 .env** 라, 격리하지 않으면 테스트가 이 레포의 실제 .env 를 건드린다.
    """
    return ["--env-file", str(tmp_path / ".env"), "--project-dir", str(tmp_path)]


def _write_json(tmp_path, name, payload) -> str:
    path = tmp_path / name
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return str(path)


# --- validate -----------------------------------------------------------------


def test_validate_passes_with_exit_zero(tmp_path, capsys):
    assert CLI.main(["validate", _write_json(tmp_path, "a.json", GOOD_ANSWERS)]) == 0
    assert "검증 통과" in capsys.readouterr().out


def test_validate_fails_with_exit_one(tmp_path, capsys):
    code = CLI.main(["validate", _write_json(tmp_path, "a.json", NO_CONSENT)])
    assert code == CLI.EXIT_GATE_FAILED
    assert "consent.full_permissions" in capsys.readouterr().out


def test_validate_json_output_is_machine_readable(tmp_path, capsys):
    CLI.main(["validate", _write_json(tmp_path, "a.json", NO_CONSENT), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert any(f["code"] == "consent_required" for f in payload["findings"])
    assert {"level", "key", "code", "message", "hint"} == set(payload["findings"][0])


def test_validate_reads_stdin(monkeypatch, capsys):
    import io

    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(GOOD_ANSWERS)))
    assert CLI.main(["validate"]) == 0
    assert "검증 통과" in capsys.readouterr().out


# --- 사용 오류(게이트 실패와 구분된다) -----------------------------------------


def test_missing_answers_file_is_usage_error(capsys):
    with pytest.raises(SystemExit) as exc:
        CLI.main(["validate", "없는파일.json"])
    assert exc.value.code == CLI.EXIT_USAGE
    assert "없습니다" in capsys.readouterr().err


def test_broken_json_is_usage_error(tmp_path, capsys):
    path = tmp_path / "a.json"
    path.write_text("{ not json", encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        CLI.main(["validate", str(path)])
    assert exc.value.code == CLI.EXIT_USAGE
    assert "JSON" in capsys.readouterr().err


def test_non_object_json_is_usage_error(tmp_path):
    path = tmp_path / "a.json"
    path.write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        CLI.main(["validate", str(path)])
    assert exc.value.code == CLI.EXIT_USAGE


# --- render --------------------------------------------------------------------


def test_render_writes_a_loadable_config(tmp_path, capsys):
    from app import config as C

    out = str(tmp_path / "config.yaml")
    code = CLI.main(["render", _write_json(tmp_path, "a.json", GOOD_ANSWERS), "-o", out]
                    + _render_args(tmp_path))
    assert code == 0 and os.path.exists(out)
    assert "생성" in capsys.readouterr().out
    assert C.load_config(out).jira.project == "ACME"


def test_render_refuses_when_validation_fails(tmp_path, capsys):
    out = str(tmp_path / "config.yaml")
    code = CLI.main(["render", _write_json(tmp_path, "a.json", NO_CONSENT), "-o", out]
                    + _render_args(tmp_path))
    assert code == CLI.EXIT_GATE_FAILED
    assert not os.path.exists(out)          # 검증 못 넘으면 산출물이 없다
    assert "생성하지 않았습니다" in capsys.readouterr().err


def test_render_refuses_to_clobber_without_force(tmp_path, capsys):
    out = str(tmp_path / "config.yaml")
    answers = _write_json(tmp_path, "a.json", GOOD_ANSWERS)
    CLI.main(["render", answers, "-o", out] + _render_args(tmp_path))
    with pytest.raises(SystemExit) as exc:
        CLI.main(["render", answers, "-o", out] + _render_args(tmp_path))
    assert exc.value.code == CLI.EXIT_USAGE
    assert "--force" in capsys.readouterr().err


def test_render_force_backs_up(tmp_path, capsys):
    out = str(tmp_path / "config.yaml")
    answers = _write_json(tmp_path, "a.json", GOOD_ANSWERS)
    CLI.main(["render", answers, "-o", out] + _render_args(tmp_path))
    assert CLI.main(["render", answers, "-o", out, "--force"]
                    + _render_args(tmp_path)) == 0
    assert "백업" in capsys.readouterr().out
    assert any(n.startswith("config.yaml.bak-") for n in os.listdir(tmp_path))


def test_render_stdout_does_not_touch_files(tmp_path, capsys):
    out = str(tmp_path / "config.yaml")
    answers = _write_json(tmp_path, "a.json", GOOD_ANSWERS)
    assert CLI.main(["render", answers, "-o", out, "--stdout"]
                    + _render_args(tmp_path)) == 0
    assert not os.path.exists(out)
    assert "project: ACME" in capsys.readouterr().out


def test_render_json_summary(tmp_path, capsys):
    out = str(tmp_path / "config.yaml")
    CLI.main(["render", _write_json(tmp_path, "a.json", GOOD_ANSWERS), "-o", out,
              "--json"] + _render_args(tmp_path))
    payload = json.loads(capsys.readouterr().out)
    assert payload["path"] == out
    assert "jira.project" in payload["replaced"]
    assert "unanswered" in payload


def test_render_missing_template_is_usage_error(tmp_path, capsys):
    with pytest.raises(SystemExit) as exc:
        CLI.main(["render", _write_json(tmp_path, "a.json", GOOD_ANSWERS),
                  "-o", str(tmp_path / "c.yaml"), "--template", "없는템플릿.yaml"]
                 + _render_args(tmp_path))
    assert exc.value.code == CLI.EXIT_USAGE
    assert "템플릿" in capsys.readouterr().err


# --- doctor ---------------------------------------------------------------------


def _rendered_config(tmp_path, capsys=None, answers=None) -> str:
    """doctor 검사용 config.yaml 을 만든다(렌더 출력은 버린다 — 뒤 검사와 섞이지 않게)."""
    out = str(tmp_path / "config.yaml")
    CLI.main(["render", _write_json(tmp_path, "a.json", answers or GOOD_ANSWERS),
              "-o", out] + _render_args(tmp_path))
    if capsys is not None:
        capsys.readouterr()
    return out


def test_doctor_reports_failures_with_exit_one(tmp_path, capsys):
    """오프라인 검사만 골라 돌린다(네트워크 없음) — **남은 예시 자리표시자**를 잡아야 한다.

    ⚠️ 이 검사가 마지막 그물이다. dlc-meta URL 은 이제 설치 관문이 자동으로 채우지만
    (app/setup_autofill.py), 사람이 config.yaml 을 손으로 고치다 다른 조직의 예시 값을
    되돌려 놓을 수 있다 — 그건 비어 있지 않아 눈으로는 넘어가고, 그대로 두면 사내 토큰이
    엉뚱한 호스트로 나갈 수 있다. 그래서 자리표시자를 다시 심어 두고 doctor 가 잡는지 본다.
    """
    path = _rendered_config(tmp_path, capsys)
    text = open(path, encoding="utf-8").read().replace(
        "https://git.example.com/acme/dlc-meta.git",
        "https://gitlab.example.com/<your-group>/dlc-meta.git")
    open(path, "w", encoding="utf-8", newline="").write(text)
    code = CLI.main(["doctor", "--config", path, "--project-dir", str(tmp_path),
                     "--only", "config,worker_secret"])
    assert code == CLI.EXIT_GATE_FAILED
    out = capsys.readouterr().out
    assert "[FAIL] config" in out          # dlc_meta_repo_url 자리표시자가 되살아났다


def test_doctor_json_output(tmp_path, capsys):
    path = _rendered_config(tmp_path, capsys)
    CLI.main(["doctor", "--config", path, "--project-dir", str(tmp_path),
              "--only", "worker_secret", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["checks"][0]["name"] == "worker_secret"
    assert set(payload["counts"]) == {"pass", "fail", "warn", "skip"}


def test_doctor_passes_when_everything_offline_is_fine(tmp_path, capsys):
    """렌더 결과에는 자리표시자가 **남지 않는다** — 손대지 않아도 config 검사가 통과한다.

    예전에는 render 산출물에 ``dlc_meta_repo_url: https://gitlab.example.com/<your-group>/…``
    가 그대로 남아 이 검사가 실패했고, 사람이 손으로 고쳐야 했다.
    """
    path = _rendered_config(tmp_path, capsys)
    assert "<your-group>" not in open(path, encoding="utf-8").read()
    code = CLI.main(["doctor", "--config", path, "--project-dir", str(tmp_path),
                     "--only", "config"])
    assert code == 0
    assert "[PASS] config" in capsys.readouterr().out


def test_doctor_unknown_check_name_is_usage_error(tmp_path, capsys):
    path = _rendered_config(tmp_path, capsys)
    with pytest.raises(SystemExit) as exc:
        CLI.main(["doctor", "--config", path, "--only", "없는검사"])
    assert exc.value.code == CLI.EXIT_USAGE
    assert "알 수 없는 검사" in capsys.readouterr().err


def test_doctor_missing_config_is_usage_error(capsys):
    with pytest.raises(SystemExit) as exc:
        CLI.main(["doctor", "--config", "없는설정.yaml"])
    assert exc.value.code == CLI.EXIT_USAGE
    assert "로드할 수 없습니다" in capsys.readouterr().err


def test_doctor_does_not_send_notifications_by_default(tmp_path):
    """⚠️ 진단이 남의 채널에 메시지를 쏘지 않는다 — 기본값 계약."""
    parser = CLI.build_parser()
    args = parser.parse_args(["doctor"])
    assert args.send_test_notification is False


# --- 파서 자체 -------------------------------------------------------------------


def test_subcommand_is_required():
    with pytest.raises(SystemExit):
        CLI.build_parser().parse_args([])


def test_help_lists_every_doctor_check():
    """``--only`` 안내가 검사 목록과 갈라지지 않게(문서 드리프트 방지)."""
    from app import setup_doctor

    parser = CLI.build_parser()
    top_help = parser.format_help()
    assert "validate" in top_help and "render" in top_help and "doctor" in top_help
    # 서브파서 help 는 SystemExit 없이 꺼내 본다.
    sub_action = next(a for a in parser._actions if hasattr(a, "choices") and a.choices)
    doctor_help = sub_action.choices["doctor"].format_help()
    for name in setup_doctor.CHECK_ORDER:
        assert name in doctor_help


# --- discover -----------------------------------------------------------------


def test_discover_skips_gracefully_without_credentials(tmp_path, capsys):
    """토큰·이메일이 없으면 조회할 수 없다 — 실패가 아니라 건너뜀(종료코드 0)."""
    path = _rendered_config(tmp_path, capsys)
    code = CLI.main(["discover", "--config", path, "--project-dir", str(tmp_path)])
    assert code == CLI.EXIT_OK
    out = capsys.readouterr().out
    assert "[SKIP] account" in out
    assert "확정할 수 있는 값이 없었습니다" in out


def test_discover_json_output_is_machine_readable(tmp_path, capsys):
    path = _rendered_config(tmp_path, capsys)
    CLI.main(["discover", "--config", path, "--project-dir", str(tmp_path),
              "--only", "labels", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert [s["name"] for s in payload["sections"]] == ["labels"]
    assert set(payload) == {"ok", "sections", "suggested_answers"}


def test_discover_unknown_section_is_usage_error(tmp_path, capsys):
    path = _rendered_config(tmp_path, capsys)
    with pytest.raises(SystemExit) as exc:
        CLI.main(["discover", "--config", path, "--only", "없는조회"])
    assert exc.value.code == CLI.EXIT_USAGE
    assert "알 수 없는 조회 항목" in capsys.readouterr().err


def test_discover_missing_config_is_usage_error(capsys):
    with pytest.raises(SystemExit) as exc:
        CLI.main(["discover", "--config", "없는설정.yaml"])
    assert exc.value.code == CLI.EXIT_USAGE
    assert "로드할 수 없습니다" in capsys.readouterr().err


def test_discover_reports_non_zero_when_a_lookup_fails(tmp_path, capsys, monkeypatch):
    """조회 실패는 게이트 실패다 — 그대로 두면 설정을 추측으로 채우게 된다."""
    from app.jira_client import JiraError

    path = _rendered_config(tmp_path, capsys)

    class Boom:
        def myself(self):
            raise JiraError("nope", status_code=401)

    # ⚠️ 네트워크에 닿지 않는다 — 클라이언트 생성 지점만 대역으로 바꾼다.
    monkeypatch.setattr("app.setup_doctor._jira_client", lambda cfg, pd: (Boom(), ""))
    code = CLI.main(["discover", "--config", path, "--project-dir", str(tmp_path),
                     "--only", "account"])
    assert code == CLI.EXIT_GATE_FAILED
    assert "[FAIL] account" in capsys.readouterr().out


# --- 자동 채움(dlc-meta URL · worker 공유 시크릿) --------------------------------


def _fake_git_clone(tmp_path, url, name="dlc-meta"):
    """origin 이 ``url`` 인 것처럼 보이는 클론을 만든다(git 은 실제로 부르지 않는다)."""
    path = tmp_path / name
    (path / ".git").mkdir(parents=True)
    import subprocess

    def fake_run(cmd, **kwargs):
        from types import SimpleNamespace

        if cmd[:2] == ["git", "-C"] and cmd[2] == str(path):
            return SimpleNamespace(returncode=0, stdout=url + "\n", stderr="")
        return SimpleNamespace(returncode=128, stdout="", stderr="not a repo")

    return str(path), fake_run


def test_render_injects_the_dlc_meta_url_from_a_clone(tmp_path, capsys, monkeypatch):
    """설치자가 손으로 적지 않아도 dlc-meta 원격 URL 이 config.yaml 에 들어간다.

    그리고 그 값에서 forge base_url 이 파생돼야 한다 — 예전에는 예시 URL 이 그대로 남아
    사내 토큰이 남의 호스트로 나갈 수 있었다.
    """
    from app import config as C
    from app import forge as F
    from app import setup_autofill as A

    clone, fake_run = _fake_git_clone(tmp_path, "https://git.corp.example/acme/dlc-meta.git")
    monkeypatch.setattr(A.subprocess, "run", fake_run)

    answers = {k: v for k, v in GOOD_ANSWERS.items() if k != "run"}
    out = str(tmp_path / "config.yaml")
    code = CLI.main(["render", _write_json(tmp_path, "a.json", answers), "-o", out,
                     "--dlc-meta", clone] + _render_args(tmp_path))
    assert code == 0
    text = capsys.readouterr().out
    assert "자동 채움: run.dlc_meta_repo_url" in text
    assert "<your-group>" not in open(out, encoding="utf-8").read()

    cfg = C.load_config(out)
    assert cfg.run.dlc_meta_repo_url == "https://git.corp.example/acme/dlc-meta.git"
    # ⚠️ 주입이 파생까지 먹는지 — base_url 이 그 호스트에서 유도돼야 한다.
    assert cfg.forge.base_url == "https://git.corp.example"
    assert cfg.forge.base_url_source == F.SOURCE_DERIVED
    assert cfg.forge.base_url_origin == "run.dlc_meta_repo_url"


def test_render_infers_forge_kind_from_the_injected_url(tmp_path, capsys, monkeypatch):
    from app import config as C
    from app import setup_autofill as A

    clone, fake_run = _fake_git_clone(tmp_path, "https://github.example.com/acme/dlc-meta.git")
    monkeypatch.setattr(A.subprocess, "run", fake_run)

    answers = {k: v for k, v in GOOD_ANSWERS.items() if k not in ("run", "forge")}
    answers["forge"] = {"token_ref": "service/forge-token"}   # kind 는 답하지 않는다
    out = str(tmp_path / "config.yaml")
    assert CLI.main(["render", _write_json(tmp_path, "a.json", answers), "-o", out,
                     "--dlc-meta", clone] + _render_args(tmp_path)) == 0
    capsys.readouterr()
    assert C.load_config(out).forge.kind == "github"


def test_render_refuses_when_the_dlc_meta_url_cannot_be_found(tmp_path, capsys,
                                                              monkeypatch):
    """못 채우면 **예시 값이 남는 대신** 게이트가 막는다(그리고 고치는 법을 말한다)."""
    from app import setup_autofill as A

    monkeypatch.setattr(A, "candidate_clone_paths", lambda **kw: [])
    answers = {k: v for k, v in GOOD_ANSWERS.items() if k != "run"}
    out = str(tmp_path / "config.yaml")
    code = CLI.main(["render", _write_json(tmp_path, "a.json", answers), "-o", out]
                    + _render_args(tmp_path))
    assert code == CLI.EXIT_GATE_FAILED
    assert not os.path.exists(out)
    captured = capsys.readouterr()
    assert "--dlc-meta" in captured.out
    assert "run.dlc_meta_repo_url" in captured.out


def test_no_autofill_flag_turns_the_injection_off(tmp_path, capsys, monkeypatch):
    from app import setup_autofill as A

    clone, fake_run = _fake_git_clone(tmp_path, "https://git.corp.example/acme/dlc-meta.git")
    monkeypatch.setattr(A.subprocess, "run", fake_run)
    answers = {k: v for k, v in GOOD_ANSWERS.items() if k != "run"}
    code = CLI.main(["validate", _write_json(tmp_path, "a.json", answers),
                     "--dlc-meta", clone, "--no-autofill"])
    assert code == CLI.EXIT_GATE_FAILED           # 채우지 않았으니 필수 누락으로 막힌다


def test_render_creates_the_worker_shared_secret_without_printing_it(tmp_path, capsys):
    from app import setup_autofill as A

    env_file = tmp_path / ".env"
    out = str(tmp_path / "config.yaml")
    assert CLI.main(["render", _write_json(tmp_path, "a.json", GOOD_ANSWERS), "-o", out]
                    + _render_args(tmp_path)) == 0
    captured = capsys.readouterr()
    assert "worker 공유 시크릿" in captured.out

    body = env_file.read_text(encoding="utf-8")
    value = body.split("WORKER_SHARED_SECRET=", 1)[1].strip()
    assert len(value) == A.SECRET_BYTES * 2
    # ⚠️ 값이 표준출력·표준에러 어디에도 실리지 않는다.
    assert value not in captured.out and value not in captured.err

    # 두 번째 render 는 같은 값을 유지한다(멱등 — 재생성하면 워커가 전부 401).
    CLI.main(["render", _write_json(tmp_path, "a.json", GOOD_ANSWERS), "-o", out,
              "--force"] + _render_args(tmp_path))
    assert env_file.read_text(encoding="utf-8") == body


def test_render_json_summary_carries_autofill_and_secret_status(tmp_path, capsys):
    out = str(tmp_path / "config.yaml")
    CLI.main(["render", _write_json(tmp_path, "a.json", GOOD_ANSWERS), "-o", out,
              "--json"] + _render_args(tmp_path))
    payload = json.loads(capsys.readouterr().out)
    assert payload["worker_secret"]["status"] == "generated"
    assert "value" not in payload["worker_secret"]         # 값은 담지 않는다
    assert payload["autofill"]["filled"] == []             # 답변에 이미 있었다


def test_render_stdout_does_not_create_the_env_secret(tmp_path, capsys):
    """``--stdout`` 은 '파일을 건드리지 않는다'는 약속이다."""
    out = str(tmp_path / "config.yaml")
    CLI.main(["render", _write_json(tmp_path, "a.json", GOOD_ANSWERS), "-o", out,
              "--stdout"] + _render_args(tmp_path))
    assert not os.path.exists(str(tmp_path / ".env"))
