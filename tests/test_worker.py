"""worker 단위테스트 — 폴링 루프(mock HTTP): 204 sleep, 잡 실행·회신,
interrupted→resume 경로, 예외 격리, 시크릿 헤더. + main.run_worker 배선.

라이브 네트워크/claude는 호출하지 않는다(run/resume는 스텁 주입).
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from app import agent_runner as ar
from app import main
from app import worker as w


# --- HTTP 대역 ---------------------------------------------------------------


class FakeResp:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no json body")
        return self._payload


class FakeHTTP:
    def __init__(self, get_responses):
        self._gets = list(get_responses)
        self.get_calls = []
        self.posts = []

    def get(self, url, headers=None):
        self.get_calls.append((url, headers))
        return self._gets.pop(0) if self._gets else FakeResp(204)

    def post(self, url, json=None, headers=None):
        self.posts.append((url, json, headers))
        return FakeResp(200, {})


def _cfg():
    # 단일 잡 경로 회귀 테스트용 기본 fixture. worker_max_concurrency=1로 고정해
    # 이 파일의 단일 잡 loop 메커니즘 테스트가 결정적으로 단일 경로를 탄다(동시 경로 테스트는
    # concurrency=2를 명시로 오버라이드). 프로덕션 기본 안전 상한(64)은 test_config/test_spawner가 검증.
    return SimpleNamespace(
        resume=SimpleNamespace(reset_buffer_sec=0),
        run=SimpleNamespace(orchestrator_repo="/app/orch", claude_bin="claude",
                            output_format="stream-json", worker_max_concurrency=1),
        secrets=SimpleNamespace(base_dir=""),
    )


_ENV = {"DISPATCH_USER": "u1", "CENTRAL_URL": "http://central:8787"}
_NOW = lambda: datetime(2026, 1, 1, tzinfo=timezone.utc)


def _statuses(http):
    return [p[1].get("status") for p in http.posts]


# --- 폴링 루프 ---------------------------------------------------------------


def test_loop_204_sleeps_and_no_post():
    http = FakeHTTP([FakeResp(204)])
    slept = []
    n = w.worker_loop(_cfg(), env=dict(_ENV), http=http,
                      run=lambda *a, **k: None, resume=lambda *a, **k: None,
                      sleep=slept.append, now=_NOW, max_iterations=1)
    assert n == 1
    assert http.posts == []          # 잡 없으면 회신 없음
    assert slept == [w.DEFAULT_POLL_INTERVAL_SEC]
    assert http.get_calls[0][0] == "http://central:8787/dispatch/u1/next"


def test_loop_runs_job_and_reports_running_then_done():
    job = {"ticket": "PROJ-1", "autonomy_mode": "A", "branch": "auto/PROJ-1"}
    http = FakeHTTP([FakeResp(200, job), FakeResp(204)])
    result = ar.AgentResult(status=ar.STATUS_DONE, session_id="s1",
                            mr_url="http://mr/1", log_summary="ok")
    n = w.worker_loop(_cfg(), env=dict(_ENV), http=http,
                      run=lambda *a, **k: result, resume=lambda *a, **k: result,
                      sleep=lambda s: None, now=_NOW, max_iterations=2)
    assert n == 2
    assert _statuses(http) == ["진행중", "완료"]
    # 완료 회신에 mr_url/session_id 포함
    done_payload = http.posts[-1][1]
    assert done_payload["mr_url"] == "http://mr/1"
    assert done_payload["session_id"] == "s1"
    # status URL은 ticket 기반
    assert http.posts[0][0] == "http://central:8787/dispatch/u1/PROJ-1/status"


def test_loop_interrupted_then_resume_to_done():
    job = {"ticket": "PROJ-2", "autonomy_mode": "B"}
    http = FakeHTTP([FakeResp(200, job)])
    calls = {"run": 0, "resume": 0}

    def run(*a, **k):
        calls["run"] += 1
        return ar.AgentResult(status=ar.STATUS_INTERRUPTED, session_id="s2",
                              reset_at="2026-01-01T00:00:00Z")

    def resume(job_, sid, creds, cfg, **k):
        calls["resume"] += 1
        assert sid == "s2"
        return ar.AgentResult(status=ar.STATUS_DONE, session_id="s2", mr_url="http://mr/2")

    slept = []
    w.worker_loop(_cfg(), env=dict(_ENV), http=http, run=run, resume=resume,
                  sleep=slept.append, now=_NOW, max_iterations=1)
    assert calls == {"run": 1, "resume": 1}
    assert _statuses(http) == ["진행중", "interrupted", "완료"]
    interrupted_payload = http.posts[1][1]
    assert interrupted_payload["reset_at"] == "2026-01-01T00:00:00Z"


def test_loop_notifies_on_terminal_done():
    """터미널(done) 결과에서 완료 알림(notify)이 호출된다(주입 대역으로 검증)."""
    job = {"ticket": "PROJ-N", "autonomy_mode": "A", "branch": "auto/PROJ-N"}
    http = FakeHTTP([FakeResp(200, job), FakeResp(204)])
    result = ar.AgentResult(status=ar.STATUS_DONE, session_id="s1", mr_url="http://mr/1")
    seen = []
    w.worker_loop(_cfg(), env=dict(_ENV), http=http,
                  run=lambda *a, **k: result, resume=lambda *a, **k: result,
                  sleep=lambda s: None, now=_NOW, max_iterations=2,
                  notify=lambda cfg, res, j, creds: seen.append((res.status, j["ticket"])))
    assert seen == [(ar.STATUS_DONE, "PROJ-N")]


def test_loop_notify_failure_does_not_kill_job():
    """알림 대역이 던져도 잡/루프는 죽지 않고 채널 F 회신은 정상(격리)."""
    job = {"ticket": "PROJ-NF", "autonomy_mode": "B"}
    http = FakeHTTP([FakeResp(200, job), FakeResp(204)])
    result = ar.AgentResult(status=ar.STATUS_DONE, session_id="s1")

    def boom_notify(*a, **k):
        raise RuntimeError("chat down")

    n = w.worker_loop(_cfg(), env=dict(_ENV), http=http,
                      run=lambda *a, **k: result, resume=lambda *a, **k: result,
                      sleep=lambda s: None, now=_NOW, max_iterations=2,
                      notify=boom_notify)
    assert n == 2
    # 알림 실패에도 최종 회신(완료)은 남는다.
    assert _statuses(http) == ["진행중", "완료"]


def test_loop_exception_isolation_does_not_crash():
    job = {"ticket": "PROJ-3"}
    http = FakeHTTP([FakeResp(200, job), FakeResp(204)])

    def boom(*a, **k):
        raise RuntimeError("agent blew up")

    slept = []
    n = w.worker_loop(_cfg(), env=dict(_ENV), http=http, run=boom,
                      resume=lambda *a, **k: None, sleep=slept.append,
                      now=_NOW, max_iterations=2)
    assert n == 2                       # 예외에도 루프 생존
    # 착수(진행중)는 보고됐고, 그 뒤 예외로 중단 → 최종 회신 없음
    assert _statuses(http) == ["진행중"]
    assert len(slept) >= 1              # 예외 후 백오프 sleep


def test_loop_sends_worker_secret_header():
    http = FakeHTTP([FakeResp(204)])
    env = dict(_ENV, WORKER_SHARED_SECRET="topsecret")
    w.worker_loop(_cfg(), env=env, http=http, run=lambda *a, **k: None,
                  resume=lambda *a, **k: None, sleep=lambda s: None,
                  now=_NOW, max_iterations=1)
    _, headers = http.get_calls[0]
    assert headers.get("X-Worker-Secret") == "topsecret"


def test_loop_requires_user_and_central():
    import pytest
    with pytest.raises(RuntimeError):
        w.worker_loop(_cfg(), env={}, http=FakeHTTP([]), max_iterations=1)


# --- 복원력: central 재기동 창의 연결 오류 재시도/백오프 --------------------


def test_backoff_delay_schedule_and_cap():
    # 지수 백오프 1·2·4·8·16… 이후 cap.
    assert [w._backoff_delay(i) for i in range(5)] == [1, 2, 4, 8, 16]
    assert w._backoff_delay(100) == w.RETRY_MAX_BACKOFF_SEC


class _SeqHTTP:
    """get이 시퀀스(예외 or FakeResp)를 순서대로 내고, post는 성공 기록."""

    def __init__(self, get_seq):
        self._seq = list(get_seq)
        self._i = 0
        self.posts = []

    def get(self, url, headers=None):
        item = self._seq[self._i] if self._i < len(self._seq) else FakeResp(204)
        self._i += 1
        if isinstance(item, Exception):
            raise item
        return item

    def post(self, url, json=None, headers=None):
        self.posts.append((url, json, headers))
        return FakeResp(200, {})


def test_fetch_next_retries_on_connection_error_then_delivers_job():
    """_fetch_next는 연결 오류를 백오프로 계속 재시도하고, 복귀하면 잡을 전달한다.

    루프가 죽지 않고, 백오프 시퀀스(1·2초)가 관측된다.
    """
    job = {"ticket": "PROJ-R", "autonomy_mode": "A", "branch": "auto/PROJ-R"}
    http = _SeqHTTP([
        ConnectionError("Failed to resolve 'central'"),
        ConnectionError("Failed to resolve 'central'"),
        FakeResp(200, job),
        FakeResp(204),
    ])
    result = ar.AgentResult(status=ar.STATUS_DONE, session_id="s1")
    slept = []
    n = w.worker_loop(_cfg(), env=dict(_ENV), http=http,
                      run=lambda *a, **k: result, resume=lambda *a, **k: result,
                      sleep=slept.append, now=_NOW, max_iterations=1,
                      notify=lambda *a, **k: None)
    assert n == 1
    # 2회 연결 실패 → 백오프 1·2초 후 잡 수신.
    assert slept[:2] == [1, 2]
    assert _statuses(http) == ["진행중", "완료"]


def test_fetch_next_finite_retries_then_none():
    """연결 오류가 계속되면 유한 재시도(FETCH_RETRY_ATTEMPTS) 후 None 반환 —
    한 폴 호출을 무한 재시도하지 않는다(수정 1). 예외로 죽지 않는다.
    """
    class HTTP:
        def __init__(self):
            self.n = 0

        def get(self, url, headers=None):
            self.n += 1
            raise ConnectionError("Failed to resolve 'central'")

        def post(self, *a, **k):
            return FakeResp(200, {})

    http = HTTP()
    slept = []
    got = w._fetch_next(http, "http://c", "u1", {}, sleep=slept.append)
    assert got is None
    assert http.n == w.FETCH_RETRY_ATTEMPTS            # 유한 횟수만 시도(무한 아님)
    assert len(slept) == w.FETCH_RETRY_ATTEMPTS - 1    # 마지막 시도엔 sleep 없음


def test_loop_survives_when_fetch_exhausts_retries():
    """폴이 유한 재시도를 소진해 None이면 루프는 죽지 않고 폴 간격만큼 쉰 뒤 계속한다."""
    class HTTP:
        def get(self, url, headers=None):
            raise ConnectionError("central down")

        def post(self, *a, **k):
            return FakeResp(200, {})

    slept = []
    n = w.worker_loop(_cfg(), env=dict(_ENV), http=HTTP(),
                      run=lambda *a, **k: None, resume=lambda *a, **k: None,
                      sleep=slept.append, now=_NOW, max_iterations=2)
    assert n == 2                       # 루프 생존(2회 반복)
    # 각 반복: 재시도 백오프 sleep들 + 소진 후 폴 간격 sleep.
    assert slept.count(w.DEFAULT_POLL_INTERVAL_SEC) == 2


def test_loop_does_not_rerun_completed_job_when_terminal_post_failed():
    """터미널 회신이 미도달해 central이 같은 잡을 다시 dispatch해도 **재실행하지 않는다**
    (무한루프 방지, 수정 1). 재실행 대신 종결 회신만 재시도한다.
    """
    job = {"ticket": "PROJ-DUP", "autonomy_mode": "A"}

    class HTTP:
        def __init__(self):
            self.gets = 0
            self.post_calls = 0

        def get(self, url, headers=None):
            self.gets += 1
            return FakeResp(200, job) if self.gets <= 2 else FakeResp(204)

        def post(self, url, json=None, headers=None):
            self.post_calls += 1
            raise ConnectionError("central down")   # 회신 계속 실패

    http = HTTP()
    ran = {"n": 0}

    def run(*a, **k):
        ran["n"] += 1
        return ar.AgentResult(status=ar.STATUS_DONE, session_id="s1")

    n = w.worker_loop(_cfg(), env=dict(_ENV), http=http, run=run,
                      resume=lambda *a, **k: None, sleep=lambda s: None,
                      now=_NOW, max_iterations=2, notify=lambda *a, **k: None)
    assert n == 2
    assert ran["n"] == 1     # 두 번째 fetch에서 재실행하지 않는다(무한루프 방지)
    assert http.post_calls > 0   # 대신 종결 회신을 재시도했다


def test_loop_reposts_cached_terminal_status_until_success():
    """미도달했던 종결 회신은 central 복귀 시 재-fetch에서 성공적으로 재전달된다.

    첫 처리에서 '완료' 회신이 유한 재시도를 모두 소진(캐시)하고, 재-fetch에서
    재실행 없이 캐시된 회신만 재시도해 성공한다.
    """
    job = {"ticket": "PROJ-CACHE", "autonomy_mode": "A"}

    class HTTP:
        def __init__(self):
            self.gets = 0
            self.terminal_attempts = 0
            self.ok_posts = []

        def get(self, url, headers=None):
            self.gets += 1
            return FakeResp(200, job) if self.gets <= 2 else FakeResp(204)

        def post(self, url, json=None, headers=None):
            status = json.get("status")
            if status == "완료":
                self.terminal_attempts += 1
                # 첫 처리의 회신(STATUS_POST_ATTEMPTS회)은 모두 실패 → 캐시.
                if self.terminal_attempts <= w.STATUS_POST_ATTEMPTS:
                    raise ConnectionError("down")
                # 재-fetch에서의 재회신은 성공.
                self.ok_posts.append(status)
                return FakeResp(200, {})
            return FakeResp(200, {})   # 진행중은 정상

    http = HTTP()
    ran = {"n": 0}

    def run(*a, **k):
        ran["n"] += 1
        return ar.AgentResult(status=ar.STATUS_DONE, session_id="s1")

    n = w.worker_loop(_cfg(), env=dict(_ENV), http=http, run=run,
                      resume=lambda *a, **k: run(), sleep=lambda s: None,
                      now=_NOW, max_iterations=2, notify=lambda *a, **k: None)
    assert n == 2
    assert ran["n"] == 1             # 재실행 없이(캐시 회신만)
    assert http.ok_posts == ["완료"]  # 캐시된 완료 회신이 재전달됨


def test_fetch_next_stop_event_breaks_retry_loop():
    """재시도 중 stop_event가 set되면 즉시 None으로 빠져나온다(무한루프 방지)."""
    import threading

    stop = threading.Event()
    calls = {"n": 0}

    class HTTP:
        def get(self, url, headers=None):
            calls["n"] += 1
            stop.set()  # 첫 실패 후 정지 신호
            raise ConnectionError("down")

        def post(self, *a, **k):
            return FakeResp(200, {})

    got = w._fetch_next(HTTP(), "http://c", "u1", {}, sleep=lambda s: None, stop_event=stop)
    assert got is None
    assert calls["n"] == 1  # 한 번 실패 후 stop으로 종료


def test_post_status_retries_then_succeeds():
    """상태 POST는 유한 재시도 후 복귀하면 성공(True)하고 백오프가 관측된다."""
    calls = {"n": 0}

    class HTTP:
        def __init__(self):
            self.posts = []

        def post(self, url, json=None, headers=None):
            calls["n"] += 1
            if calls["n"] <= 2:
                raise ConnectionError("boom")
            self.posts.append((url, json, headers))
            return FakeResp(200, {})

    http = HTTP()
    slept = []
    ok = w._post_status(http, "http://c", "u1", "T", {}, {"status": "완료"},
                        sleep=slept.append)
    assert ok is True
    assert slept == [1, 2]
    assert len(http.posts) == 1


def test_post_status_gives_up_after_attempts_without_raising():
    """유한 재시도 소진 시 예외 없이 False 반환 — 연결 실패를 job 실패로 오판 금지."""
    class HTTP:
        def post(self, url, json=None, headers=None):
            raise ConnectionError("central down")

    slept = []
    ok = w._post_status(HTTP(), "http://c", "u1", "T", {}, {"status": "완료"},
                        sleep=slept.append)
    assert ok is False
    assert len(slept) == w.STATUS_POST_ATTEMPTS - 1  # 마지막 시도엔 sleep 없음


def test_loop_survives_status_post_connection_error_no_failed_misjudge():
    """상태 회신이 연결 오류로 계속 실패해도 루프는 생존하고 잡을 재실행/failed로
    오판하지 않는다(회신만 못 했을 뿐).
    """
    job = {"ticket": "PROJ-X", "autonomy_mode": "A"}

    class HTTP:
        def __init__(self):
            self.gets = 0

        def get(self, url, headers=None):
            self.gets += 1
            return FakeResp(200, job) if self.gets == 1 else FakeResp(204)

        def post(self, url, json=None, headers=None):
            raise ConnectionError("Failed to resolve 'central'")

    result = ar.AgentResult(status=ar.STATUS_DONE, session_id="s1")
    ran = {"n": 0}

    def run(*a, **k):
        ran["n"] += 1
        return result

    n = w.worker_loop(_cfg(), env=dict(_ENV), http=HTTP(), run=run,
                      resume=lambda *a, **k: result, sleep=lambda s: None,
                      now=_NOW, max_iterations=1, notify=lambda *a, **k: None)
    assert n == 1          # 루프 생존
    assert ran["n"] == 1   # 잡은 한 번만 실행(연결 실패가 failed/재실행으로 번지지 않음)


# --- per-user 동시 실행(Increment 2) --------------------------------------


import threading  # noqa: E402
import time  # noqa: E402
from urllib.parse import parse_qs, urlparse  # noqa: E402


class ConcHTTP:
    """동시성 테스트용 fake central.

    - GET /next?exclude=... : exclude·이미 종결된 티켓을 빼고 첫 남은 잡을 준다
      (central의 next_for_user + 레포락 dispatch를 흉내). 없으면 204.
    - POST .../<ticket>/status : 터미널 회신을 받으면 그 티켓을 소진 처리한다.
    모든 상태는 락으로 보호(여러 잡 스레드가 동시에 post).
    """

    def __init__(self, jobs):
        self._available = [dict(j) for j in jobs]
        self._terminal = set()
        self._lock = threading.Lock()
        self.posts = []

    def get(self, url, headers=None):
        exclude = set()
        q = urlparse(url).query
        if q:
            raw = (parse_qs(q).get("exclude") or [""])[0]
            exclude = {t for t in raw.split(",") if t}
        with self._lock:
            for j in self._available:
                tk = j["ticket"]
                if tk in self._terminal or tk in exclude:
                    continue
                return FakeResp(200, dict(j))
        return FakeResp(204)

    @staticmethod
    def _ticket_of(url):
        return url.rstrip("/").split("/")[-2]  # .../<ticket>/status

    def post(self, url, json=None, headers=None):
        with self._lock:
            self.posts.append((url, json, headers))
            status = (json or {}).get("status")
            if status in ("완료", "failed", "cancelled", "handed_off"):
                self._terminal.add(self._ticket_of(url))
        return FakeResp(200, {})

    def statuses(self):
        with self._lock:
            return [p[1].get("status") for p in self.posts]


def _wait_until(pred, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return False


def test_worker_runs_two_jobs_concurrently():
    """concurrency=2: 서로 다른 두 잡이 **동시에** _process_job(run)에 진입한다.

    barrier(2)로 '둘 다 도착해야 통과'를 강제 → 통과 자체가 동시 실행의 증거.
    둘 다 완료 회신도 확인한다.
    """
    jobs = [{"ticket": "P1", "autonomy_mode": "A", "branch": "auto/P1"},
            {"ticket": "P2", "autonomy_mode": "A", "branch": "auto/P2"}]
    http = ConcHTTP(jobs)
    barrier = threading.Barrier(2, timeout=5)
    started = []
    slock = threading.Lock()

    def run(job, creds, cfg, cancel_check=None):
        with slock:
            started.append(job["ticket"])
        barrier.wait()   # 둘 다 도착 → 동시 실행 증명
        return ar.AgentResult(status=ar.STATUS_DONE, session_id="s")

    stop = threading.Event()
    t = threading.Thread(target=lambda: w.worker_loop(
        _cfg(), env=dict(_ENV), http=http, run=run, resume=lambda *a, **k: None,
        sleep=lambda s: time.sleep(0.005), now=_NOW, stop_event=stop,
        concurrency=2, notify=lambda *a, **k: None))
    t.start()
    assert _wait_until(lambda: len(started) >= 2), "두 잡이 동시에 시작되지 않음"
    assert sorted(started) == ["P1", "P2"]
    stop.set()
    t.join(timeout=5)
    assert not t.is_alive()
    assert http.statuses().count("완료") == 2       # 둘 다 터미널 회신


def test_worker_third_job_starts_only_after_slot_frees():
    """concurrency=2: 3번째 잡은 슬롯이 빌 때(한 잡 종료)까지 시작되지 않는다."""
    jobs = [{"ticket": "P1"}, {"ticket": "P2"}, {"ticket": "P3"}]
    http = ConcHTTP(jobs)
    ev = {tk: threading.Event() for tk in ("P1", "P2", "P3")}
    started = []
    slock = threading.Lock()

    def run(job, creds, cfg, cancel_check=None):
        tk = job["ticket"]
        with slock:
            started.append(tk)
        ev[tk].wait(timeout=5)   # 자기 이벤트가 set될 때까지 블록(슬롯 점유)
        return ar.AgentResult(status=ar.STATUS_DONE, session_id="s")

    stop = threading.Event()
    t = threading.Thread(target=lambda: w.worker_loop(
        _cfg(), env=dict(_ENV), http=http, run=run, resume=lambda *a, **k: None,
        sleep=lambda s: time.sleep(0.005), now=_NOW, stop_event=stop,
        concurrency=2, notify=lambda *a, **k: None))
    t.start()
    # P1·P2 두 슬롯이 찰 때까지 대기 — P3는 아직 못 뜬다.
    assert _wait_until(lambda: len(started) >= 2)
    assert sorted(started) == ["P1", "P2"]
    assert "P3" not in started                       # cap=2 → 3번째 대기
    ev["P1"].set()                                   # 슬롯 하나 비움
    assert _wait_until(lambda: "P3" in started)      # 이제 P3 시작
    ev["P2"].set()
    ev["P3"].set()
    stop.set()
    t.join(timeout=5)
    assert not t.is_alive()


def test_concurrency_one_is_single_path_regression():
    """concurrency=1은 기존 단일 잡 경로와 **동일**하게 동작한다(회귀 안전)."""
    job = {"ticket": "PROJ-1", "autonomy_mode": "A", "branch": "auto/PROJ-1"}
    http = FakeHTTP([FakeResp(200, job), FakeResp(204)])
    result = ar.AgentResult(status=ar.STATUS_DONE, session_id="s1",
                            mr_url="http://mr/1", log_summary="ok")
    n = w.worker_loop(_cfg(), env=dict(_ENV), http=http,
                      run=lambda *a, **k: result, resume=lambda *a, **k: result,
                      sleep=lambda s: None, now=_NOW, max_iterations=2, concurrency=1)
    assert n == 2
    assert _statuses(http) == ["진행중", "완료"]
    # 단일 경로 URL은 exclude 쿼리를 붙이지 않는다.
    assert http.get_calls[0][0] == "http://central:8787/dispatch/u1/next"


def test_worker_concurrency_defaults_to_safety_ceiling():
    """worker 동시 실행은 runaway 방지 **안전 상한**으로 파생된다(정책 cap 아님).

    우선순위: env WORKER_CONCURRENCY > config.run.worker_max_concurrency(>0) >
    기본 64. 진짜 스로틀은 central의 서버 자원 어드미션이므로 이 값은 상한일 뿐이다.
    """
    # config에 worker_max_concurrency 없음 → 기본 안전 상한 64.
    cfg_no_field = SimpleNamespace(run=SimpleNamespace())
    assert w._worker_concurrency({}, cfg_no_field) == w.DEFAULT_WORKER_MAX_CONCURRENCY == 64
    # config 값 존중.
    cfg_val = SimpleNamespace(run=SimpleNamespace(worker_max_concurrency=8))
    assert w._worker_concurrency({}, cfg_val) == 8
    # env가 최우선(스포너 주입).
    assert w._worker_concurrency({"WORKER_CONCURRENCY": "3"}, cfg_val) == 3


def test_locked_dict_thread_safe_under_concurrent_access():
    """_LockedDict가 다수 스레드의 set/get/pop/contains 동시 접근에도 견딘다."""
    d = w._LockedDict()
    errors = []

    def hammer(n):
        try:
            for i in range(2000):
                k = f"t{n}-{i % 8}"
                d[k] = i
                _ = d.get(k)
                _ = k in d
                if i % 3 == 0:
                    d.pop(k, None)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=hammer, args=(n,)) for n in range(8)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert errors == []


def test_concurrent_completed_cache_reposts_without_rerun():
    """동시 경로에서도 터미널 회신 미도달 잡은 재실행 없이 캐시 회신만 재시도한다.

    첫 '완료' 회신이 STATUS_POST_ATTEMPTS회 모두 연결 실패(캐시) → 재-fetch에서
    재실행(run) 없이 캐시된 회신만 성공적으로 재전달한다(무한 재실행 방지, 스레드 안전).
    """
    job = {"ticket": "PC1", "autonomy_mode": "A"}

    class FailTerminalHTTP(ConcHTTP):
        def __init__(self, jobs, fail_times):
            super().__init__(jobs)
            self._fail_left = dict(fail_times)

        def post(self, url, json=None, headers=None):
            status = (json or {}).get("status")
            tk = self._ticket_of(url)
            with self._lock:
                if status == "완료" and self._fail_left.get(tk, 0) > 0:
                    self._fail_left[tk] -= 1
                    raise ConnectionError("central down")
                self.posts.append((url, json, headers))
                if status in ("완료", "failed", "cancelled", "handed_off"):
                    self._terminal.add(tk)
            return FakeResp(200, {})

    http = FailTerminalHTTP([job], {"PC1": w.STATUS_POST_ATTEMPTS})
    ran = {"n": 0}
    rlock = threading.Lock()

    def run(*a, **k):
        with rlock:
            ran["n"] += 1
        return ar.AgentResult(status=ar.STATUS_DONE, session_id="s1")

    stop = threading.Event()
    t = threading.Thread(target=lambda: w.worker_loop(
        _cfg(), env=dict(_ENV), http=http, run=run, resume=lambda *a, **k: None,
        sleep=lambda s: None, now=_NOW, stop_event=stop,
        concurrency=2, notify=lambda *a, **k: None))
    t.start()
    # 캐시된 완료 회신이 재전달(성공적으로 posts에 기록)될 때까지 대기.
    assert _wait_until(lambda: "완료" in http.statuses())
    stop.set()
    t.join(timeout=5)
    assert not t.is_alive()
    assert ran["n"] == 1                    # 재실행 없이(캐시 회신만)


# --- main.run_worker 배선 ----------------------------------------------------


# --- 작업 C: central reconcile 배선(main) -----------------------------------


def test_has_active_job_predicate_reflects_active_statuses():
    from app import queue as q

    class FakeQ:
        def list_jobs(self):
            return [SimpleNamespace(user="u1", status=q.RUNNING),
                    SimpleNamespace(user="u3", status=q.CANCELLING),
                    SimpleNamespace(user="u2", status="done")]

    pred = main._has_active_job_predicate({"queue": FakeQ()})
    assert pred("u1") is True      # running
    assert pred("u3") is True      # cancelling(레포 붙들고 있음)
    assert pred("u2") is False     # terminal
    assert pred("nobody") is False


def test_has_active_job_predicate_conservative_without_queue():
    # 큐 확인 불가 → 보수적으로 활성(True)로 봐 in-flight 잡을 배포가 죽이지 않게.
    pred = main._has_active_job_predicate({"queue": None})
    assert pred("anyone") is True


def test_reconcile_worker_images_passes_enabled_users_and_predicate():
    captured = {}

    class FakeSpawner:
        def reconcile_workers(self, users, has_active_job, **k):
            captured["users"] = [u.username for u in users]
            captured["pred_callable"] = callable(has_active_job)
            return {"recreated": [], "deferred": [], "skipped": [], "errors": []}

    class FakeReg:
        def list_users(self):
            return [SimpleNamespace(username="a", enabled=True),
                    SimpleNamespace(username="b", enabled=False),
                    SimpleNamespace(username="c", enabled=True)]

    comps = {"spawner": FakeSpawner(), "registry": FakeReg(), "queue": None}
    main.reconcile_worker_images(comps)
    assert captured["users"] == ["a", "c"]      # enabled만 넘긴다
    assert captured["pred_callable"] is True


def test_reconcile_worker_images_noop_without_components():
    assert main.reconcile_worker_images({"spawner": None, "registry": None}) is None


def test_run_worker_starts_loop_thread_serve_false():
    seen = {}

    def fake_loop(cfg, stop_event=None):
        seen["cfg"] = cfg
        seen["stop"] = stop_event

    cfg = _cfg()
    t = main.run_worker(config=cfg, serve=False, worker_loop_fn=fake_loop)
    t.join(timeout=2)
    assert seen["cfg"] is cfg
    assert seen["stop"] is not None      # stop 이벤트 주입
    assert not t.is_alive()


# --- copy_worker_settings (두 번째 spawn 버그 픽스: 파일 바인드 → 부팅 복사) ---------


def test_copy_worker_settings_copies_when_source_present(tmp_path):
    secrets = tmp_path / "secrets"
    src = secrets / "u1" / "claude-settings.json"
    src.parent.mkdir(parents=True)
    src.write_text('{"permissions": {"defaultMode": "bypassPermissions"}}', encoding="utf-8")
    config_dir = tmp_path / "claude"

    env = {
        "SECRETS_DIR": str(secrets),
        "DISPATCH_USER": "u1",
        "CLAUDE_CONFIG_DIR": str(config_dir),
    }
    dest = main.copy_worker_settings(env=env)
    assert dest == str(config_dir / "settings.json")
    # dest 파일이 실제로 만들어졌고 내용이 소스와 동일(멱등 복사).
    assert (config_dir / "settings.json").read_text(encoding="utf-8") == src.read_text(
        encoding="utf-8"
    )


def test_copy_worker_settings_idempotent_overwrites(tmp_path):
    secrets = tmp_path / "secrets"
    src = secrets / "u1" / "claude-settings.json"
    src.parent.mkdir(parents=True)
    src.write_text("V1", encoding="utf-8")
    config_dir = tmp_path / "claude"
    config_dir.mkdir()
    # 이미 낡은 dest가 존재해도 최신 소스로 덮어쓴다.
    (config_dir / "settings.json").write_text("OLD", encoding="utf-8")

    env = {"SECRETS_DIR": str(secrets), "DISPATCH_USER": "u1",
           "CLAUDE_CONFIG_DIR": str(config_dir)}
    # 두 번 호출해도 예외 없이 최신 내용으로 수렴(멱등).
    main.copy_worker_settings(env=env)
    src.write_text("V2", encoding="utf-8")
    dest = main.copy_worker_settings(env=env)
    assert (config_dir / "settings.json").read_text(encoding="utf-8") == "V2"
    assert dest == str(config_dir / "settings.json")


def test_copy_worker_settings_missing_source_warns_no_exception(tmp_path, caplog):
    import logging

    secrets = tmp_path / "secrets"  # 존재하지 않는 소스
    config_dir = tmp_path / "claude"
    env = {"SECRETS_DIR": str(secrets), "DISPATCH_USER": "u1",
           "CLAUDE_CONFIG_DIR": str(config_dir)}
    with caplog.at_level(logging.WARNING, logger="jad.main"):
        dest = main.copy_worker_settings(env=env)
    assert dest is None                       # 복사 없음
    assert not (config_dir / "settings.json").exists()
    assert any("소스 없음" in r.getMessage() for r in caplog.records)


def test_copy_worker_settings_missing_env_warns_no_exception(caplog):
    import logging

    with caplog.at_level(logging.WARNING, logger="jad.main"):
        dest = main.copy_worker_settings(env={})   # SECRETS_DIR/DISPATCH_USER 미설정
    assert dest is None
    assert any("SECRETS_DIR" in r.getMessage() for r in caplog.records)


def test_copy_worker_settings_removes_dest_dir_before_copy(tmp_path):
    """하드닝: dest(settings.json)가 과거 실패 바인드 잔재로 **디렉토리**로 남아
    있으면 copyfile이 IsADirectoryError로 죽는다 → 디렉토리를 제거하고 파일로 복사.
    """
    secrets = tmp_path / "secrets"
    src = secrets / "u1" / "claude-settings.json"
    src.parent.mkdir(parents=True)
    src.write_text('{"ok": true}', encoding="utf-8")
    config_dir = tmp_path / "claude"
    config_dir.mkdir()
    # dest가 디렉토리로 잔존(실패 바인드 잔재 재현) — 안에 파일도 하나 둔다.
    dest = config_dir / "settings.json"
    dest.mkdir()
    (dest / "leftover").write_text("stale", encoding="utf-8")

    env = {"SECRETS_DIR": str(secrets), "DISPATCH_USER": "u1",
           "CLAUDE_CONFIG_DIR": str(config_dir)}
    result = main.copy_worker_settings(env=env)

    assert result == str(dest)
    # 이제 dest는 (디렉토리가 아니라) 파일이고 소스 내용과 동일하다.
    assert dest.is_file()
    assert dest.read_text(encoding="utf-8") == '{"ok": true}'
    # 두 번째 호출도 멱등(이미 파일 → 그대로 덮어쓰기, 예외 없음).
    main.copy_worker_settings(env=env)
    assert dest.is_file()


def test_copy_worker_settings_default_config_dir_and_injection(tmp_path):
    """CLAUDE_CONFIG_DIR 미설정 시 기본 상수 사용 + copyfile/makedirs 주입 검증."""
    secrets = tmp_path / "secrets"
    src = secrets / "u1" / "claude-settings.json"
    src.parent.mkdir(parents=True)
    src.write_text("X", encoding="utf-8")

    made = []
    copied = []
    env = {"SECRETS_DIR": str(secrets), "DISPATCH_USER": "u1"}  # CLAUDE_CONFIG_DIR 없음
    dest = main.copy_worker_settings(
        env=env,
        copyfile=lambda s, d: copied.append((s, d)),
        makedirs=lambda p, exist_ok=False: made.append((p, exist_ok)),
    )
    import os as _os

    expected_dest = _os.path.join(main.DEFAULT_CLAUDE_CONFIG_DIR, "settings.json")
    assert dest == expected_dest
    assert made == [(main.DEFAULT_CLAUDE_CONFIG_DIR, True)]
    assert copied == [(str(src), expected_dest)]


# --- 사이클로그 회신 스레딩(Phase 3a) ---------------------------------------


class _PostResp:
    def __init__(self, body):
        self._body = body
        self.status_code = 200

    def json(self):
        return self._body


class _PostHTTP:
    def __init__(self, body):
        self._body = body
        self.posts = []

    def post(self, url, json=None, headers=None):
        self.posts.append((url, json))
        return _PostResp(self._body)


def test_post_status_core_returns_body_with_cycle_log_path():
    http = _PostHTTP({"ok": True, "dispatched": [], "cycle_log_path": "n/cycles/C1"})
    ok, body = w._post_status_core(http, "http://c", "u1", "T-1", {}, {"status": "완료"})
    assert ok is True
    assert body["cycle_log_path"] == "n/cycles/C1"


def test_stamp_cycle_log_sets_job_field():
    job = {"ticket": "T-1"}
    w._stamp_cycle_log(job, {"cycle_log_path": "n/cycles/C1"})
    assert job["cycle_log_path"] == "n/cycles/C1"


def test_stamp_cycle_log_noop_without_path():
    job = {"ticket": "T-1"}
    w._stamp_cycle_log(job, {"ok": True})     # 경로 없음 → no-op
    assert "cycle_log_path" not in job
    w._stamp_cycle_log(job, None)             # 본문 없음 → no-op
    assert "cycle_log_path" not in job


# --- 프랙탈 P1 신경로 라우팅 + 플래그 OFF byte-for-byte ---------------------


def _cfg_fractal(on):
    """_cfg() 와 동일하되 run.fractal_worker 만 토글. persistent 전제 충족."""
    return SimpleNamespace(
        resume=SimpleNamespace(reset_buffer_sec=0),
        run=SimpleNamespace(orchestrator_repo="/app/orch", claude_bin="claude",
                            output_format="stream-json", input_format="stream-json",
                            persistent_session=True, worker_max_concurrency=1,
                            fractal_worker=on),
        secrets=SimpleNamespace(base_dir=""),
    )


class _FakeSession:
    """UserSession 대역 — run_ticket/resume_ticket/drain 호출을 계수(라우팅 검증)."""

    def __init__(self, result):
        self.result = result
        self.run_calls = 0
        self.resume_calls = 0
        self.drains = 0

    def run_ticket(self, job, creds, config, *, cancel_check=None, **kw):
        self.run_calls += 1
        return self.result

    def resume_ticket(self, job, sid, creds, config, *, cancel_check=None, **kw):
        self.resume_calls += 1
        return self.result

    def drain(self):
        self.drains += 1


def test_flag_off_does_not_route_to_fractal_and_uses_per_ticket_run():
    """플래그 OFF: 신경로(session)를 **절대** 타지 않고 기존 per-ticket run 을 쓴다.

    주입한 fake session 은 건드려지지 않고(run/resume/drain 0회), 주입한 per-ticket run
    스텁이 호출된다 → OFF 는 오늘 동작과 동일(byte-for-byte dispatch/lifecycle)."""
    job = {"ticket": "PROJ-OFF", "autonomy_mode": "A", "branch": "auto/PROJ-OFF"}
    http = FakeHTTP([FakeResp(200, job), FakeResp(204)])
    result = ar.AgentResult(status=ar.STATUS_DONE, session_id="s1", mr_url="http://mr/1")
    sess = _FakeSession(ar.AgentResult(status=ar.STATUS_FAILED))  # 타면 안 됨
    per_ticket_runs = {"n": 0}

    def per_ticket_run(*a, **k):
        per_ticket_runs["n"] += 1
        return result

    n = w.worker_loop(_cfg_fractal(False), env=dict(_ENV), http=http,
                      run=per_ticket_run, resume=lambda *a, **k: result,
                      sleep=lambda s: None, now=_NOW, max_iterations=2, session=sess)
    assert n == 2
    assert _statuses(http) == ["진행중", "완료"]     # 기존과 동일한 dispatch/lifecycle
    assert per_ticket_runs["n"] == 1                 # per-ticket 경로 사용
    assert (sess.run_calls, sess.resume_calls, sess.drains) == (0, 0, 0)  # 신경로 미접촉


def test_flag_on_routes_to_fractal_session_and_drains():
    """플래그 ON: 신경로 session.run_ticket 을 쓰고, 큐가 비면(204) drain 한다."""
    job = {"ticket": "PROJ-ON", "autonomy_mode": "B", "branch": "auto/PROJ-ON"}
    http = FakeHTTP([FakeResp(200, job), FakeResp(204)])
    sess = _FakeSession(ar.AgentResult(status=ar.STATUS_DONE, session_id="s1"))
    per_ticket_runs = {"n": 0}

    n = w.worker_loop(_cfg_fractal(True), env=dict(_ENV), http=http,
                      run=lambda *a, **k: per_ticket_runs.__setitem__("n", per_ticket_runs["n"] + 1),
                      resume=lambda *a, **k: None,
                      sleep=lambda s: None, now=_NOW, max_iterations=2, session=sess)
    assert n == 2
    assert _statuses(http) == ["진행중", "완료"]     # 채널 F 브리지 유지
    assert sess.run_calls == 1                       # 신경로 사용(티켓 주입)
    assert per_ticket_runs["n"] == 0                 # per-ticket run 미사용
    assert sess.drains >= 1                           # 204(큐 빔) + 종료 시 drain


def test_flag_on_reraise_free_drains_on_exit():
    """플래그 ON: 잡이 없어도(즉시 204) 세션은 drain 되어 종료된다(좀비 방지)."""
    http = FakeHTTP([FakeResp(204)])
    sess = _FakeSession(ar.AgentResult(status=ar.STATUS_DONE))
    n = w.worker_loop(_cfg_fractal(True), env=dict(_ENV), http=http,
                      sleep=lambda s: None, now=_NOW, max_iterations=1, session=sess)
    assert n == 1
    assert sess.run_calls == 0
    assert sess.drains >= 1
