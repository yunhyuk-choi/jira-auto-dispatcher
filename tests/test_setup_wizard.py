"""설치 마법사(app/setup_wizard.py) 단위테스트 — **대화는 편의, 게이트는 기계다.**

이 테스트가 고정하는 계약:
    - 대화가 무엇을 건너뛰든 :mod:`app.setup_validate` 를 통과하지 못하면 ``config.yaml``
      은 **생성되지 않고** 종료코드는 non-zero 다(동의를 거부한 경우가 그 예다).
    - 필수 항목에 빈 답을 주면 **진행이 막히고 같은 질문이 다시 온다.**
    - 시크릿 **값**은 0600 파일로만 가고 답변 파일·설정·출력 어디에도 남지 않는다.
    - 중단(EOF)해도 여태 모은 답이 파일로 남고, 다시 돌리면 이어서 한다.
    - 자동으로 알아낸 값(dlc-meta URL·커스텀필드 id)은 **묻지 않거나 확인만** 받고,
      자동 선택이라도 사용자가 바꿀 수 있다.

네트워크·도커는 전부 대역 주입이다(CI 는 ubuntu 에서 네트워크 없이 돈다).
"""

from __future__ import annotations

import json
import os
import stat

import pytest

from app import setup as CLI
from app import setup_discover as D
from app import setup_doctor as DOC
from app import setup_wizard as W

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE = os.path.join(REPO_ROOT, "config", "config.example.yaml")

DLC_URL = "https://git.corp.example/acme/dlc-meta.git"


@pytest.fixture(autouse=True)
def _no_ambient_env(monkeypatch):
    """호스트 env 가 답을 대신 채워 테스트가 거짓 통과하지 않게."""
    for name in ("HOST_DEPLOY_DIR", "SECRETS_DIR", "JIRA_WATCHER_EMAIL",
                 "DLC_META_DIR"):
        monkeypatch.delenv(name, raising=False)


# ---------------------------------------------------------------------------
# 대역 — 입력 시퀀스 / 조회 / 진단
# ---------------------------------------------------------------------------


class Responder:
    """프롬프트 문자열로 답을 고르는 입력 대역.

    고정 큐가 아니라 **규칙**으로 답하는 이유: 질문 하나가 늘 때마다 모든 테스트의 큐가
    한 칸씩 밀리면, 테스트가 계약이 아니라 순서를 고정하게 된다. 규칙에 없는 질문은
    빈 문자열(=Enter, 기본값 수락)로 답한다 — 그래서 "기본값만으로 끝까지 가는가"도
    같이 검증된다.
    """

    #: 폭주 방지(테스트가 무한 루프에 빠지면 원인 프롬프트를 들고 죽는다).
    MAX_CALLS = 300

    def __init__(self, rules=(), *, secrets=()):
        self.rules = list(rules)
        self.secret_rules = list(secrets)
        self.prompts: list = []
        self.secret_prompts: list = []
        self.calls = 0

    def _match(self, prompt, rules):
        self.calls += 1
        if self.calls > self.MAX_CALLS:
            raise AssertionError(f"질문이 끝나지 않습니다(마지막: {prompt!r})")
        for needle, answer in rules:
            if needle in prompt:
                return answer() if callable(answer) else answer
        return ""

    def read(self, prompt):
        self.prompts.append(prompt)
        return self._match(prompt, self.rules)

    def read_secret(self, prompt):
        self.secret_prompts.append(prompt)
        return self._match(prompt, self.secret_rules)

    def asked(self, needle) -> int:
        """그 문구를 담은 질문이 몇 번 나왔는가."""
        return sum(1 for p in self.prompts if needle in p)


class Recorder:
    """출력 대역 — 화면에 시크릿이 새지 않는지 보는 데 쓴다."""

    def __init__(self):
        self.lines: list = []

    def __call__(self, text):
        self.lines.append(str(text))

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


def fake_discover(*, statuses=True, transitions=True, custom_fields=True):
    """``app.setup_discover.discover`` 대역 — 실제 응답 모양 그대로."""

    def _discover(cfg, **kwargs):
        sections = [D.Section("account", D.STATUS_OK, "봇 (bot@acme.example)",
                              data={"account_id": "5f00:abcd", "display_name": "봇",
                                    "email": "bot@acme.example", "active": True})]
        if statuses:
            sections.append(D.Section(
                "statuses", D.STATUS_OK, "ACME 의 상태 3종",
                data={"project": "ACME",
                      "statuses": [{"id": "10000", "name": "해야 할 일", "category": "new"},
                                   {"id": "10001", "name": "진행 중", "category": "indeterminate"},
                                   {"id": "10002", "name": "취소됨", "category": "done"}],
                      "configured": {"trigger_statuses": [], "cancel_statuses": []},
                      "unknown": []}))
        if transitions:
            sections.append(D.Section(
                "transitions", D.STATUS_OK, "ACME-1 의 전이 2개",
                data={"issue_key": "ACME-1", "sampled": True,
                      "transitions": [
                          {"id": "21", "name": "진행", "to_name": "진행 중",
                           "to_category": "indeterminate"},
                          {"id": "41", "name": "완료", "to_name": "완료됨",
                           "to_category": "done"}],
                      "done_candidates": [],
                      "done_selected": {"id": "41", "name": "완료",
                                        "reason": "name_match"},
                      "configured": {}}))
        if custom_fields:
            sections.append(D.Section(
                "custom_fields", D.STATUS_OK, "필드 200개 조회",
                data={"logical_keys": {
                    "start_date": {"selected": "customfield_20001",
                                   "reason": "exact",
                                   "candidates": [
                                       {"id": "customfield_20001", "name": "시작날짜",
                                        "custom": True, "type": "date", "tier": "exact"},
                                       {"id": "customfield_20009", "name": "실제 시작일",
                                        "custom": True, "type": "date",
                                        "tier": "partial"}]},
                    "due_date": {"selected": "duedate", "reason": "exact",
                                 "candidates": [{"id": "duedate", "name": "마감일",
                                                 "custom": False, "type": "date",
                                                 "tier": "exact"}]},
                    "actual_start": {"selected": None, "reason": "no_exact_match",
                                     "candidates": [
                                         {"id": "customfield_30001",
                                          "name": "실제 시작일(팀A)", "custom": True,
                                          "type": "date", "tier": "partial"}]},
                    "actual_end": {"selected": None, "reason": "no_exact_match",
                                   "candidates": []},
                }, "field_count": 200, "invalid_configured": []}))
        return D.DiscoveryResult(sections=sections)

    return _discover


def fake_doctor(status=DOC.STATUS_PASS):
    def _run_checks(cfg, **kwargs):
        return [DOC.CheckResult("config", status, "확인함")]

    return _run_checks


def fake_git_clone(tmp_path, url=DLC_URL, name="dlc-meta"):
    """origin 이 ``url`` 인 것처럼 보이는 클론(git 을 실제로 부르지 않는다)."""
    path = tmp_path / name
    (path / ".git").mkdir(parents=True)

    def fake_run(cmd, **kwargs):
        from types import SimpleNamespace

        if cmd[:2] == ["git", "-C"] and cmd[2] == str(path):
            return SimpleNamespace(returncode=0, stdout=url + "\n", stderr="")
        return SimpleNamespace(returncode=128, stdout="", stderr="not a repo")

    return str(path), fake_run


# ---------------------------------------------------------------------------
# 공통 시나리오
# ---------------------------------------------------------------------------


HAPPY_RULES = (
    ("동의", "y"),
    ("jira.base_url", "https://acme.atlassian.net"),
    ("jira.project", "ACME"),
    ("jira.watcher_email", "bot@acme.example"),
    ("착수(트리거) 상태", "1"),
    ("'취소' 상태", "3"),
    ("run.dlc_meta_repo_url", DLC_URL),
)

HAPPY_SECRETS = (
    ("Jira 감시 계정 API 토큰", "jira-token-값"),
    ("forge 토큰", "forge-token-값"),
)


def make_options(tmp_path, **overrides):
    opts = dict(
        answers_path=str(tmp_path / "setup-answers.json"),
        project_dir=str(tmp_path),
        config_path=str(tmp_path / "config.yaml"),
        template=TEMPLATE,
        secrets_dir=str(tmp_path / "secrets"),
        use_discover=True,
        use_doctor=True,
        autofill=False,     # 기본은 git 을 부르지 않는다(개별 테스트가 켠다)
    )
    opts.update(overrides)
    return W.WizardOptions(**opts)


def run(tmp_path, rules=HAPPY_RULES, secrets=HAPPY_SECRETS, *, options=None,
        discover=None, doctor=None):
    responder = Responder(rules, secrets=secrets)
    out = Recorder()
    io = W.WizardIO(reader=responder.read, secret_reader=responder.read_secret,
                    writer=out)
    code = W.run_wizard(
        io, options or make_options(tmp_path),
        discover_fn=discover or fake_discover(),
        doctor_fn=doctor or fake_doctor(),
        now_fn=lambda: "2026-08-26T09:00:00+09:00",
        token_fn=lambda: "0" * 64,
    )
    return code, responder, out


# ---------------------------------------------------------------------------
# 행복 경로 — 대화만으로 config.yaml 까지
# ---------------------------------------------------------------------------


def test_wizard_walks_from_nothing_to_a_loadable_config(tmp_path):
    """빈 디렉토리 → 대화 → 로드 가능한 config.yaml (리허설의 손일이 사라진 지점)."""
    from app import config as C

    code, _responder, out = run(tmp_path)
    assert code == W.EXIT_OK, out.text

    cfg = C.load_config(str(tmp_path / "config.yaml"))
    assert cfg.jira.base_url == "https://acme.atlassian.net"
    assert cfg.jira.project == "ACME"
    assert cfg.consent.full_permissions is True
    # 프로파일 파생값이 실제로 config 에 들어간다(설치자가 계산하지 않는다).
    assert cfg.deploy.docker_host == "tcp://socket-proxy:2375"
    # 리허설에서 사람이 막혔던 자리 — 묻고 채웠다.
    assert cfg.secrets.base_dir == "/run/secrets"


def test_wizard_does_not_create_a_retired_worker_shared_secret(tmp_path):
    """마법사는 **아무것도 인증하지 않는 시크릿**을 만들지 않는다(은퇴한 노브).

    옛 ``WORKER_SHARED_SECRET`` 은 워커→중앙 dispatch HTTP 의 ``X-Worker-Secret``
    값이었다. 그 경로가 프랙탈 seam(중앙 → docker exec 푸시)으로 대체되며 읽는 곳이
    사라졌으므로, 온보딩이 의미 없는 것을 챙기지 않는다.
    """
    code, _responder, out = run(tmp_path)
    assert code == W.EXIT_OK
    assert not (tmp_path / ".env").exists()
    assert "WORKER_SHARED_SECRET" not in out.text
    assert "WORKER_SHARED_SECRET" not in (tmp_path / "config.yaml").read_text(
        encoding="utf-8")
def test_status_names_come_from_the_instance_not_from_typing(tmp_path):
    """상태는 **조회 목록에서 고른다** — id 와 이름이 함께 박힌다."""
    code, _responder, _out = run(tmp_path)
    assert code == W.EXIT_OK
    answers = json.loads((tmp_path / "setup-answers.json").read_text(encoding="utf-8"))
    assert answers["jira"]["trigger_statuses"] == [{"id": "10000", "name": "해야 할 일"}]
    assert answers["jira"]["cancel_statuses"] == [{"id": "10002", "name": "취소됨"}]
    assert answers["jira"]["done_transition_names"] == [{"id": "41", "name": "완료"}]


def test_answers_file_is_the_same_shape_the_manual_path_accepts(tmp_path):
    """마법사가 남긴 답변 파일을 수동 CLI 가 그대로 받는다(대화형은 유일 경로가 아니다)."""
    code, _responder, _out = run(tmp_path)
    assert code == W.EXIT_OK
    assert CLI.main(["validate", str(tmp_path / "setup-answers.json"),
                     "--no-autofill"]) == CLI.EXIT_OK


# ---------------------------------------------------------------------------
# 게이트 — 대화가 건너뛰려 해도 막힌다
# ---------------------------------------------------------------------------


def test_declining_consent_blocks_and_writes_no_config(tmp_path):
    """동의를 거부하면 **검증기가** 막는다 — 마법사가 판정하는 것이 아니다."""
    rules = tuple(r for r in HAPPY_RULES if r[0] != "동의") + (("동의", "n"),)
    code, _responder, out = run(tmp_path, rules=rules)
    assert code == W.EXIT_GATE_FAILED
    assert not (tmp_path / "config.yaml").exists()
    assert "consent.full_permissions" in out.text
    # 여태 모은 답은 남는다(중단·재개).
    assert (tmp_path / "setup-answers.json").exists()


def test_required_answer_cannot_be_skipped(tmp_path):
    """필수 항목에 Enter 만 치면 **같은 질문이 다시 온다**(진행이 막힌다)."""
    seen = {"n": 0}

    def project_answer():
        seen["n"] += 1
        return "" if seen["n"] < 3 else "ACME"

    rules = tuple(r for r in HAPPY_RULES if r[0] != "jira.project") + \
        (("jira.project", project_answer),)
    code, responder, out = run(tmp_path, rules=rules)
    assert code == W.EXIT_OK
    assert responder.asked("jira.project") == 3      # 두 번 거절당하고 세 번째에 통과
    assert "필수 항목입니다" in out.text


def test_validation_failure_stops_before_render(tmp_path, monkeypatch):
    """검증이 실패하면 config.yaml 을 만들지 않는다(게이트를 우회하는 경로가 없다)."""
    calls = {"render": 0}
    real_render = W.setup_render.render_config

    def counting_render(*a, **kw):
        calls["render"] += 1
        return real_render(*a, **kw)

    monkeypatch.setattr(W.setup_render, "render_config", counting_render)
    # dlc-meta URL 을 답하지 않으면 required 가 막는다(자동 채움도 꺼져 있다).
    rules = tuple(r for r in HAPPY_RULES if r[0] != "run.dlc_meta_repo_url") + \
        (("run.dlc_meta_repo_url", "<your-group>"),      # 자리표시자 = 검증 오류
         ("지금 고칠까요", "n"))
    code, _responder, out = run(tmp_path, rules=rules)
    assert code == W.EXIT_GATE_FAILED
    assert calls["render"] == 0
    assert not (tmp_path / "config.yaml").exists()
    assert "run.dlc_meta_repo_url" in out.text


def test_doctor_failure_is_reported_as_non_zero(tmp_path):
    """진단이 실패하면 config.yaml 은 남기되 종료코드로 알린다."""
    code, _responder, out = run(tmp_path, doctor=fake_doctor(DOC.STATUS_FAIL))
    assert code == W.EXIT_GATE_FAILED
    assert (tmp_path / "config.yaml").exists()
    assert "python -m app.setup doctor" in out.text


def test_exit_codes_match_the_cli_contract():
    """마법사와 CLI 의 종료코드 계약이 갈라지지 않는다."""
    assert (W.EXIT_OK, W.EXIT_GATE_FAILED, W.EXIT_USAGE) == \
        (CLI.EXIT_OK, CLI.EXIT_GATE_FAILED, CLI.EXIT_USAGE)


# ---------------------------------------------------------------------------
# 시크릿 — 값은 파일로만
# ---------------------------------------------------------------------------


def test_secret_values_go_to_0600_files_and_nowhere_else(tmp_path):
    code, _responder, out = run(tmp_path)
    assert code == W.EXIT_OK

    jira_token = tmp_path / "secrets" / "service" / "jira-token"
    forge_token = tmp_path / "secrets" / "service" / "forge-token"
    assert jira_token.read_text(encoding="utf-8") == "jira-token-값"
    assert forge_token.read_text(encoding="utf-8") == "forge-token-값"

    # 값이 화면·답변 파일·설정 어디에도 없다.
    answers = (tmp_path / "setup-answers.json").read_text(encoding="utf-8")
    config = (tmp_path / "config.yaml").read_text(encoding="utf-8")
    for value in ("jira-token-값", "forge-token-값"):
        assert value not in out.text
        assert value not in answers
        assert value not in config
    # 설정에는 **참조만** 있다.
    assert "service/jira-token" in config

    if os.name != "nt":   # 윈도우는 chmod 가 사실상 무시된다(INSTALL §2.3)
        assert stat.S_IMODE(os.stat(jira_token).st_mode) == 0o600


def test_webhook_token_is_generated_not_asked(tmp_path):
    """사람이 정할 이유가 없는 값은 묻지 않는다(고엔트로피 웹훅 토큰)."""
    code, responder, out = run(tmp_path)
    assert code == W.EXIT_OK
    assert (tmp_path / "secrets" / "service" / "jira-webhook").read_text(
        encoding="utf-8") == "0" * 64
    assert not any("웹훅 수신 토큰 값" in p for p in responder.secret_prompts)
    assert "0" * 64 not in out.text          # 생성한 값도 화면에 싣지 않는다


def test_existing_secret_file_is_not_asked_again(tmp_path):
    """재개 시 같은 토큰을 두 번 붙여넣게 하지 않는다."""
    path = tmp_path / "secrets" / "service" / "jira-token"
    path.parent.mkdir(parents=True)
    path.write_text("이미-있는-토큰", encoding="utf-8")

    code, responder, _out = run(tmp_path)
    assert code == W.EXIT_OK
    assert not any("Jira 감시 계정" in p for p in responder.secret_prompts)
    assert path.read_text(encoding="utf-8") == "이미-있는-토큰"


def test_skipping_a_secret_value_leaves_a_note_not_a_silent_hole(tmp_path):
    """값을 지금 못 구해도 진행할 수 있고, 남은 일이 안내에 남는다."""
    code, _responder, out = run(tmp_path, secrets=(("forge 토큰", "forge-값"),))
    assert code == W.EXIT_OK          # 참조는 채워졌으므로 검증은 통과한다
    assert "건너뜀" in out.text
    assert "남은 일" in out.text
    assert not (tmp_path / "secrets" / "service" / "jira-token").exists()


# ---------------------------------------------------------------------------
# 참조 자리에 값이 오면 — 리허설에서 실제로 난 사고
# ---------------------------------------------------------------------------


#: 유출되면 안 되는 것처럼 다루는 가짜 토큰(알려진 GitLab PAT 접두사 —
#: :func:`app.setup_validate.looks_like_secret_value` 가 잡는 형태).
PASTED_TOKEN = "glpat-DO-NOT-LEAK-THIS-INTO-A-FILENAME"

#: 참조 자리를 묻는 프롬프트 조각(키 이름이 그대로 프롬프트에 들어간다).
REF_PROMPTS = (
    "jira.watcher_token_file",
    "forge.token_ref",
    "webhook.secret_ref",
    "notifier.webhook_ref",
)

#: 되묻기를 켜려면 알림 채널이 ``none`` 이 아니어야 한다(그래야 webhook_ref 를 묻는다).
NOTIFIER_ON = ("notifier.provider", "2")   # 1)none 2)google_chat …


def answer_then(bad, good):
    """첫 호출만 ``bad``, 이후로는 ``good`` 을 답하는 대역(되묻기 검증용)."""
    state = {"n": 0}

    def _answer():
        state["n"] += 1
        return bad if state["n"] == 1 else good

    return _answer


@pytest.mark.parametrize("ref_key", REF_PROMPTS)
def test_a_token_pasted_into_the_reference_prompt_is_caught_and_reasked(tmp_path, ref_key):
    """참조 자리에 토큰을 붙여넣으면 **파일 이름이 되기 전에** 붙잡힌다.

    리허설에서 난 사고 그대로다 — 참조 자리에 값을 넣자 ``secrets/<값>`` 파일이 생기고
    ``config.yaml`` 의 ``*_ref`` 에도 그 값이 적혔다(파일명은 ls·로그·오류 메시지에
    실리므로 그 자체가 유출 표면이다). 참조를 묻는 자리는 **전부** 막혀야 한다.
    """
    good_ref = "service/제대로-된-참조"
    rules = HAPPY_RULES + (NOTIFIER_ON,
                           (ref_key, answer_then(PASTED_TOKEN, good_ref)))
    code, responder, out = run(tmp_path, rules=rules,
                               secrets=HAPPY_SECRETS + (("값", "아무-값"),))

    assert code == W.EXIT_OK
    # 그 프롬프트가 실제로 나왔고, **다시** 물었다(테스트가 헛돌지 않는다).
    assert responder.asked(ref_key) >= 2, ref_key
    assert "파일 경로" in out.text
    # 토큰이 파일 이름이 되지 않았다.
    assert not (tmp_path / "secrets" / PASTED_TOKEN).exists()
    assert not any(PASTED_TOKEN in p for p in _all_paths(tmp_path / "secrets"))
    # 설정·답변 파일 어디에도 없다.
    for path in ("config.yaml", "setup-answers.json"):
        assert PASTED_TOKEN not in (tmp_path / path).read_text(encoding="utf-8")
    # 되물은 뒤 받은 **참조**는 정상적으로 쓰인다.
    assert good_ref in (tmp_path / "setup-answers.json").read_text(encoding="utf-8")


@pytest.mark.parametrize("ref_key", REF_PROMPTS)
def test_the_warning_never_echoes_what_was_typed(tmp_path, ref_key):
    """경고문이 입력값을 되비추지 않는다 — 진짜 토큰이면 그 에코가 곧 유출이다."""
    rules = HAPPY_RULES + (NOTIFIER_ON,
                           (ref_key, answer_then(PASTED_TOKEN, "service/ok")))
    _code, responder, out = run(tmp_path, rules=rules,
                                secrets=HAPPY_SECRETS + (("값", "아무-값"),))

    assert responder.asked(ref_key) >= 2, ref_key
    assert "파일 경로" in out.text                # 무엇이 문제인지 말은 한다
    assert PASTED_TOKEN not in out.text          # 값은 말하지 않는다
    assert PASTED_TOKEN[:12] not in out.text     # 앞자락도 흘리지 않는다
    assert "길이" in out.text                     # 형태(길이)만 알려준다


def test_insisting_ends_the_reasking_and_hands_the_call_to_the_gate(tmp_path):
    """휴리스틱은 오탐이 난다 — **마법사는** 사용자를 여기서 막지 않는다.

    다만 되묻기를 그만두는 것이 검사를 끄는 것은 아니다. 판정 권한은 게이트에 있고
    (``validate`` 가 같은 함수로 다시 본다), 이 모듈은 그 게이트를 우회하는 경로를 만들지
    않는다 — 그래서 "통과할 수 있는 탈출구"인 척하지 않고 그 사실을 그대로 알린다.
    """
    odd_ref = "a" * 40          # 값처럼 보이지만 사람이 고집할 수 있는 참조
    rules = HAPPY_RULES + (("forge.token_ref", odd_ref),
                           ("그래도 방금 입력을 참조 경로로 쓸까요", "y"))
    code, responder, out = run(tmp_path, rules=rules)

    # 마법사는 되묻기를 멈추고 그대로 진행했다 — 값까지 받아 그 참조 파일에 저장한다.
    assert (tmp_path / "secrets" / odd_ref).read_text(encoding="utf-8") == "forge-token-값"
    assert any("forge 토큰" in p for p in responder.secret_prompts)
    # 밀어붙인 대가(파일명·로그 노출)와 게이트가 다시 본다는 사실을 둘 다 알린다.
    assert "ls·로그" in out.text
    assert "게이트를 우회하지 않습니다" in out.text
    # 그리고 막은 것은 마법사가 아니라 **게이트**다(config.yaml 은 생기지 않는다).
    assert code == W.EXIT_GATE_FAILED
    assert not (tmp_path / "config.yaml").exists()


def test_declining_the_flagged_reference_does_not_leave_it_in_the_answers(tmp_path):
    """되묻기로 버린 입력은 답변 파일에 **한 번도** 얹히지 않는다(중단해도 남지 않게)."""
    rules = HAPPY_RULES + (("forge.token_ref",
                            answer_then(PASTED_TOKEN, "service/forge-token")),)
    seen: list = []

    real_write = W.write_answers

    def spy(path, flat):
        seen.append(dict(flat))
        real_write(path, flat)

    W.write_answers = spy
    try:
        code, _responder, _out = run(tmp_path, rules=rules)
    finally:
        W.write_answers = real_write

    assert code == W.EXIT_OK
    assert not any(PASTED_TOKEN in str(snapshot.values()) for snapshot in seen)


def test_the_wizard_and_the_validator_judge_by_the_same_rule():
    """판정은 마법사가 새로 만들지 않는다 — 검증기의 것을 그대로 쓴다.

    기준이 두 벌이 되면 한쪽만 갱신돼 어긋난다(대화에서 통과한 값이 검증에서 막히는 식).
    그리고 스키마 **예시**가 자기 자신의 경고에 걸리면 안 된다.
    """
    from app import setup_schema as SC
    from app import setup_validate as V

    assert V.looks_like_secret_value(PASTED_TOKEN)   # 같은 함수가 잡는다
    for key in REF_PROMPTS:
        example = SC.get_field(key).example
        if example:
            assert V.looks_like_secret_value(str(example)) == "", key


def _all_paths(root) -> list:
    """``root`` 아래 모든 경로 문자열(파일명 유출 검사용)."""
    out: list = []
    for dirpath, dirnames, filenames in os.walk(str(root)):
        out.extend(os.path.join(dirpath, n) for n in list(dirnames) + list(filenames))
    return out


# ---------------------------------------------------------------------------
# 묻지 않는 값 · 자동 선택
# ---------------------------------------------------------------------------


def test_dlc_meta_url_is_not_asked_when_a_clone_is_found(tmp_path, monkeypatch):
    """dlc-meta URL 은 **묻지 않는 값**이다 — 클론의 origin 에서 읽는다."""
    from app import setup_autofill as A

    clone, fake_run = fake_git_clone(tmp_path)
    monkeypatch.setattr(A.subprocess, "run", fake_run)

    options = make_options(tmp_path, autofill=True, dlc_meta=clone)
    rules = tuple(r for r in HAPPY_RULES if r[0] != "run.dlc_meta_repo_url")
    code, responder, out = run(tmp_path, rules=rules, options=options)
    assert code == W.EXIT_OK
    assert responder.asked("run.dlc_meta_repo_url") == 0
    assert DLC_URL in out.text
    assert DLC_URL in (tmp_path / "config.yaml").read_text(encoding="utf-8")


def test_auto_selected_custom_field_can_be_overridden(tmp_path):
    """자동 선택은 **확정이 아니다** — 아니라고 하면 후보에서 다시 고를 수 있다."""
    rules = HAPPY_RULES + (
        # ⚠️ 규칙은 **앞에서부터** 매칭된다 — 확인 질문을 먼저 잡고, 그다음 선택 질문.
        ("customfield_20001 로 채웠습니다", "n"),   # "맞습니까" 에 아니오
        ("start_date", "2"),                        # 후보 목록에서 두 번째(실제 시작일)
    )
    code, _responder, _out = run(tmp_path, rules=rules)
    assert code == W.EXIT_OK
    answers = json.loads((tmp_path / "setup-answers.json").read_text(encoding="utf-8"))
    assert answers["jira"]["custom_fields"]["start_date"] == "customfield_20009"
    # 확정된 것은 그대로 들어간다.
    assert answers["jira"]["custom_fields"]["due_date"] == "duedate"


def test_unconfirmed_custom_field_falls_back_to_the_declared_default(tmp_path):
    """확정하지 못한 항목은 추측하지 않고 기본값 유지를 **기본 선택**으로 제시한다."""
    code, _responder, _out = run(tmp_path)
    assert code == W.EXIT_OK
    answers = json.loads((tmp_path / "setup-answers.json").read_text(encoding="utf-8"))
    assert answers["jira"]["custom_fields"]["actual_start"] == "customfield_10187"


# ---------------------------------------------------------------------------
# 중단 · 재개
# ---------------------------------------------------------------------------


def test_interrupt_saves_answers_and_resume_reuses_them(tmp_path):
    """중단(EOF)해도 답이 남고, 다시 돌리면 그 값이 기본값으로 제시된다."""

    class _Stop:
        """base_url 까지만 답하고 그다음 질문에서 EOF(사용자가 Ctrl-D)."""

        def __init__(self):
            self.n = 0

        def read(self, prompt):
            self.n += 1
            if "동의" in prompt:
                return "y"
            if "jira.base_url" in prompt:
                return "https://acme.atlassian.net"
            raise EOFError

        def read_secret(self, prompt):
            raise EOFError

    stop = _Stop()
    out = Recorder()
    options = make_options(tmp_path)
    code = W.run_wizard(W.WizardIO(reader=stop.read, secret_reader=stop.read_secret,
                                   writer=out),
                        options, discover_fn=fake_discover(), doctor_fn=fake_doctor(),
                        now_fn=lambda: "2026-08-26T09:00:00+09:00")
    assert code == W.EXIT_GATE_FAILED
    assert "중단했습니다" in out.text
    saved = json.loads((tmp_path / "setup-answers.json").read_text(encoding="utf-8"))
    assert saved["jira"]["base_url"] == "https://acme.atlassian.net"
    assert saved["consent"]["full_permissions"] is True

    # 이어서 — 이미 답한 항목은 기본값으로 제시되므로 다시 답하지 않아도 된다.
    rules = tuple(r for r in HAPPY_RULES if r[0] != "jira.base_url")
    code2, responder2, _out2 = run(tmp_path, rules=rules, options=make_options(tmp_path))
    assert code2 == W.EXIT_OK
    assert any("https://acme.atlassian.net" in p for p in responder2.prompts)


def test_resume_does_not_reask_answered_questions_needlessly(tmp_path):
    """재개 시 질문 수가 줄어든다(기본값 Enter 로 넘어간다)."""
    code, first, _out = run(tmp_path)
    assert code == W.EXIT_OK
    os.remove(tmp_path / "config.yaml")
    code2, second, _out2 = run(tmp_path, rules=(), secrets=())   # 전부 Enter
    assert code2 == W.EXIT_OK
    assert second.calls <= first.calls


# ---------------------------------------------------------------------------
# 조회 실패·오프라인
# ---------------------------------------------------------------------------


def test_discovery_failure_falls_back_to_typing(tmp_path):
    """조회가 죽어도 설치가 멈추지 않는다 — 직접 입력으로 내려간다."""

    def exploding(cfg, **kwargs):
        raise RuntimeError("Jira 에 못 붙음")

    rules = HAPPY_RULES + (("jira.trigger_statuses", "해야 할 일"),)
    code, _responder, out = run(tmp_path, rules=rules, discover=exploding)
    assert code == W.EXIT_OK
    assert "조회하지 못했습니다" in out.text
    answers = json.loads((tmp_path / "setup-answers.json").read_text(encoding="utf-8"))
    assert answers["jira"]["trigger_statuses"] == ["해야 할 일"]


def test_offline_mode_skips_network_steps(tmp_path):
    """``--no-discover --no-doctor`` 는 네트워크를 한 번도 건드리지 않는다."""

    def forbidden(*a, **kw):
        raise AssertionError("네트워크를 부르면 안 된다")

    options = make_options(tmp_path, use_discover=False, use_doctor=False)
    rules = HAPPY_RULES + (("jira.trigger_statuses", "해야 할 일"),)
    code, _responder, out = run(tmp_path, rules=rules, options=options,
                                discover=forbidden, doctor=forbidden)
    assert code == W.EXIT_OK
    assert (tmp_path / "config.yaml").exists()
    assert "--no-doctor" in out.text


# ---------------------------------------------------------------------------
# CLI 배선
# ---------------------------------------------------------------------------


def test_cli_exposes_wizard_and_wires_the_options(tmp_path, monkeypatch):
    """``python -m app.setup wizard`` 가 옵션을 그대로 넘긴다(껍데기 계약)."""
    captured = {}

    def fake_run_wizard(io, options, **kwargs):
        captured["options"] = options
        return W.EXIT_OK

    monkeypatch.setattr(W, "run_wizard", fake_run_wizard)
    code = CLI.main(["wizard", "--project-dir", str(tmp_path),
                     "-o", str(tmp_path / "c.yaml"), "--no-discover", "--no-doctor"])
    assert code == 0
    options = captured["options"]
    assert options.project_dir == str(tmp_path)
    assert options.config_path == str(tmp_path / "c.yaml")
    assert options.use_discover is False and options.use_doctor is False
    assert options.answers_path == os.path.join(str(tmp_path), W.DEFAULT_ANSWERS_PATH)
    assert options.secrets_root == os.path.join(str(tmp_path), "secrets")


def test_wizard_is_listed_first_in_help(capsys):
    """받는 사람이 `--help` 첫 화면에서 시작점을 본다(발견 가능성)."""
    with pytest.raises(SystemExit):
        CLI.main(["--help"])
    text = capsys.readouterr().out
    assert "wizard" in text
    assert text.index("wizard") < text.index("discover")
