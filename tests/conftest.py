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
                project="HAN", repo_map=None, branch_prefix="auto/"):
    """스케줄러/폴러가 참조하는 최소 config 유사 객체."""
    return SimpleNamespace(
        role="central",
        jira=SimpleNamespace(base_url="https://x", project=project,
                             poll_interval_sec=60, watcher_token_file="", watcher_email=""),
        match=SimpleNamespace(statuses=statuses or ["해야 할 일"]),
        git=SimpleNamespace(branch_prefix=branch_prefix, github_owner="me"),
        run=SimpleNamespace(global_concurrency=global_concurrency,
                            concurrency_per_worker=concurrency_per_worker,
                            dlc_meta_repo="/app/dlc-meta", dataspace_docs_repo="/app/ds"),
        repo_map=repo_map or {},
        webhook=SimpleNamespace(enabled=False, path="/jira-webhook", shared_secret_file=""),
        secrets=SimpleNamespace(base_dir=""),
        worker_shared_secret="",
    )


@pytest.fixture()
def config():
    return make_config()
