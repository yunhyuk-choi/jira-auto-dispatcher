"""pytest 공용 픽스처 — 상태 격리 + 경량 config/유저 팩토리.

라이브 Jira/네트워크는 절대 호출하지 않는다(상위가 별도 검증). 모든 테스트는
임시 state 디렉토리에서 결정적으로 돈다.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app import state


@pytest.fixture()
def isolated_state(tmp_path):
    """state.* 를 임시 디렉토리로 격리(테스트 간 오염 방지)."""
    prev = state.get_state_dir()
    state.set_state_dir(str(tmp_path / "state"))
    state.ensure_state_dir()
    yield tmp_path
    state.set_state_dir(prev)


def make_config(global_concurrency=3, concurrency_per_worker=1, statuses=None,
                project="PROJ", repo_map=None, branch_prefix="auto/",
                min_free_mem_mb=1536, per_job_mem_reserve_mb=1024, max_load_per_core=0.9,
                worker_max_concurrency=64, cancel_statuses=None, optout_labels=None):
    """스케줄러/폴러가 참조하는 최소 config 유사 객체.

    ⚠️ ``global_concurrency``/``concurrency_per_worker`` 는 잡 수 cap이 제거되면서
    스케줄러가 더 이상 참조하지 않는다(하위 호출부 호환을 위해 인자만 남긴 no-op).
    dispatch 스로틀은 이제 ``admission`` 의 서버 자원 기반 어드미션이다.
    """
    return SimpleNamespace(
        role="central",
        jira=SimpleNamespace(base_url="https://x", project=project,
                             poll_interval_sec=60, watcher_token_file="", watcher_email=""),
        match=SimpleNamespace(statuses=statuses or ["해야 할 일"],
                              cancel_statuses=cancel_statuses or ["취소됨"],
                              optout_labels=optout_labels or ["자동화_추적_해제"]),
        git=SimpleNamespace(branch_prefix=branch_prefix, github_owner="me"),
        admission=SimpleNamespace(min_free_mem_mb=min_free_mem_mb,
                                  per_job_mem_reserve_mb=per_job_mem_reserve_mb,
                                  max_load_per_core=max_load_per_core),
        run=SimpleNamespace(worker_max_concurrency=worker_max_concurrency,
                            dlc_meta_repo="/app/dlc-meta", docs_repo="/app/docs"),
        repo_map=repo_map or {},
        webhook=SimpleNamespace(enabled=False, secret_ref="service/jira-webhook"),
        secrets=SimpleNamespace(base_dir=""),
    )


@pytest.fixture()
def config():
    return make_config()
