"""잡 활동시각(updated_at) — 매 쓰기경로 스탬프·영속·단조성 단위테스트.

관리 콘솔이 "최근 활동 먼저" 정렬과 KST 갱신시각 컬럼을 위해 쓰는 필드다.
스탬프는 **순수 파이썬 코드**(set_status/update/enqueue/reopen/reassign)로만 남고,
오케스트레이터/claude 개입이 없다. 값은 ISO-8601 UTC 문자열로 영속되며 구 레코드
(필드 없음)는 None 으로 관대하게 로드된다. 라이브 호출 없음.
"""

from __future__ import annotations

from datetime import datetime

from app import queue as q
from app.queue import Job, JobQueue


def _enq(qq: JobQueue, ticket: str = "PROJ-1", **kw) -> Job:
    job = Job(ticket=ticket, user=kw.pop("user", "alice"), **kw)
    qq.enqueue(job)
    return qq.get(ticket)


def _parse(s: str) -> datetime:
    return datetime.fromisoformat(s)


# --- 스탬프: 매 쓰기경로 -----------------------------------------------------


def test_enqueue_stamps_updated_at(isolated_state):
    qq = JobQueue()
    j = _enq(qq)
    assert j.updated_at is not None
    # ISO-8601 UTC(파싱 가능·오프셋 존재).
    dt = _parse(j.updated_at)
    assert dt.utcoffset() is not None
    assert dt.utcoffset().total_seconds() == 0


def test_set_status_stamps_updated_at(isolated_state):
    qq = JobQueue()
    j = _enq(qq)
    before = j.updated_at
    qq.set_status("PROJ-1", q.RUNNING)
    after = qq.get("PROJ-1").updated_at
    assert after is not None
    assert after >= before  # ISO UTC 문자열 = 사전식 시각순


def test_update_refreshes_updated_at(isolated_state):
    qq = JobQueue()
    j = _enq(qq)
    before = j.updated_at
    qq.update("PROJ-1", log_summary="진행 30%")
    after = qq.get("PROJ-1").updated_at
    assert after is not None
    assert after >= before


def test_reopen_and_reassign_stamp(isolated_state):
    qq = JobQueue()
    _enq(qq)
    qq.set_status("PROJ-1", q.CANCELLED)
    prev = qq.get("PROJ-1").updated_at
    qq.reopen(qq.get("PROJ-1"))
    assert qq.get("PROJ-1").updated_at >= prev
    prev2 = qq.get("PROJ-1").updated_at
    qq.reassign(qq.get("PROJ-1"), "bob")
    assert qq.get("PROJ-1").updated_at >= prev2


# --- 영속: to_dict/from_dict 라운드트립 + 구 레코드 관대 로드 ----------------


def test_updated_at_roundtrips_through_dict(isolated_state):
    qq = JobQueue()
    j = _enq(qq)
    d = j.to_dict()
    assert "updated_at" in d
    assert Job.from_dict(d).updated_at == j.updated_at


def test_from_dict_tolerates_missing_updated_at():
    # 구 jobs.json 레코드(필드 없음) → None 으로 로드(에러 없음).
    legacy = {"ticket": "OLD-9", "user": "carol", "status": "done"}
    job = Job.from_dict(legacy)
    assert job.updated_at is None
    # 라운드트립도 안전.
    assert Job.from_dict(job.to_dict()).updated_at is None


def test_persisted_updated_at_survives_reload(isolated_state):
    qq = JobQueue()
    j = _enq(qq)
    stamp = j.updated_at
    # 새 큐 인스턴스로 재로드(state jobs.json 경유).
    qq2 = JobQueue()
    assert qq2.get("PROJ-1").updated_at == stamp


# --- 단조성: 연속 전이의 updated_at 은 비감소 -------------------------------


def test_updated_at_monotonic_non_decreasing(isolated_state):
    qq = JobQueue()
    _enq(qq)
    stamps = [qq.get("PROJ-1").updated_at]
    for st in (q.QUEUED, q.RUNNING, q.DONE):
        qq.set_status("PROJ-1", st)
        stamps.append(qq.get("PROJ-1").updated_at)
    for earlier, later in zip(stamps, stamps[1:]):
        assert later >= earlier
