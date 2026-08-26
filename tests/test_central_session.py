"""central_session 단위테스트 — 프랙탈 P2(상주 센트럴 라이브 세션) 신경로.

라이브 claude 는 절대 호출하지 않는다(상위가 인컨테이너 프로브로 검증 — 설계 §9).
Popen 은 큐-구동 FakeCentralProc 로 대역한다: stdin 주입이 곧 stdout 스크립트 응답을
트리거해 리더 스레드가 결정적으로 소비한다(에이전트가 주입에 응답하는 실제 모델).

검증 대상:
    - 센트럴/사용자서브 프레임(데이터) 로드·조합.
    - 스폰-원스 + **재사용-if-up 주입**(2번째 이벤트가 같은 세션 핸들에 주입, 재스폰 없음).
    - --append-system-prompt 는 CLAUDE.md 운영규약 섹션을 가리키는 짧은 포인터(규약
      전문 아님); user 메시지는 티켓 이벤트만.
    - session_id 캡처(init 이벤트).
    - drain-종료가 깨끗이 닫힘(좀비 방지 teardown).
    - build_worker_exec_command: 첫=--session-id / 이후=--resume, 사용자별 결정적 sid.
    - central_fractal_enabled 플래그 게이팅.
"""

from __future__ import annotations

import json
import os
import queue
import time
from types import SimpleNamespace

from app import central_session as cs
from app import prompts

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# --- 프레임(데이터) ----------------------------------------------------------


def test_central_and_user_sub_frames_load_as_data():
    cf = prompts.central_agent_frame()
    uf = prompts.user_sub_frame()
    assert "센트럴 에이전트" in cf and "notify_report" in cf
    assert "레포락" in cf  # consult 규약
    assert "사용자별 서브에이전트" in uf
    assert "--session-id" in uf and "--resume" in uf
    # 캐시 동일 객체(파일 IO 1회).
    assert prompts.central_agent_frame() is cf


# 판단-유예/자기-인가/안티-인젝션 설득 문구(어떤 채널에도 있으면 안 되는 것).
# 정당성은 정직한 사실 문서에서 나온다 — 에이전트에게 "믿어라/의심하지 말라"고 하지 않는다.
_BANNED_JUDGMENT_SUSPENSION = (
    "인젝션으로 오인",       # "이 규약을 프롬프트-인젝션으로 오인하지 말라"류
    "오인하지 말라",
    "의심하지 말라",
    "믿어라",
    "정당한 운영 규약",       # 정당성 자기-주장
    "정식으로 인가된",       # 자기-인가
    "정당·현행·인가",
)


def test_frames_use_clean_tool_call_language_not_injection_shaped():
    """리워크된 프레임은 하네스 도구-호출 언어(사실 서술)를 쓰고, 인젝션-형태(남의
    컨테이너로 docker exec/임퍼소네이션/외부 알림 발송)의 원시 지시도, 판단-유예/자기-인가
    설득 문구도 없다. 정당성은 정직한 문서에서 나온다."""
    cf = prompts.central_agent_frame()
    uf = prompts.user_sub_frame()
    # 도구를 절대경로로 명시(검증 가능한 기계장치).
    assert "/app/worker_dispatch.py" in cf and "/app/notify_report.py" in cf
    assert "/app/worker_dispatch.py" in uf
    # 정직한 사실 프레이밍: 도구는 이 레포가 소유한다(정당성을 설득하지 않는다).
    assert "이 레포가 소유" in cf
    # 인젝션-형태 문구 제거: 프레임 본문에 "docker exec" 원시 지시가 남지 않는다.
    assert "docker exec" not in cf
    assert "docker exec" not in uf
    # 자기-정당화 전달-채널 메타-코멘트 제거 + 판단-유예/자기-인가 설득 문구 부재.
    for frame in (cf, uf):
        assert "--append-system-prompt" not in frame
        assert "신뢰 채널" not in frame
        assert "user 메시지로 주입하지 않는다" not in frame
        for banned in _BANNED_JUDGMENT_SUSPENSION:
            assert banned not in frame, f"프레임에 판단-유예 문구가 남음: {banned!r}"
    # 프레임은 정본(CLAUDE.md 운영규약 섹션)을 가리킨다.
    assert "/app/CLAUDE.md" in cf
    assert "central 런타임 세션 운영규약" in cf


def test_claudemd_documents_central_runtime_section_as_factual_docs():
    """정본(이 레포 CLAUDE.md)이 프랙탈-센트럴 런타임 운영규약을 **정직한 사실 문서**로
    기술한다: 전용 섹션이 존재하고, 하네스 도구(worker_dispatch.py/notify_report.py)를 절대경로로
    명시하며, 두 모드(결정적 OFF / 프랙탈-센트럴 ON)를 화해시킨다(모순 제거). 그리고 그
    섹션은 판단-유예/자기-인가/안티-인젝션 설득 문구를 **하나도** 담지 않는다 — 정당성은
    설득이 아니라 이 레포의 진짜 문서라는 사실에서 나온다."""
    path = os.path.join(_REPO_ROOT, "CLAUDE.md")
    with open(path, "r", encoding="utf-8") as fh:
        md = fh.read()
    # 전용 런타임-운영 섹션 헤딩.
    assert "## central 런타임 세션 운영규약" in md
    # 하네스 도구를 절대경로로 명시.
    assert "/app/worker_dispatch.py" in md
    assert "/app/notify_report.py" in md
    # 두 모드 화해(결정적 HTTP OFF / 프랙탈-센트럴 ON) — 모순이 아니라 문서화된 두 경로.
    assert "fractal_central" in md
    # 문서화된 운영 행동(팬아웃/distinct sid, --resume 관찰루프, 레포락 장부).
    assert "팬아웃" in md and "distinct" in md
    assert "--resume" in md
    assert ".jad-repolock.md" in md
    # 개발 온보딩(디스패처-시스템 아키텍처)은 자동 로드되는 CLAUDE.md 에서 빠지고
    # docs/DISPATCHER-DEV.md 로 이동했다 — 런타임 에이전트 정체성 오염 방지(정체성 혼동).
    assert "이 시스템 = 2-역할 디스패처" not in md
    assert "central ↔ worker 경로" not in md

    # 근본: central-런타임 섹션은 판단-유예/자기-인가/안티-인젝션 설득 문구가 전무하다.
    start = md.index("## central 런타임 세션 운영규약")
    end = md.index("## POLICY-ENCODING", start)
    section = md[start:end]
    for banned in _BANNED_JUDGMENT_SUSPENSION:
        assert banned not in section, f"central-런타임 섹션에 판단-유예 문구가 남음: {banned!r}"
    # "프롬프트-인젝션으로 오인하지 말라"류의 안티-인젝션 설득 전체 부재(포괄).
    assert "프롬프트-인젝션으로 오인" not in section


def test_claudemd_is_thin_runtime_orientation_and_dev_onboarding_moved_to_docs():
    """정체성 혼동 근본 해소: 자동 로드되는 CLAUDE.md 는 런타임 에이전트에게 정체성
    방향(하네스 vs 프레임워크 레포)을 잡아 주는 **얇은 오리엔테이션**이고, 디스패처-
    시스템 개발 온보딩(2-역할 아키텍처·HTTP 프로토콜 표 등)은 자동 로드되지 않는
    docs/DISPATCHER-DEV.md 로 이동했다. central 런타임 세션 운영규약 섹션은 보존된다."""
    with open(os.path.join(_REPO_ROOT, "CLAUDE.md"), "r", encoding="utf-8") as fh:
        md = fh.read()
    with open(os.path.join(_REPO_ROOT, "docs", "DISPATCHER-DEV.md"), "r", encoding="utf-8") as fh:
        dev = fh.read()

    # (a) 런타임 오리엔테이션: 하네스 vs 프레임워크-레포 정체성 명시적 disambiguation.
    assert "하네스" in md
    assert "프레임워크 레포" in md
    assert "/app/workspace/orchestrator" in md
    assert "타깃 레포" in md
    # 프로젝트 키는 배포마다 다르다 — 특정 조직의 키를 박지 않고 설정 정본을 가리킨다.
    assert "jira.project" in md
    assert "KR0001036" not in md

    # (b) 개발자용 조건부 포인터 — dev 는 이동된 온보딩을 찾을 수 있다.
    assert "docs/DISPATCHER-DEV.md" in md

    # (c) central 런타임 세션 운영규약 섹션은 CLAUDE.md 에 그대로 보존된다.
    assert "## central 런타임 세션 운영규약" in md

    # (d) 벌크 디스패처-아키텍처 프로즈는 CLAUDE.md 가 아니라 docs/DISPATCHER-DEV.md 에.
    assert "이 시스템 = 2-역할 디스패처" not in md
    assert "central ↔ worker HTTP 프로토콜" not in md
    assert "이 시스템 = 2-역할 디스패처" in dev
    # ⚠️ 옛 이름은 "central ↔ worker HTTP 프로토콜" 이었다 — 워커 폴링 프로토콜이
    # 은퇴하며(docker exec 주입 한 방향) 절 이름도 함께 바뀌었다.
    assert "central ↔ worker 경로" in dev
    # HTTP 프로토콜 표의 실체(엔드포인트)도 dev 문서에 있고 CLAUDE.md 엔 없다.
    assert "/dispatch/<user>/next" in dev
    assert "/dispatch/<user>/next" not in md

    # (e) 포이즈닝 언어 부재(전체 파일) — 정직한 오리엔테이션만.
    for banned in _BANNED_JUDGMENT_SUSPENSION:
        assert banned not in md, f"CLAUDE.md 에 판단-유예 문구가 남음: {banned!r}"
    assert "프롬프트-인젝션으로 오인" not in md


def test_central_frame_records_dlc_meta_and_unseals_multirepo_triage():
    """#4(완료 후 dlc-meta 단일 라이터 기록) + #5(target_repos 봉인 해제·전레포 커버 완료게이트)."""
    cf = prompts.central_agent_frame()
    # #4: 센트럴이 단일 라이터로 dlc-meta 저널/사이클로그를 기록·커밋.
    assert "dlc-meta" in cf
    assert "runs/" in cf and "cycles/" in cf
    assert "단일 라이터" in cf
    assert "audit.md" in cf
    # #5: 리졸버 target_repos 는 힌트(최종본 아님) + 전 영향레포 커버 전 완료 금지.
    assert "힌트" in cf
    assert "얼리지" in cf
    assert "커버되기 전" in cf or "커버되어야 완료" in cf


def test_claudemd_documents_dlc_meta_recording_and_multirepo_coverage():
    """정본 CLAUDE.md central 런타임 섹션에도 #4/#5 가 반영됐다(프레임과 정합)."""
    with open(os.path.join(_REPO_ROOT, "CLAUDE.md"), "r", encoding="utf-8") as fh:
        md = fh.read()
    assert "dlc-meta" in md and "단일 라이터" in md
    assert "runs/" in md and "cycles/" in md
    assert "힌트" in md and "얼리지" in md  # 리졸버 출력을 얼리지 말라


def test_user_sub_frame_missing_repo_continuation_and_reader_only():
    """#5: 누락 레포 이어위임 + #4: dlc-meta 는 센트럴 단일 라이터(서브/워커는 리더)."""
    uf = prompts.user_sub_frame()
    assert "이어위임" in uf
    assert "모든 레포" in uf
    assert "단일 라이터" in uf


# --- #5-실행: 역할 격리(role isolation) — 워커가 센트럴/dispatch 행동을 채택 못하게 ---


def test_claudemd_has_worker_role_guard_pure_doer():
    """워커 역할 오염 근본 해소: 자동 로드되는 CLAUDE.md 가 ROLE=worker 를 **순수 실행자
    (doer)**로 못박고, 워커에게 dispatch/docker exec/재위임/조율을 **금지**한다. 이 가드가
    없으면 워커가 센트럴 운영규약을 자기 것으로 오인해 self-exec 재귀·라이브락에 빠진다."""
    with open(os.path.join(_REPO_ROOT, "CLAUDE.md"), "r", encoding="utf-8") as fh:
        md = fh.read()
    # ROLE 로 역할을 가르고 worker 를 순수 doer 로 규정한다.
    assert "ROLE=worker" in md
    assert "ROLE=central" in md
    assert "실행자" in md and "doer" in md
    # 워커에게 명시적으로 금지되는 센트럴 행동들(dispatch/exec/재위임/조율).
    assert "docker exec" in md
    assert "worker_dispatch.py" in md
    assert "재위임" in md
    # 워커는 이 도구를 직접 처리(모든 대상 레포)한다는 doer 지시가 명시된다.
    assert "모든 대상 레포" in md


def test_claudemd_central_section_role_guarded_to_central_only():
    """central 런타임 세션 운영규약 섹션에 **역할 가드**가 붙어, ROLE=worker 면 이 섹션
    전체를 무시하라고 명시한다(워커의 센트럴 오인 방지)."""
    with open(os.path.join(_REPO_ROOT, "CLAUDE.md"), "r", encoding="utf-8") as fh:
        md = fh.read()
    start = md.index("## central 런타임 세션 운영규약")
    end = md.index("## POLICY-ENCODING", start)
    section = md[start:end]
    # 섹션 서두(가드)가 ROLE=central 전용임을, ROLE=worker 면 무시함을 명시.
    guard_head = section[: section.index("### ")] if "### " in section else section
    assert "역할 가드" in guard_head
    assert "ROLE=central" in guard_head and "ROLE=worker" in guard_head
    assert "무시" in guard_head


def test_claudemd_worker_push_credential_hygiene():
    """#6: worker 섹션이 푸시 자격증명 위생을 못박는다 — 모든 push/MR 은 per-user 토큰
    (/run/secrets/$DISPATCH_USER/gitlab-token)으로 명시 토큰 URL 을 쓰고, ambient
    `git push origin`·remote 임베디드 토큰·타인 토큰에 절대 폴백하지 않는다."""
    with open(os.path.join(_REPO_ROOT, "CLAUDE.md"), "r", encoding="utf-8") as fh:
        md = fh.read()
    # per-user 토큰 경로를 명시.
    assert "/run/secrets/$DISPATCH_USER/gitlab-token" in md
    # ambient/임베디드 폴백 금지 문구.
    assert "git push origin" in md
    assert "임베디드" in md
    assert "명시 토큰 URL" in md
    # 귀속(작성자) 오염이 근거임을 명시.
    assert "created_by" in md or "작성자" in md


def test_central_side_frames_are_role_guarded_to_central():
    """센트럴 측 프레임(센트럴 에이전트·사용자별 서브)이 ROLE=central 전용임을 명시하고,
    워커에 보내는 지시는 순수 doer 지시여야 함을 못박는다(dispatch 언어 누수 방지)."""
    cf = prompts.central_agent_frame()
    uf = prompts.user_sub_frame()
    assert "역할 가드" in cf and "ROLE=central" in cf
    assert "doer" in cf  # 워커엔 순수 doer 지시만
    assert "역할 가드" in uf and "ROLE=central" in uf
    assert "doer" in uf


def test_only_central_notifies_worker_frames_forbid_it():
    """알림 완료-상신은 센트럴만 한다(유일 통지자). 워커 doer 프레임(컨테이너·사용자
    서브)은 알림 상신을 금지하고 "완료 = 리포트 반환"으로 못박는다 — 티켓당 2개 알림
    (센트럴+워커 각 1) 근본 차단. 센트럴 프레임엔 '센트럴만 상신' 문구가 있어야 한다."""
    cf = prompts.central_agent_frame()
    cont = prompts.container_agent_frame()
    uf = prompts.user_sub_frame()
    # 센트럴 = 유일 통지자.
    assert "센트럴만 수행" in cf and "유일 통지자" in cf
    # 워커 컨테이너(순수 doer): 알림 상신 금지 + 완료는 리포트 반환.
    assert "notify_report.py" in cont and "통지자가 아니다" in cont
    assert "리포트 방출뿐" in cont
    # 사용자별 서브: 알림 도구 직접 호출 금지.
    assert "notify_report" in uf and "상신 금지" in uf
    # CLAUDE.md 워커(ROLE=worker) 섹션의 금지 목록에 알림/외부통지 금지 문구가 있어야 한다.
    with open(os.path.join(_REPO_ROOT, "CLAUDE.md"), encoding="utf-8") as fh:
        claudemd = fh.read()
    assert "notify_report.py" in claudemd and "완료-상신" in claudemd


def test_user_sub_frame_instructs_ac1_fanout_and_ac2_resume_loop():
    """AC1(병렬 팬아웃: 티켓당 distinct sid) + AC2(관찰→판단→--resume 이어가기)가
    사용자 서브 프레임에 명시돼 있다."""
    uf = prompts.user_sub_frame()
    # AC1: 병렬 팬아웃 + distinct session-id.
    assert "팬아웃" in uf
    assert "distinct" in uf
    assert "sub-sub" in uf
    assert "동시" in uf  # 동시 세션
    # AC2: 관찰→판단→이어가기(--resume 재호출), JSON report 를 읽어 판단.
    assert "--resume" in uf
    assert "report" in uf
    assert "재호출" in uf


# --- Popen 대역(큐-구동: 주입 → 스크립트 응답) -------------------------------


class _FakeStdin:
    def __init__(self, on_write, on_close):
        self.writes = []
        self.closed = False
        self._on_write = on_write
        self._on_close = on_close

    def write(self, data):
        self.writes.append(data)
        self._on_write(data)

    def flush(self):
        pass

    def close(self):
        if not self.closed:
            self.closed = True
            self._on_close()


class _FakeStdout:
    """블로킹 라인 이터레이터 — 큐로 피드, 센티넬로 EOF."""

    _EOF = object()

    def __init__(self):
        self._q: queue.Queue = queue.Queue()

    def feed(self, line):
        self._q.put(line)

    def eof(self):
        self._q.put(self._EOF)

    def __iter__(self):
        return self

    def __next__(self):
        item = self._q.get()
        if item is self._EOF:
            raise StopIteration
        return item


class FakeCentralProc:
    """센트럴 세션 Popen 대역 — i번째 주입이 scripts[i] 를 stdout 에 피드."""

    def __init__(self, scripts):
        self._scripts = list(scripts)
        self._inject_idx = 0
        self.stdout = _FakeStdout()
        self.stdin = _FakeStdin(self._on_write, self._on_close)
        self.returncode = None
        self.terminated = False

    def _on_write(self, _data):
        if self._inject_idx < len(self._scripts):
            for line in self._scripts[self._inject_idx]:
                self.stdout.feed(line)
        self._inject_idx += 1

    def _on_close(self):
        self.stdout.eof()

    def poll(self):
        return 0 if self.terminated else None

    def wait(self, timeout=None):
        self.returncode = 0
        self.terminated = True
        return 0

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.terminated = True


def _init_line(sid):
    return json.dumps(
        {"type": "system", "subtype": "init", "session_id": sid}, ensure_ascii=False
    ) + "\n"


def _cfg(tmp_path):
    return SimpleNamespace(
        run=SimpleNamespace(
            claude_bin="claude", output_format="stream-json", input_format="stream-json",
            persistent_session=True, orchestrator_repo=str(tmp_path / "orch"),
            workspace_dir=str(tmp_path / "ws"), fractal_central=True,
        ),
        secrets=SimpleNamespace(base_dir=str(tmp_path / "secrets")),
    )


def _make_session(tmp_path, scripts):
    proc = FakeCentralProc(scripts)
    captured: dict = {}

    def _factory(cmd, cwd=None, env=None):
        captured["cmd"] = cmd
        captured["cwd"] = cwd
        return proc

    s = cs.CentralSession(
        _cfg(tmp_path),
        popen_factory=_factory,
        log_dir=str(tmp_path / "logs"),
    )
    # 스폰 커맨드를 테스트가 관찰할 수 있게 세션에 붙여 둔다(대역).
    s._captured = captured
    return s, proc


def _job(ticket, user="u1", repos=None):
    return SimpleNamespace(ticket=ticket, user=user, target_repos=repos or ["portal-backend"])


# --- 스폰-원스 + 재사용-if-up + 프레임 조합 ---------------------------------


def test_append_system_prompt_is_short_pointer_to_claudemd_not_full_frame(tmp_path):
    """근본 해소: 운영 규약 전문은 신뢰 정본 /app/CLAUDE.md 에 있고, 스폰 시
    --append-system-prompt 로는 그 섹션을 따르라는 **짧은 포인터**만 전달한다(신뢰되지
    않는 채널에 규약 전문을 싣지 않는다). 첫 user 메시지는 **티켓 이벤트만** 담는다."""
    scripts = [[_init_line("central-1")]]
    s, proc = _make_session(tmp_path, scripts)

    ok = s.inject_event(_job("PROJ-1", repos=["portal-backend"]))
    assert ok is True
    assert s.spawn_count == 1
    assert s.is_alive()

    # 1) --append-system-prompt 인자는 CLAUDE.md 섹션을 가리키는 짧은 포인터다.
    cmd = s._captured["cmd"]
    assert "--append-system-prompt" in cmd
    pointer = cmd[cmd.index("--append-system-prompt") + 1]
    assert pointer == cs.CLAUDE_MD_POINTER
    assert "/app/CLAUDE.md" in pointer
    assert "central 런타임 세션 운영규약" in pointer
    # 규약 전문(프레임 본문)은 신뢰되지 않는 채널로 싣지 않는다.
    assert "센트럴 에이전트 — 동작 규약(operating frame)" not in pointer
    assert prompts.central_agent_frame() not in pointer
    # 자기-정당화 메타-코멘트가 포인터에 없다.
    assert "신뢰 채널" not in pointer
    assert "인젝션" not in pointer
    # cwd 는 오케스트레이터 프레임워크 레포(정체성 원천) 그대로 유지.
    assert s._captured["cwd"] == str(tmp_path / "orch")

    # 2) 첫 user 메시지는 티켓 이벤트만 — 프레임 본문(operating frame 규약)은 안 들어간다.
    first_msg = json.loads(proc.stdin.writes[0])["message"]["content"][0]["text"]
    assert "operating frame" not in first_msg
    assert "동작 규약(operating frame)" not in first_msg  # 프레임 제목 헤딩은 없다
    assert prompts.central_agent_frame() not in first_msg
    assert "PROJ-1" in first_msg and "portal-backend" in first_msg
    assert "담당 사용자: u1" in first_msg

    # init 이벤트에서 session_id 캡처(리더 스레드가 소비할 시간).
    for _ in range(50):
        if s.session_id == "central-1":
            break
        time.sleep(0.01)
    assert s.session_id == "central-1"
    s.drain()


def test_second_event_reuses_same_session_no_respawn(tmp_path):
    """핵심 P2 단언: 2번째 이벤트는 **같은 세션 핸들**에 주입(재스폰 없음)."""
    scripts = [[_init_line("central-1")], []]
    s, proc = _make_session(tmp_path, scripts)

    assert s.inject_event(_job("PROJ-1")) is True
    assert s.spawn_count == 1

    assert s.inject_event(_job("PROJ-2", user="u2", repos=["portal-frontend"])) is True
    # ⚠️ 재사용: 프로세스를 새로 띄우지 않았다(스폰 여전히 1회).
    assert s.spawn_count == 1
    # 2번째 주입은 프레임 없이 이벤트만(프레임은 세션에 이미 확립).
    second_msg = json.loads(proc.stdin.writes[1])["message"]["content"][0]["text"]
    assert "operating frame" not in second_msg
    assert "PROJ-2" in second_msg and "portal-frontend" in second_msg
    s.drain()


def test_drain_terminates_cleanly_and_closes_stdin(tmp_path):
    scripts = [[_init_line("central-1")]]
    s, proc = _make_session(tmp_path, scripts)
    s.inject_event(_job("PROJ-1"))

    assert s.is_alive()
    s.drain()
    assert proc.stdin.closed is True
    assert proc.terminated is True
    assert s.is_alive() is False
    time.sleep(0.05)
    assert s._reader is None
    # drain 멱등(다시 불러도 예외 없음).
    s.drain()


def test_inject_failure_tears_down(tmp_path, monkeypatch):
    """stdin 주입이 실패하면 세션을 정리해 다음 이벤트가 재스폰을 유도한다."""
    scripts = [[_init_line("central-1")]]
    s, _ = _make_session(tmp_path, scripts)
    # _write_stdin 을 실패로 대역.
    monkeypatch.setattr(cs, "_write_stdin", lambda proc, data: False)
    ok = s.inject_event(_job("PROJ-1"))
    assert ok is False
    assert s.is_alive() is False


# --- docker exec 커맨드 구성(--resume 교차-exec) -----------------------------


def test_build_worker_exec_command_first_uses_session_id(tmp_path):
    cfg = _cfg(tmp_path)
    cmd = cs.build_worker_exec_command("yhchoi", "HAN-1", first=True, config=cfg)
    assert cmd[:5] == ["docker", "exec", "jad-worker-yhchoi", "claude", "-p"]
    # #2: --output-format json 으로 단일 최종 result 객체를 받는다.
    assert "--output-format" in cmd and cmd[cmd.index("--output-format") + 1] == "json"
    assert "--session-id" in cmd and "--resume" not in cmd
    sid = cmd[cmd.index("--session-id") + 1]
    # #1: 미지정 → (user,ticket) 파생 UUID.
    assert sid == cs.worker_session_id("yhchoi", "HAN-1")
    assert cmd[-1] == "HAN-1"


def test_build_worker_exec_command_subsequent_uses_resume_same_sid(tmp_path):
    """AC2: 같은 (user,ticket) 재개는 첫 턴과 **같은** 파생 sid 로 --resume 한다."""
    cfg = _cfg(tmp_path)
    first = cs.build_worker_exec_command("yhchoi", "HAN-1", first=True, config=cfg)
    later = cs.build_worker_exec_command("yhchoi", "HAN-1", first=False, config=cfg)
    assert "--resume" in later and "--session-id" not in later
    assert later[later.index("--resume") + 1] == first[first.index("--session-id") + 1]
    assert later[-1] == "HAN-1"


def test_build_worker_exec_command_uses_instruction_when_given(tmp_path):
    cfg = _cfg(tmp_path)
    cmd = cs.build_worker_exec_command(
        "yhchoi", "HAN-1", first=True, config=cfg, instruction="상세 지시 본문",
    )
    # 지시가 주어지면 프롬프트로 티켓 대신 지시 본문을 쓴다.
    assert cmd[-1] == "상세 지시 본문"


def test_build_worker_exec_command_distinct_sids_for_parallel(tmp_path):
    """AC1: 병렬 티켓은 (user,ticket) 파생으로 **자동 distinct** sid 를 얻는다."""
    cfg = _cfg(tmp_path)
    a = cs.build_worker_exec_command("u", "HAN-1", first=True, config=cfg)
    b = cs.build_worker_exec_command("u", "HAN-2", first=True, config=cfg)
    sid_a = a[a.index("--session-id") + 1]
    sid_b = b[b.index("--session-id") + 1]
    assert sid_a == cs.worker_session_id("u", "HAN-1")
    assert sid_b == cs.worker_session_id("u", "HAN-2")
    assert sid_a != sid_b


def test_build_worker_exec_command_respects_explicit_uuid(tmp_path):
    """호출자가 유효 UUID 를 주면 그대로 존중(세션 핀 고정); 비UUID 는 파생으로 대체."""
    cfg = _cfg(tmp_path)
    valid = "123e4567-e89b-12d3-a456-426614174000"
    cmd = cs.build_worker_exec_command("u", "HAN-1", first=True, config=cfg, session_id=valid)
    assert cmd[cmd.index("--session-id") + 1] == valid
    cmd2 = cs.build_worker_exec_command("u", "HAN-1", first=True, config=cfg, session_id="not-a-uuid")
    assert cmd2[cmd2.index("--session-id") + 1] == cs.worker_session_id("u", "HAN-1")


def test_worker_session_id_deterministic_and_per_ticket():
    assert cs.worker_session_id("u", "HAN-1") == cs.worker_session_id("u", "HAN-1")
    assert cs.worker_session_id("u", "HAN-1") != cs.worker_session_id("u", "HAN-2")
    assert cs.worker_session_id("a", "HAN-1") != cs.worker_session_id("b", "HAN-1")
    # 파생값은 유효 UUID 여야 한다(claude --session-id 요건).
    import uuid as _uuid
    _uuid.UUID(cs.worker_session_id("u", "HAN-1"))


def test_user_session_id_is_deterministic_and_per_user():
    assert cs.user_session_id("a") == cs.user_session_id("a")
    assert cs.user_session_id("a") != cs.user_session_id("b")


def test_format_event_carries_ticket_user_repos():
    txt = cs.format_event(_job("HAN-9", user="kim", repos=["r1", "r2"]))
    assert "HAN-9" in txt and "kim" in txt and "r1, r2" in txt
    # 미해석(빈 repos) → 전역 직렬 문구.
    txt2 = cs.format_event(SimpleNamespace(ticket="HAN-10", user="kim", target_repos=[]))
    assert "전역 직렬" in txt2


def test_format_event_carries_autonomy_mode():
    """autonomy_mode 가 이벤트에 실려 센트럴이 모드별 '다음 동작' 블록을 쓸 수 있다(additive)."""
    # A(완전자율=리뷰 모드).
    txt_a = cs.format_event(
        SimpleNamespace(ticket="HAN-11", user="kim", target_repos=["r1"], autonomy_mode="A")
    )
    assert "autonomy_mode" in txt_a
    assert "A(완전자율)" in txt_a and "리뷰 모드" in txt_a
    # B(경량 1차=이어작업 모드).
    txt_b = cs.format_event(
        SimpleNamespace(ticket="HAN-12", user="kim", target_repos=["r1"], autonomy_mode="B")
    )
    assert "autonomy_mode" in txt_b
    assert "이어작업 모드" in txt_b
    # 필드 미지정(구 Job/이벤트) → 기본 B(이어작업). 기존 필드/형식 파싱은 그대로.
    txt_default = cs.format_event(_job("HAN-13", user="kim", repos=["r1"]))
    assert "autonomy_mode" in txt_default and "이어작업 모드" in txt_default
    assert "HAN-13" in txt_default and "kim" in txt_default  # 기존 필드 불변


def test_central_frame_has_next_action_block_for_local_orchestrator():
    """central_agent_frame §3 에 '다음 동작(로컬 오케스트레이터)' 블록 + 모드별 지시 +
    autonomy_mode 문구가 존재한다(로컬 오케스트레이터용 겸용 메시지 보강)."""
    cf = prompts.central_agent_frame()
    assert "다음 동작(로컬 오케스트레이터)" in cf
    assert "autonomy_mode" in cf
    # 모드별 명시적 다음 동작(리뷰 모드=MR 리뷰·머지·Done / 이어작업 모드=fetch·남은 작업).
    assert "리뷰 모드" in cf and "이어작업 모드" in cf
    assert "리뷰" in cf and "머지" in cf and "Done" in cf
    assert "남은 작업" in cf and "fetch" in cf
    # 컨텍스트 포인터(로컬이 맥락 파악용) — dlc-meta 경로.
    assert "runs/" in cf and "audit.md" in cf
    # 전송 메커니즘은 불변(내용만 보강)임을 프레임이 명시.
    assert "전송 메커니즘" in cf or "전송 방식은 바꾸지 않는다" in cf


# --- 플래그 게이팅 -----------------------------------------------------------


def test_central_fractal_enabled_default_off():
    off = SimpleNamespace(run=SimpleNamespace(
        output_format="stream-json", input_format="stream-json", persistent_session=True))
    assert cs.central_fractal_enabled(off) is False          # fractal_central 미설정 → OFF
    on = SimpleNamespace(run=SimpleNamespace(
        fractal_central=True, output_format="stream-json",
        input_format="stream-json", persistent_session=True))
    assert cs.central_fractal_enabled(on) is True
    # 지속 세션이 꺼져 있으면 센트럴 신경로도 성립 불가(무시).
    no_persist = SimpleNamespace(run=SimpleNamespace(
        fractal_central=True, output_format="text",
        input_format="stream-json", persistent_session=True))
    assert cs.central_fractal_enabled(no_persist) is False
    assert cs.central_fractal_enabled(SimpleNamespace(run=None)) is False
