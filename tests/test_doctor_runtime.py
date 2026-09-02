"""central 부팅 자가진단(app/doctor_runtime.py) 단위테스트.

이 기능이 지켜야 하는 약속은 넷이다:
    1. **기동을 막지 않는다** — 진단이 실패해도, 심지어 진단 자체가 터져도 central 은 뜬다
       (설정을 고칠 관리 UI 가 같이 안 뜨면 자충수).
    2. **부팅을 늦추지 않는다** — 백그라운드에서 돌고 결과는 캐시된다. 조회는 재실행하지 않는다.
    3. **치명적 FAIL 은 온보딩을 막는다** — 다만 "모르면 막지 않는다"(진단 전에는 통과).
    4. **시크릿을 흘리지 않는다** — 관리 UI 에는 인증이 없다.

⚠️ 실제 검사(:func:`app.setup_doctor.run_checks`)는 부르지 않는다 — ``run_checks`` 대역만
쓴다(CI: ubuntu, 네트워크·docker 없음).
"""

from __future__ import annotations

import threading
import time

import pytest

from app import doctor_runtime as DR
from app import setup_doctor as D


def _results(**status_by_name):
    return [D.CheckResult(name, status, f"{name} 메시지", f"{name} 힌트")
            for name, status in status_by_name.items()]


def _runtime(results=None, *, calls=None, boom=False):
    """대역 검사기를 물린 :class:`DoctorRuntime`."""
    def run_checks(cfg, **kwargs):
        if calls is not None:
            calls.append(kwargs)
        if boom:
            raise RuntimeError("진단 폭발")
        return list(results or [])

    return DR.DoctorRuntime(object(), config_path="config/config.yaml",
                            project_dir=".", run_checks=run_checks)


# ---------------------------------------------------------------------------
# 캐시 · 상태
# ---------------------------------------------------------------------------


def test_snapshot_before_first_run_is_pending_and_blocks_nothing():
    rt = _runtime()
    snap = rt.snapshot()
    assert snap["state"] == DR.STATE_PENDING
    assert snap["ok"] is None and snap["checks"] == []
    # ⚠️ "모르면 막지 않는다" — 부팅 직후 관리 UI 가 이유 없이 잠기면 안 된다.
    assert snap["onboarding_blocked"] is False
    assert rt.blocking_failures() == []


def test_run_once_caches_results_and_snapshot_does_not_rerun():
    calls = []
    rt = _runtime(_results(config=D.STATUS_PASS), calls=calls)
    rt.run_once()
    for _ in range(5):
        snap = rt.snapshot()
    assert len(calls) == 1              # 조회는 재실행하지 않는다(폴링이 진단 폭주가 되면 안 됨)
    assert snap["state"] == DR.STATE_READY
    assert snap["ok"] is True
    assert snap["duration_sec"] is not None and snap["age_sec"] is not None


def test_run_checks_gets_the_config_path_and_project_dir():
    calls = []
    _runtime(_results(config=D.STATUS_PASS), calls=calls).run_once()
    assert calls[0]["config_path"] == "config/config.yaml"
    assert calls[0]["project_dir"] == "."


def test_start_runs_in_the_background_and_does_not_block():
    rt = _runtime(_results(config=D.STATUS_PASS))
    assert rt.start() is True
    for _ in range(200):               # 데몬 스레드가 끝나기를 잠깐 기다린다
        if rt.snapshot()["state"] == DR.STATE_READY:
            break
        time.sleep(0.01)
    assert rt.snapshot()["state"] == DR.STATE_READY


def test_a_crashing_check_run_never_propagates():
    """진단 사고가 central 을 죽이면 안 된다 — 결과가 없을 뿐이다."""
    rt = _runtime(boom=True)
    assert rt.run_once() == []
    snap = rt.snapshot()
    assert snap["state"] == DR.STATE_PENDING
    assert "RuntimeError" in snap["error"]
    assert rt.blocking_failures() == []


def test_concurrent_runs_are_collapsed_into_one():
    started = threading.Event()
    release = threading.Event()
    calls = []

    def run_checks(cfg, **kwargs):
        calls.append(kwargs)
        started.set()
        release.wait(2)
        return _results(config=D.STATUS_PASS)

    rt = DR.DoctorRuntime(object(), run_checks=run_checks)
    rt.start()
    assert started.wait(2)
    assert rt.start() is False          # 이미 도는 중 — 겹쳐 쌓지 않는다
    assert rt.snapshot()["running"] is True
    release.set()
    for _ in range(200):
        if not rt.running:
            break
        time.sleep(0.01)
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# 온보딩 차단 기준
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", DR.BLOCKING_CHECKS)
def test_each_blocking_check_blocks_onboarding(name):
    rt = _runtime(_results(**{name: D.STATUS_FAIL}))
    rt.run_once()
    assert rt.blocking_failures() == [name]
    assert rt.snapshot()["onboarding_blocked"] is True


@pytest.mark.parametrize("name", ["forge_token", "dlc_meta", "notifier", "worker_secret"])
def test_degrading_failures_do_not_block_onboarding(name):
    """central 자신의 git·알림 경로가 깨져도 잡은 돌고 MR/PR 은 나온다 — 막지 않는다."""
    rt = _runtime(_results(**{name: D.STATUS_FAIL}))
    rt.run_once()
    assert rt.blocking_failures() == []
    assert rt.snapshot()["onboarding_blocked"] is False


def test_warnings_and_skips_never_block():
    rt = _runtime(_results(config=D.STATUS_WARN, docker=D.STATUS_SKIP,
                           secrets=D.STATUS_WARN))
    rt.run_once()
    assert rt.blocking_failures() == []


def test_blocking_failures_are_reported_in_declaration_order():
    rt = _runtime(_results(docker=D.STATUS_FAIL, config=D.STATUS_FAIL,
                           forge_token=D.STATUS_FAIL))
    rt.run_once()
    assert rt.blocking_failures() == ["config", "docker"]   # forge_token 은 막지 않는다


def test_blocking_checks_are_all_real_check_names():
    """오타 방지 — 목록에 실재하지 않는 검사 이름이 들어가면 영원히 발동하지 않는다."""
    assert set(DR.BLOCKING_CHECKS) <= set(D.CHECK_ORDER)


# ---------------------------------------------------------------------------
# 운영 중 실측 반영(폴러 → note_jira_auth)
# ---------------------------------------------------------------------------


def test_runtime_auth_failure_overrides_a_passing_boot_check():
    """부팅 때는 자격이 멀쩡했어도, 폴링 중 만료되면 그 사실이 진단에 뜬다."""
    rt = _runtime(_results(config=D.STATUS_PASS, jira_auth=D.STATUS_PASS))
    rt.run_once()
    assert rt.blocking_failures() == []

    rt.note_jira_auth(False, "HTTP 401")
    snap = rt.snapshot()
    auth = next(c for c in snap["checks"] if c["name"] == "jira_auth")
    assert auth["status"] == D.STATUS_FAIL
    assert "401" in auth["message"]
    # jira_auth 는 BLOCKING_CHECKS 라 온보딩이 막힌다(조용히 노는 워커를 늘리지 않는다).
    assert snap["onboarding_blocked"] is True
    assert rt.blocking_failures() == ["jira_auth"]


def test_runtime_auth_recovery_lifts_the_block():
    rt = _runtime(_results(jira_auth=D.STATUS_PASS))
    rt.run_once()
    rt.note_jira_auth(False, "HTTP 401")
    assert rt.blocking_failures() == ["jira_auth"]
    rt.note_jira_auth(True, "/myself 확인")
    assert rt.blocking_failures() == []


def test_a_fresh_diagnosis_supersedes_an_older_runtime_note():
    """설정을 고치고 '다시 진단' 했으면 낡은 런타임 관측이 그 결과를 가리면 안 된다."""
    rt = _runtime(_results(jira_auth=D.STATUS_PASS))
    rt.note_jira_auth(False, "HTTP 401")
    time.sleep(0.01)
    rt.run_once()                       # 더 나중에 끝난 회차가 이긴다
    assert rt.blocking_failures() == []


def test_runtime_note_survives_before_any_diagnosis_ran():
    """아직 한 회차도 안 돌았어도 폴러가 아는 사실은 버리지 않는다."""
    rt = _runtime()
    rt.note_jira_auth(False, "HTTP 401")
    snap = rt.snapshot()
    assert [c["name"] for c in snap["checks"]] == ["jira_auth"]
    assert snap["onboarding_blocked"] is True


def test_runtime_note_does_not_carry_a_token():
    """진단 응답은 인증 없이 읽힌다 — detail 에 온 문자열이 그대로 실려도 값은 없다."""
    rt = _runtime()
    rt.note_jira_auth(False, "HTTP 401")
    body = str(rt.snapshot())
    assert "401" in body and "glpat-" not in body


def test_a_blown_up_diagnosis_does_not_discard_the_runtime_note():
    """터진 회차는 새 사실을 못 가져왔다 — 폴러가 아는 실패를 밀어내면 안 된다."""
    rt = _runtime(boom=True)
    rt.note_jira_auth(False, "HTTP 401")
    time.sleep(0.01)
    rt.run_once()                       # 예외로 끝난다(결과 없음)
    assert rt.snapshot()["error"]
    assert rt.blocking_failures() == ["jira_auth"]
