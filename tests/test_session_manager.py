"""session_manager 단위테스트 — 프랙탈 P1(사용자당 지속 세션) 신경로.

라이브 claude 는 절대 호출하지 않는다(상위가 인컨테이너 프로브로 검증 — 설계 §9).
Popen 은 큐-구동 FakeSessionProc 로 대역한다: stdin 주입이 곧 stdout 스크립트 응답을
트리거해(에이전트가 주입에 응답하는 실제 모델) 리더 스레드가 결정적으로 소비한다.

검증 대상:
    - 프롬프트 프레임 조합/추출(app.prompts).
    - 스폰-원스 + **재사용-if-up 주입**(2번째 티켓이 같은 세션 핸들에 주입, 재스폰 없음).
    - 리치 완료-리포트 캡처(retrievable place).
    - drain-종료가 깨끗이 닫힘(좀비 방지 teardown).
    - fractal_enabled 플래그 게이팅.
"""

from __future__ import annotations

import json
import queue
import time
from types import SimpleNamespace

from app import prompts
from app import session_manager as sm
from app.agent_runner import UserCreds


# --- 프롬프트 프레임(데이터) -------------------------------------------------


def test_frames_load_as_data_and_are_nonempty():
    cf = prompts.container_agent_frame()
    rf = prompts.repo_sub_frame()
    assert "리치 완료-리포트" in cf and prompts.REPORT_BEGIN in cf
    assert "레포별 서브에이전트" in rf
    # 캐시 동일 객체(파일 IO 1회).
    assert prompts.container_agent_frame() is cf


def test_repolock_ledger_path_and_protocol_in_frames():
    """P2.5: 레포락 공유 장부(파일) 경로+프로토콜이 프레임 데이터에 들어있다(코드 로직 0).

    장부는 파이썬 게이트/엔드포인트가 아니라, 에이전트가 평소 파일 도구로 읽고 쓰는
    공유 파일이다. 경로는 전 프레임에서 동일 문자열로 고정된다.
    """
    ledger_path = "<run.workspace_dir>/.jad-repolock.md"
    cf = prompts.container_agent_frame()
    uf = prompts.user_sub_frame()
    xf = prompts.central_agent_frame()
    # 경로가 세 프레임에서 동일하게 명시된다(중앙: 참조/코디네이트, 하위: check-and-record).
    for frame in (cf, uf, xf):
        assert ledger_path in frame
        assert "공유 장부" in frame
    # 하위 프레임은 착수/위임 전 확인·기록(check-and-record) 프로토콜을 담는다.
    assert "check-and-record" in cf and "check-and-record" in uf
    for verb in ("Read", "append", "지운다"):
        assert verb in cf and verb in uf
    # 프랙탈 경로의 레포락 결정 기준은 /scheduler/state HTTP 가 아니라 장부 파일이다.
    assert "curl" not in xf


def test_compose_places_frame_before_core():
    out = prompts.compose("FRAME-TEXT", "CORE-INSTRUCTION")
    assert out.startswith("FRAME-TEXT")
    assert out.rstrip().endswith("CORE-INSTRUCTION")
    # 한쪽이 비면 다른 쪽만.
    assert prompts.compose("", "C") == "C"
    assert prompts.compose("F", "") == "F"


def test_extract_completion_report_block_and_ticket():
    text = (
        "머릿말\n"
        f"{prompts.REPORT_BEGIN} PROJ-7===\n"
        "- 티켓: PROJ-7 — 요지\n- 레포: r\n"
        f"{prompts.REPORT_END} PROJ-7===\n"
        "꼬리말"
    )
    ticket, body = prompts.extract_completion_report(text)
    assert ticket == "PROJ-7"
    assert "PROJ-7 — 요지" in body and prompts.REPORT_BEGIN not in body
    assert prompts.extract_completion_report("리포트 없음") is None


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


class FakeSessionProc:
    """지속 세션 Popen 대역 — stdin 주입이 i번째 스크립트 응답을 stdout 에 피드.

    ``scripts[i]`` = i번째 주입에 대한 stdout 라인 리스트(에이전트 응답). 주입 순간
    피드하므로 run_ticket 이 아직 대기(_await 세팅)에 들어간 뒤 응답이 오는 실제 순서를
    보장한다(레이스 없음).
    """

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


def _result_line(text):
    return json.dumps(
        {"type": "result", "subtype": "success", "is_error": False, "result": text},
        ensure_ascii=False,
    ) + "\n"


def _init_line(sid):
    return json.dumps(
        {"type": "system", "subtype": "init", "session_id": sid}, ensure_ascii=False
    ) + "\n"


def _cfg(tmp_path):
    return SimpleNamespace(
        run=SimpleNamespace(
            claude_bin="claude", output_format="stream-json", input_format="stream-json",
            persistent_session=True, orchestrator_repo=str(tmp_path / "orch"),
            workspace_dir=str(tmp_path / "ws"), session_max_sec=30, session_idle_sec=30,
            fractal_worker=True,
        ),
        secrets=SimpleNamespace(base_dir=str(tmp_path / "secrets")),
    )


def _report_text(ticket, mr=None):
    lines = [
        "작업을 마쳤습니다.",
        f"{prompts.REPORT_BEGIN} {ticket}===",
        f"- 티켓: {ticket} — 1차 산출",
        "- 레포: portal-backend",
        f"- 브랜치: auto/{ticket}",
        f"- MR: {mr or '없음(모드 B)'}",
        "- 테스트: pytest 3 passed",
        f"- 사이클로그: runs/{ticket}/",
        f"{prompts.REPORT_END} {ticket}===",
    ]
    return "\n".join(lines)


def _make_session(tmp_path, scripts):
    """provision 를 no-op 로 주입한 UserSession + 그 세션이 띄울 FakeSessionProc."""
    proc = FakeSessionProc(scripts)
    creds = UserCreds(user="u1")
    s = sm.UserSession(
        _cfg(tmp_path), creds, user="u1",
        popen_factory=lambda cmd, cwd=None, env=None: proc,
        provision_fn=lambda *a, **k: None,   # 프로비저닝 skip(레포 IO 없음)
        log_dir=str(tmp_path / "logs"),
    )
    return s, proc


# --- 스폰-원스 + 재사용-if-up + 리포트 캡처 ---------------------------------


def test_first_ticket_spawns_and_injects_frame_then_captures_report(tmp_path):
    mr = "https://gitlab.example.com/g/p/-/merge_requests/9"
    scripts = [[_init_line("sess-1"), _result_line(_report_text("PROJ-1", mr))]]
    s, proc = _make_session(tmp_path, scripts)

    res = s.run_ticket({"ticket": "PROJ-1", "autonomy_mode": "B"}, s.creds, s.config)

    assert res.status == "done"
    assert res.session_id == "sess-1"
    assert res.mr_url == mr
    # 첫 주입엔 컨테이너 프레임이 앞에 조합됐다.
    first_msg = json.loads(proc.stdin.writes[0])["message"]["content"][0]["text"]
    assert "동작 규약(operating frame)" in first_msg
    assert "PROJ-1" in first_msg
    # 리치 완료-리포트가 캡처됐다(retrievable place).
    rep = s.get_report("PROJ-1")
    assert rep is not None and "1차 산출" in rep and prompts.REPORT_BEGIN not in rep
    s.drain()


def test_second_ticket_reuses_same_session_no_respawn(tmp_path):
    """핵심 P1 단언: 2번째 티켓은 **같은 세션 핸들**에 주입(재스폰 없음)."""
    scripts = [
        [_init_line("sess-1"), _result_line(_report_text("PROJ-1"))],
        [_result_line(_report_text("PROJ-2"))],
    ]
    s, proc = _make_session(tmp_path, scripts)

    r1 = s.run_ticket({"ticket": "PROJ-1"}, s.creds, s.config)
    assert r1.status == "done"
    assert s.spawn_count == 1
    assert s.is_alive()   # 완료 후에도 세션 살아있음(drain 전까지).

    r2 = s.run_ticket({"ticket": "PROJ-2"}, s.creds, s.config)
    assert r2.status == "done"
    # ⚠️ 재사용: 프로세스를 새로 띄우지 않았다(스폰 여전히 1회).
    assert s.spawn_count == 1
    # 2번째 주입은 프레임 없이 핵심 지시만(프레임은 세션에 이미 확립).
    second_msg = json.loads(proc.stdin.writes[1])["message"]["content"][0]["text"]
    assert "동작 규약(operating frame)" not in second_msg
    assert "PROJ-2" in second_msg
    assert s.get_report("PROJ-2") is not None
    s.drain()


def test_drain_terminates_cleanly_and_closes_stdin(tmp_path):
    scripts = [[_result_line(_report_text("PROJ-1"))]]
    s, proc = _make_session(tmp_path, scripts)
    s.run_ticket({"ticket": "PROJ-1"}, s.creds, s.config)

    assert s.is_alive()
    s.drain()
    # EOF(정상 종료) + wait 로 reap.
    assert proc.stdin.closed is True
    assert proc.terminated is True
    assert s.is_alive() is False
    # 리더 스레드가 join 됐다(좀비/누수 없음).
    time.sleep(0.05)
    assert s._reader is None

    # drain 멱등(다시 불러도 예외 없음).
    s.drain()


def test_limit_result_is_interrupted_with_reset(tmp_path):
    lim = ("Claude usage limit reached. reset at 2099-01-01T00:00:00Z")
    scripts = [[_result_line(lim)]]
    s, proc = _make_session(tmp_path, scripts)
    res = s.run_ticket({"ticket": "PROJ-3"}, s.creds, s.config)
    assert res.status == "interrupted"
    assert res.reset_at == "2099-01-01T00:00:00Z"
    s.drain()


def test_pending_bg_result_not_terminal_until_cleared(tmp_path):
    """백그라운드 위임 대기 중 result 는 비종결 — 비워진 뒤 result 가 종결(HAN-537 동형)."""
    bg = (json.dumps({"type": "system", "subtype": "background_tasks_changed",
                      "tasks": [{"task_id": "a"}]}, ensure_ascii=False) + "\n")
    awaiting = _result_line("백그라운드 대기 중")
    cleared = (json.dumps({"type": "system", "subtype": "background_tasks_changed",
                           "tasks": []}, ensure_ascii=False) + "\n")
    final = _result_line(_report_text("PROJ-4"))
    scripts = [[bg, awaiting, cleared, final]]
    s, proc = _make_session(tmp_path, scripts)
    res = s.run_ticket({"ticket": "PROJ-4"}, s.creds, s.config)
    assert res.status == "done"      # 첫 result(대기)에서 종결 안 됨 → 둘째까지 소비.
    assert s.get_report("PROJ-4") is not None
    s.drain()


# --- 플래그 게이팅 -----------------------------------------------------------


def test_fractal_enabled_default_off():
    off = SimpleNamespace(run=SimpleNamespace(
        output_format="stream-json", input_format="stream-json", persistent_session=True))
    assert sm.fractal_enabled(off) is False           # fractal_worker 미설정 → OFF
    on = SimpleNamespace(run=SimpleNamespace(
        fractal_worker=True, output_format="stream-json",
        input_format="stream-json", persistent_session=True))
    assert sm.fractal_enabled(on) is True
    # 지속 세션이 꺼져 있으면 fractal 도 성립 불가(무시).
    no_persist = SimpleNamespace(run=SimpleNamespace(
        fractal_worker=True, output_format="text",
        input_format="stream-json", persistent_session=True))
    assert sm.fractal_enabled(no_persist) is False
    assert sm.fractal_enabled(SimpleNamespace(run=None)) is False
