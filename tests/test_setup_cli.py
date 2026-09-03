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
                 "DLC_META_DIR", "JAD_SETUP_ACTOR"):
        monkeypatch.delenv(name, raising=False)


def _grant_consent(tmp_path, **kwargs) -> str:
    """사람 동의 **증서**를 tmp 에 만든다 → 그 경로.

    3차 리허설 이후 답변 파일의 ``consent.full_permissions: true`` 만으로는 통과하지
    않는다 — 동의가 **어디서 왔는지**를 증명하는 증서가 있어야 한다
    (:mod:`app.setup_consent`). 정상 경로 테스트는 사람이 동의한 상태를 재현한다.
    """
    from app import setup_consent as C

    record = C.ConsentRecord(
        full_permissions=kwargs.get("full_permissions", True),
        accepted_at="2026-08-25T09:00:00+09:00",
        channel=kwargs.get("channel", C.CHANNEL_HUMAN),
        granted_by=kwargs.get("granted_by", "installer@acme.example"),
        relayed_by=kwargs.get("relayed_by", ""),
        statement=kwargs.get("statement", ""),
    )
    return C.save_record(record, project_dir=str(tmp_path))


def _render_args(tmp_path) -> list:
    """render 의 부수효과(dlc-meta 자동 탐색 기준점)를 tmp 안에 가둔다.

    ⚠️ 여기서 동의 증서도 함께 만든다 — ``--project-dir`` 가 증서를 찾는 기준점이기도
    하다. 증서 없는 경로는 아래 전용 테스트가 따로 본다.
    """
    _grant_consent(tmp_path)
    return ["--project-dir", str(tmp_path)]


def _write_json(tmp_path, name, payload) -> str:
    path = tmp_path / name
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return str(path)


# --- validate -----------------------------------------------------------------


def test_validate_passes_with_exit_zero(tmp_path, capsys):
    assert CLI.main(["validate", _write_json(tmp_path, "a.json", GOOD_ANSWERS)]
                    + _render_args(tmp_path)) == 0
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


def test_validate_reads_stdin(monkeypatch, capsys, tmp_path):
    import io

    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(GOOD_ANSWERS)))
    assert CLI.main(["validate", "--consent-record", _grant_consent(tmp_path)]) == 0
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


def test_render_writes_profile_derived_values(tmp_path, capsys):
    """결함 재현 방지 — 프로파일을 골랐으면 산출물이 그 프로파일과 **일치**해야 한다.

    예전에는 ``local`` 로 답해도 예시 파일의 ``cloud_vm`` 값
    (``tcp://socket-proxy:2375``)이 그대로 남았고, 경고에는 "답하지 않아 예시 값이
    남은 항목"으로 ``deploy.docker_host`` 가 찍혔다. 문서(INSTALL §2.2)는 "프로파일
    하나면 끝난다"고 약속하므로 그건 문서와 산출물의 모순이다.
    """
    from app import config as C

    answers = json.loads(json.dumps(GOOD_ANSWERS))
    answers["deploy"] = {"profile": "local", "secrets_base_dir": str(tmp_path / "secrets")}
    out = str(tmp_path / "config.yaml")
    code = CLI.main(["render", _write_json(tmp_path, "a.json", answers), "-o", out]
                    + _render_args(tmp_path))
    assert code == 0
    cfg = C.load_config(out)
    assert cfg.deploy.profile == "local"
    assert cfg.deploy.docker_host == "tcp://socket-proxy:2375"
    assert cfg.deploy.workspace_volume == "jad-workspace"
    # 파생된 항목은 더 이상 "답하지 않아 예시 값이 남은 항목" 경고에 뜨지 않는다.
    captured = capsys.readouterr().out
    assert "deploy.docker_host" not in captured
    assert "deploy.workspace_volume" not in captured


def test_render_keeps_an_explicit_docker_host_over_the_profile(tmp_path, capsys):
    """소켓 직결(고급 대안)을 **명시**하면 프로파일 파생을 이긴다."""
    from app import config as C

    answers = json.loads(json.dumps(GOOD_ANSWERS))
    answers["deploy"] = {"profile": "local",
                         "secrets_base_dir": str(tmp_path / "secrets"),
                         "docker_host": "unix:///var/run/docker.sock"}
    out = str(tmp_path / "config.yaml")
    assert CLI.main(["render", _write_json(tmp_path, "a.json", answers), "-o", out]
                    + _render_args(tmp_path)) == 0
    assert C.load_config(out).deploy.docker_host == "unix:///var/run/docker.sock"


def test_render_refuses_when_validation_fails(tmp_path, capsys):
    # ⚠️ 여기서는 동의 증서를 만들지 **않는다** — 증서가 있으면 그것이 정본이라 답변의
    #    false 를 덮어쓴다(증서가 이긴다). 검증 실패를 보려면 동의 자체가 없어야 한다.
    out = str(tmp_path / "config.yaml")
    code = CLI.main(["render", _write_json(tmp_path, "a.json", NO_CONSENT), "-o", out,
                     "--project-dir", str(tmp_path)])
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
                     "--only", "config,secrets"])
    assert code == CLI.EXIT_GATE_FAILED
    out = capsys.readouterr().out
    assert "[FAIL] config" in out          # dlc_meta_repo_url 자리표시자가 되살아났다


def test_doctor_json_output(tmp_path, capsys):
    path = _rendered_config(tmp_path, capsys)
    CLI.main(["doctor", "--config", path, "--project-dir", str(tmp_path),
              "--only", "secrets", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["checks"][0]["name"] == "secrets"
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


def test_discover_without_any_input_is_a_usage_error_that_names_both_routes(tmp_path, capsys):
    """설정도 답변도 없으면 사용 오류 — 다만 **두 갈래를 모두** 알려 준다."""
    with pytest.raises(SystemExit) as exc:
        CLI.main(["discover", "--config", str(tmp_path / "없는설정.yaml"),
                  "--project-dir", str(tmp_path)])
    assert exc.value.code == CLI.EXIT_USAGE
    err = capsys.readouterr().err
    assert "조회에 쓸 입력이 없습니다" in err
    assert "--answers" in err and "jira.base_url" in err


def test_discover_broken_config_is_still_a_usage_error(tmp_path, capsys):
    """있는데 깨진 설정은 여전히 사용 오류다(조용히 답변 파일로 도망가지 않는다)."""
    bad = tmp_path / "config.yaml"
    bad.write_text("role: [불완전\n", encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        CLI.main(["discover", "--config", str(bad), "--project-dir", str(tmp_path)])
    assert exc.value.code == CLI.EXIT_USAGE
    assert "로드할 수 없습니다" in capsys.readouterr().err


# --- 순환 의존 회귀: config.yaml 없이도 조회가 된다 --------------------------------

#: 조회에 필요한 **최소 답변** — 문서가 "이 셋만 채워도 돈다"고 말하는 바로 그 셋.
_MINIMAL_DISCOVER_ANSWERS = {
    "jira": {"base_url": "https://x.atlassian.net",
             "watcher_email": "bot@example.com",
             "watcher_token_file": "service/jira-token"},
}


def _stub_discover(monkeypatch, seen: dict):
    """실제 네트워크 대신 cfg 만 받아 적는 대역을 심는다."""
    from app import setup_discover as D

    def fake(cfg, **kwargs):
        seen["base_url"] = cfg.jira.base_url
        seen["email"] = cfg.jira.watcher_email
        return D.DiscoveryResult()

    monkeypatch.setattr(D, "discover", fake)


def test_discover_runs_from_an_answers_file_without_any_config(tmp_path, monkeypatch):
    """**A 회귀**: config.yaml 이 없어도 답변 파일만으로 조회가 돈다.

    예전에는 조회하려면 ``render`` 가 먼저였고, ``render`` 는 전체 검증을 돌려 조회가
    채워 주려던 값(``jira.trigger_statuses``)이 없으면 죽었다 — 빈 디렉토리에서 문서를
    그대로 따라가면 그 자리에서 막히는 순환이었다.
    """
    seen: dict = {}
    _stub_discover(monkeypatch, seen)
    answers = _write_json(tmp_path, "setup-answers.json", _MINIMAL_DISCOVER_ANSWERS)
    assert CLI.main(["discover", "--answers", answers, "--project-dir", str(tmp_path),
                     "--config", str(tmp_path / "없다.yaml")]) == CLI.EXIT_OK
    assert seen == {"base_url": "https://x.atlassian.net", "email": "bot@example.com"}


def test_discover_falls_back_to_the_answers_file_and_says_so(tmp_path, monkeypatch, capsys):
    """플래그 없이도 막히지 않는다 — 다만 무엇을 읽었는지 **말한다**(조용한 폴백 금지)."""
    seen: dict = {}
    _stub_discover(monkeypatch, seen)
    _write_json(tmp_path, "setup-answers.json", _MINIMAL_DISCOVER_ANSWERS)
    assert CLI.main(["discover", "--project-dir", str(tmp_path),
                     "--config", str(tmp_path / "없다.yaml")]) == CLI.EXIT_OK
    assert seen["base_url"] == "https://x.atlassian.net"
    assert "setup-answers.json" in capsys.readouterr().err


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


def test_render_no_longer_creates_a_worker_shared_secret(tmp_path, capsys):
    """설치 관문은 **아무것도 인증하지 않는 시크릿**을 더 이상 만들지 않는다.

    옛 ``WORKER_SHARED_SECRET`` 은 워커가 중앙의 dispatch HTTP 를 부를 때 쓰는
    ``X-Worker-Secret`` 값이었다. 그 서빙 표면과 폴링 소비자가 프랙탈 seam(중앙 →
    docker exec 푸시)으로 대체되며 읽는 곳이 사라졌으므로, 설치자가 **왜 만드는지 모르는
    값**을 만들게 하지 않는다. ``.env`` 자체는 여전히 쓰인다(CLAUDE_CODE_OAUTH_TOKEN) —
    다만 이 명령이 거기에 쓰지 않는다.
    """
    out = str(tmp_path / "config.yaml")
    assert CLI.main(["render", _write_json(tmp_path, "a.json", GOOD_ANSWERS), "-o", out,
                     "--json"] + _render_args(tmp_path)) == 0
    captured = capsys.readouterr()
    assert "WORKER_SHARED_SECRET" not in captured.out + captured.err
    assert not os.path.exists(str(tmp_path / ".env"))
    payload = json.loads(captured.out)
    assert "worker_secret" not in payload
    assert payload["autofill"]["filled"] == []             # 답변에 이미 있었다


def test_retired_env_secret_flags_are_gone(tmp_path):
    """``--env-file``·``--no-env-secret`` 은 공유 시크릿 전용이었다 — 함께 은퇴했다."""
    out = str(tmp_path / "config.yaml")
    answers = _write_json(tmp_path, "a.json", GOOD_ANSWERS)
    for flag in (["--env-file", str(tmp_path / ".env")], ["--no-env-secret"]):
        with pytest.raises(SystemExit) as exc:
            CLI.main(["render", answers, "-o", out] + flag + _render_args(tmp_path))
        assert exc.value.code == 2                          # argparse: 모르는 인자


# --- consent — 동의는 사람에게서만 온다(3차 리허설 C1) --------------------------
#
# 온보딩 **서브 에이전트가 동의를 자기 승인**한 실측 결함을 닫은 자리다. 여기서는 CLI
# 계약만 본다(판정 로직은 tests/test_setup_consent.py).


def test_a_subagent_can_only_return_a_consent_request(tmp_path, monkeypatch, capsys):
    """서브 경로에서 할 수 있는 유일한 동작 — 요청서 반환. 파일은 만들어지지 않는다."""
    monkeypatch.setenv("JAD_SETUP_ACTOR", "subagent")
    assert CLI.main(["consent", "--request", "--json",
                     "--project-dir", str(tmp_path)]) == CLI.EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == "consent_request"
    assert "--relay" in payload["relay_command"]
    assert not (tmp_path / "setup-consent.json").exists()


def test_a_subagent_cannot_relay_consent_to_itself(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("JAD_SETUP_ACTOR", "subagent")
    code = CLI.main(["consent", "--relay", "--granted-by", "사용자",
                     "--statement", "풀 퍼미션으로 돌려도 좋습니다",
                     "--relayed-by", "onboarding-sub",
                     "--project-dir", str(tmp_path)])
    assert code == CLI.EXIT_GATE_FAILED
    assert "서브 에이전트는 동의를 만들 수 없습니다" in capsys.readouterr().err
    assert not (tmp_path / "setup-consent.json").exists()


def test_a_headless_session_cannot_press_the_human_button(tmp_path, capsys):
    """pytest 의 stdin 은 TTY 가 아니다 — 헤드리스 세션이 정확히 이 모양이다."""
    assert CLI.main(["consent", "--project-dir", str(tmp_path)]) == CLI.EXIT_GATE_FAILED
    assert "이 채널에는 사람이 없습니다" in capsys.readouterr().err
    assert not (tmp_path / "setup-consent.json").exists()


def test_the_orchestrator_can_relay_and_show_a_human_consent(tmp_path, capsys):
    assert CLI.main(["consent", "--relay", "--granted-by", "yh.choi@acme.example",
                     "--statement", "풀 퍼미션으로 돌려도 좋습니다. 위험은 이해했습니다.",
                     "--relayed-by", "orchestrator",
                     "--project-dir", str(tmp_path)]) == CLI.EXIT_OK
    capsys.readouterr()
    assert CLI.main(["consent", "--show", "--json",
                     "--project-dir", str(tmp_path)]) == CLI.EXIT_OK
    record = json.loads(capsys.readouterr().out)["record"]
    assert record["channel"] == "orchestrator_relay"
    assert record["granted_by"] == "yh.choi@acme.example"


def test_show_without_a_record_is_a_gate_failure(tmp_path):
    assert CLI.main(["consent", "--show",
                     "--project-dir", str(tmp_path)]) == CLI.EXIT_GATE_FAILED


def test_validate_blocks_an_answer_file_that_consented_to_itself(tmp_path, capsys):
    """C1 실증 — 답변에 true 만 있고 증서가 없으면 **그 자리에서 시끄럽게** 멈춘다."""
    code = CLI.main(["validate", _write_json(tmp_path, "a.json", GOOD_ANSWERS),
                     "--json", "--project-dir", str(tmp_path)])
    assert code == CLI.EXIT_GATE_FAILED
    payload = json.loads(capsys.readouterr().out)
    assert any(f["code"] == "consent_unattested" for f in payload["findings"])


def test_render_copies_the_consent_provenance_into_the_config(tmp_path):
    """동의의 출처는 증서가 정본 — config.yaml 은 그 사본이다."""
    _grant_consent(tmp_path, channel="orchestrator_relay", granted_by="사용자",
                   relayed_by="orchestrator", statement="풀 퍼미션으로 돌려도 좋습니다")
    out = str(tmp_path / "config.yaml")
    assert CLI.main(["render", _write_json(tmp_path, "a.json", GOOD_ANSWERS),
                     "-o", out, "--project-dir", str(tmp_path)]) == CLI.EXIT_OK
    import yaml

    consent = yaml.safe_load(open(out, encoding="utf-8"))["consent"]
    assert consent["full_permissions"] is True
    assert consent["channel"] == "orchestrator_relay"
    assert consent["granted_by"] == "사용자"
    assert consent["relayed_by"] == "orchestrator"
    # ⚠️ 사람의 원문은 설정 파일로 가지 않는다(설정은 컨테이너로 마운트된다).
    assert "돌려도 좋습니다" not in open(out, encoding="utf-8").read()


def test_the_answer_file_does_not_need_to_mention_consent_at_all(tmp_path, capsys):
    """증서가 정본이므로 온보딩 에이전트는 동의를 **적을 이유가 없다**(적어도 무의미)."""
    answers = {k: v for k, v in GOOD_ANSWERS.items() if k != "consent"}
    assert CLI.main(["validate", _write_json(tmp_path, "a.json", answers)]
                    + _render_args(tmp_path)) == CLI.EXIT_OK


def test_a_record_that_denies_consent_beats_an_answer_that_grants_it(tmp_path, capsys):
    """거부 증서는 답변의 true 를 덮어쓰지 않는다 — 그 모순 자체가 오류여야 한다."""
    _grant_consent(tmp_path, full_permissions=False)
    code = CLI.main(["validate", _write_json(tmp_path, "a.json", GOOD_ANSWERS),
                     "--json", "--project-dir", str(tmp_path)])
    assert code == CLI.EXIT_GATE_FAILED
    payload = json.loads(capsys.readouterr().out)
    assert any(f["code"] == "consent_unattested" for f in payload["findings"])
