"""엔트리포인트 — ROLE 분기(central | worker).

역할:
    부팅 시 env `ROLE`을 읽어 두 역할 중 하나로 동작한다.

    - **central** (상시 컨테이너): Flask app factory로 관리 UI/온보딩/디스패치/
      웹훅 라우트를 배선하고, 백그라운드 스레드(poller, scheduler)를 기동한다.
      Jira를 감시해 담당자를 등록 사용자에 매핑하고, 사용자별 worker에 잡을
      배포하며, 온보딩 시 worker 컨테이너를 동적으로 spawn한다.
    - **worker** (사용자별 동적 컨테이너): Flask UI 없이 최소 헬스 엔드포인트만
      두고, worker 폴링 루프(CENTRAL_URL → 잡 → claude 실행 → 상태 회신)를
      기동한다.

구현 Phase:
    - ROLE 분기 골격 + 로그인/인덱스/헬스: **Phase 0**(지금).
    - central 컴포넌트 조립·관리 API·poller/scheduler 기동: Phase 1~5.
    - worker 루프 기동(agent_runner): Phase 5.
    - 프로덕션 기동(gunicorn 등) + 컨테이너: Phase 6.

실행:
    ROLE=central  python -m app.main      # 중앙(관리 UI + 감시/디스패치)
    ROLE=worker   python -m app.main      # 사용자 worker(무 UI, 폴링 루프)

⚠️ 보안: worker는 도구권한 자율 에이전트를 실행하는 RCE 표면이다.
    사내망·신뢰 환경 한정. 외부 노출 금지. 자세한 내용은 README/CLAUDE.md.
"""

from __future__ import annotations

import os

from flask import Flask, jsonify, render_template

from app.auth_login import auth_bp

# central 컴포넌트 싱글턴 핸들(Phase 1~5에서 create_central_app이 채운다).
_components: dict = {}


# =========================================================================
# central 역할
# =========================================================================


def create_central_app(config_path: str = "config/config.yaml") -> Flask:
    """central Flask 앱을 조립해 반환한다(app factory).

    Phase 0: 로그인 블루프린트 + 인덱스/헬스 라우트만 배선한다(동작).
    이후 Phase에서 설정 로드·컴포넌트 생성·온보딩/디스패치/관리 API·웹훅
    배선·백그라운드 기동을 채운다.
    """
    app = Flask(__name__)

    # --- 동작하는 부분(Phase 0): 관리 UI + 브라우저 로그인(폴백 인증) ---
    app.register_blueprint(auth_bp)

    @app.route("/")
    def index():
        """관리 콘솔 렌더(온보딩/사용자/잡 현황)."""
        return render_template("index.html")

    @app.route("/healthz")
    def healthz():
        """헬스체크(도커/오케스트레이션용)."""
        return jsonify({"status": "ok", "role": "central"})

    # --- 이후 Phase 배선 지점 ---
    # TODO(Phase 1): cfg = load_config(config_path)
    # TODO(Phase 2): jira = JiraClient(...)   (watcher 토큰)
    # TODO(Phase 3): registry = Registry(); queue = JobQueue()
    #   dispatcher = Dispatcher(registry, queue); spawner = Spawner(cfg, registry)
    #   gate = DedupGate()
    # TODO(Phase 3): 온보딩/관리 API 라우트 등록
    #   POST /onboard                 사용자 등록(폼) → registry.upsert + spawner.start
    #   GET  /api/users               사용자 목록(enabled/모드/컨테이너 상태)
    #   POST /api/users/<u>/enabled   자동 트리거 토글
    #   POST /api/users/<u>/container start|stop (spawner)
    #   GET  /api/jobs                전 사용자 잡 현황
    #   POST /api/resume              수동 재개(scheduler.resume_now)
    # TODO(Phase 3): dispatch_bp 등록(GET /dispatch/<u>/next, POST .../<job>/status)
    # TODO(Phase 4): webhook_bp를 cfg.webhook.path에 등록(enabled일 때)
    # TODO(Phase 4~5): start_central_background(cfg) 로 poller/scheduler 기동

    return app


def start_central_background() -> None:
    """central 백그라운드 — poller 스레드 + resume 스케줄러 기동.

    TODO(Phase 4~5): Poller.run_forever를 데몬 스레드로,
    ResumeScheduler.start()를 호출. _components에 핸들 보관.
    (worker는 별도 컨테이너이므로 여기서 워커 스레드를 띄우지 않는다)
    """
    raise NotImplementedError("TODO(Phase 4~5): central 백그라운드 기동")


# =========================================================================
# worker 역할
# =========================================================================


def create_worker_app() -> Flask:
    """worker 최소 Flask 앱(헬스 엔드포인트 전용, UI 없음).

    TODO(Phase 5~6): 헬스만 노출. 실제 작업은 worker 루프 스레드가 담당.
    (컨테이너 헬스체크/오케스트레이션 프로브용)
    """
    app = Flask(__name__)

    @app.route("/healthz")
    def healthz():
        """헬스체크 — DISPATCH_USER 표기."""
        return jsonify({"status": "ok", "role": "worker", "user": os.environ.get("DISPATCH_USER")})

    return app


def run_worker() -> None:
    """worker 진입 — 설정/러너 조립 후 폴링 루프 기동.

    TODO(Phase 5): cfg = load_config(); runner = AgentRunner(cfg);
    Worker(cfg, runner).run_forever(). (헬스 앱은 선택적으로 별도 스레드)
    """
    raise NotImplementedError("TODO(Phase 5): worker 루프 기동")


# =========================================================================
# 진입점
# =========================================================================


def main() -> None:
    """개발용 진입점 — ROLE로 분기.

    central: create_central_app → (Phase 4~5) start_central_background → 내장 서버.
    worker : (Phase 5) run_worker (+ 선택적 헬스 앱).
    TODO(Phase 6): 프로덕션 central은 gunicorn 등 WSGI로 대체.
    """
    role = os.environ.get("ROLE", "central").strip().lower()

    if role == "worker":
        # Phase 5에서 run_worker()로 대체. 지금은 헬스 앱만 서빙(동작).
        app = create_worker_app()
        app.run(host="0.0.0.0", port=8787)
        return

    # central(기본)
    app = create_central_app()
    # 사내망 한정. 외부 노출 금지(README 보안 항목 참조).
    app.run(host="0.0.0.0", port=8787)


if __name__ == "__main__":
    main()
