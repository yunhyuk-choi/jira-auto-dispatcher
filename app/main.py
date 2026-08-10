"""엔트리포인트 — Flask app factory + 라우트 배선 + 백그라운드 스레드 기동.

역할:
    앱의 조립 지점. 설정을 로드하고, 관리 UI/로그인/웹훅 라우트를 배선하고,
    폴러·워커 백그라운드 스레드와 재개 스케줄러를 기동한다.

구현 Phase:
    - 로그인 라우트 + 인덱스 렌더: **Phase 0**(지금, auth_login 이식으로 동작).
    - 관리 API(현황/모드스위치/재개): Phase 3~5에서 각 컴포넌트와 함께 배선.
    - 폴러/워커/스케줄러 기동: Phase 4~5.
    - 프로덕션 기동(gunicorn 등): Phase 6(도커).

실행:
    python -m app.main            # 개발용(플라스크 내장 서버)

⚠️ 보안: 이 앱은 도구권한 자율 에이전트를 실행하는 RCE 표면이다.
    사내망·신뢰 환경 한정. 외부 노출 금지. 자세한 내용은 README/CLAUDE.md.
"""

from __future__ import annotations

from flask import Flask, jsonify, render_template

from app.auth_login import auth_bp

# 컴포넌트 싱글턴 핸들(Phase 3~5에서 create_app이 채운다).
_components: dict = {}


def create_app(config_path: str = "config/config.yaml") -> Flask:
    """Flask 앱을 조립해 반환한다(app factory).

    Phase 0: 로그인 블루프린트 + 인덱스 라우트만 배선한다(동작).
    이후 Phase에서 설정 로드·컴포넌트 생성·관리 API·백그라운드 기동을 채운다.
    """
    app = Flask(__name__)

    # --- 동작하는 부분(Phase 0): 관리 UI + 브라우저 로그인 ---
    app.register_blueprint(auth_bp)

    @app.route("/")
    def index():
        """관리 UI 렌더."""
        return render_template("index.html")

    @app.route("/healthz")
    def healthz():
        """헬스체크(도커/오케스트레이션용)."""
        return jsonify({"status": "ok"})

    # --- 이후 Phase 배선 지점 ---
    # TODO(Phase 1): cfg = load_config(config_path)
    # TODO(Phase 2): jira = JiraClient(...)
    # TODO(Phase 3): gate = DedupGate(); queue = JobQueue()
    # TODO(Phase 3~5): 관리 API 라우트 등록
    #   GET  /api/status        현황(잡 목록/워터마크/모드)
    #   POST /api/mode          자율 모드 A/B 스위치
    #   POST /api/resume        수동 재개(스케줄러 resume_now)
    # TODO(Phase 4): webhook_bp를 cfg.webhook.path에 등록(enabled일 때)
    # TODO(Phase 4~5): start_background(cfg) 로 폴러/워커/스케줄러 스레드 기동

    return app


def start_background() -> None:
    """폴러·워커 스레드와 재개 스케줄러를 기동한다.

    TODO(Phase 4~5): Poller.run_forever / Worker.run_forever를 데몬 스레드로,
    ResumeScheduler.start()를 호출. _components에 핸들 보관.
    """
    raise NotImplementedError("TODO(Phase 4~5): 백그라운드 기동")


def main() -> None:
    """개발용 진입점 — 앱 생성 후 내장 서버로 서빙.

    TODO(Phase 4~5): create_app 후 start_background 호출.
    TODO(Phase 6): 프로덕션은 gunicorn 등 WSGI 서버로 대체.
    """
    app = create_app()
    # 사내망 한정. 외부 노출 금지(README 보안 항목 참조).
    app.run(host="0.0.0.0", port=5000)


if __name__ == "__main__":
    main()
