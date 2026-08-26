"""main.py 역할 배선 단위테스트 — worker 역할 상주 계약 + central reconcile 헬퍼.

⚠️ 이 파일은 삭제된 ``tests/test_worker.py`` 에서 **살아남은 동작을 덮던 테스트**를
옮겨 온 것이다. 그 파일이 검증하던 레거시 워커 폴링 루프(``app/worker.py::worker_loop``)
는 프랙탈 경로와 **이중 실행**(같은 티켓 두 번 → 중복 브랜치·변경요청·완료알림)을
일으켜 모듈째 제거됐지만, 아래 셋은 그 루프와 무관하게 계속 살아 있다:

    1. ``copy_worker_settings`` — 헤드리스 자율 실행용 사전 인가 settings 복사
    2. ``run_worker`` — 워커 컨테이너 PID1 의 상주 계약(주입 materialize → 설정 로드 →
       settings 복사 → /healthz 서빙). **폴링 루프는 더 이상 없다.**
    3. ``_has_active_job_predicate`` / ``reconcile_worker_images`` — central 부팅 시
       stale 워커 이미지 조정(작업 C)

라이브 docker/네트워크는 호출하지 않는다(전부 주입 대역).
"""

from __future__ import annotations

import importlib.util
import logging
import os
import threading
from types import SimpleNamespace

from app import main


# ===========================================================================
# worker 역할 — 상주 계약
# ===========================================================================


def test_legacy_worker_polling_module_is_gone():
    """⚠️ 회귀 방지(이중 실행): 레거시 폴링 소비자 ``app/worker.py`` 는 되살아나면 안 된다.

    이 모듈이 다시 생기면 워커가 중앙에서 잡을 **당겨오는** 두 번째 실행 엔진이 되고,
    센트럴 세션이 ``docker exec`` 로 **밀어 넣는** 경로와 같은 티켓을 두 번 실행한다
    (실측: 한 티켓에 변경요청 2개 + Jira 코멘트 중복). 기계적으로 못 박는다.
    """
    assert importlib.util.find_spec("app.worker") is None


def test_worker_app_serves_healthz(monkeypatch):
    """워커 컨테이너의 **유일한 서빙 표면** — Dockerfile HEALTHCHECK 가 이걸 프로브한다.

    이게 죽으면 컨테이너가 unhealthy 로 떨어져 ``docker exec`` 주입 대상에서 밀려난다
    (= 그 사용자 티켓이 조용히 실행되지 않는다).
    """
    monkeypatch.setenv("DISPATCH_USER", "u1")
    client = main.create_worker_app().test_client()
    res = client.get("/healthz")
    assert res.status_code == 200
    assert res.get_json() == {"status": "ok", "role": "worker", "user": "u1"}


def test_run_worker_materializes_injection_before_loading_config(monkeypatch):
    """⚠️ 순서 계약: 주입 materialize 가 **설정 로드보다 먼저** 돈다.

    bind 마운트를 없앤 구조라 config.yaml·per-user 시크릿은 컨테이너 env 로만 들어온다
    (:mod:`app.inject`). materialize 가 나중에 돌면 load_config 가 없는 파일을 읽는다.
    """
    from app import config as config_mod
    from app import inject

    order: list = []
    monkeypatch.setattr(inject, "materialize", lambda **kw: order.append("materialize"))
    monkeypatch.setattr(config_mod, "load_config",
                        lambda path: order.append("load_config") or SimpleNamespace())
    monkeypatch.setattr(main, "copy_worker_settings", lambda: order.append("copy_settings"))

    assert main.run_worker(config_path="config/config.yaml", serve=False) is None
    assert order == ["materialize", "load_config", "copy_settings"]


def test_run_worker_starts_no_background_polling_thread(monkeypatch):
    """워커는 **아무 루프도 돌리지 않는다** — 실행은 밀어 넣어진다(docker exec).

    스레드가 하나라도 뜨면 그것이 곧 두 번째 실행 엔진 후보다(이중 실행의 형태).
    """
    monkeypatch.setattr(main, "copy_worker_settings", lambda: None)
    before = {id(t) for t in threading.enumerate()}
    assert main.run_worker(config=SimpleNamespace(), serve=False) is None
    new_threads = [t for t in threading.enumerate() if id(t) not in before]
    assert new_threads == []
    assert "_worker" not in main._components      # 루프 핸들도 남기지 않는다


def test_run_worker_survives_settings_copy_failure(monkeypatch, caplog):
    """사전 인가 복사가 터져도 상주는 계속된다 — 헤드리스 생존 우선(경고만)."""
    def boom():
        raise OSError("복사 실패")

    monkeypatch.setattr(main, "copy_worker_settings", boom)
    with caplog.at_level(logging.ERROR, logger="jad.main"):
        assert main.run_worker(config=SimpleNamespace(), serve=False) is None
    assert any("settings 복사" in r.getMessage() for r in caplog.records)


# ===========================================================================
# copy_worker_settings (두 번째 spawn 버그 픽스: 파일 바인드 → 부팅 복사)
# ===========================================================================


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

    made: list = []
    copied: list = []
    env = {"SECRETS_DIR": str(secrets), "DISPATCH_USER": "u1"}  # CLAUDE_CONFIG_DIR 없음
    dest = main.copy_worker_settings(
        env=env,
        copyfile=lambda s, d: copied.append((s, d)),
        makedirs=lambda p, exist_ok=False: made.append((p, exist_ok)),
    )

    expected_dest = os.path.join(main.DEFAULT_CLAUDE_CONFIG_DIR, "settings.json")
    assert dest == expected_dest
    assert made == [(main.DEFAULT_CLAUDE_CONFIG_DIR, True)]
    assert copied == [(str(src), expected_dest)]


# ===========================================================================
# central — 배포 시 워커 이미지 reconcile(작업 C)
# ===========================================================================


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
    captured: dict = {}

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
