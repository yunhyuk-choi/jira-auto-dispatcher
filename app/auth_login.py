"""브라우저 로그인 2-스텝 — claude-hacker에서 이식(동작 유지).

역할:
    터미널 없이 웹에서 `claude auth login`을 완료한다. 개발서버 컨테이너에서
    최초 세션 인증/재인증 시 사용한다(`~/.claude` 볼륨에 영속).

이식 출처: C:/workspace/claude-hacker/app.py 의 /start-login·/complete-login.
이 모듈은 스텁이 아니라 **동작하는** 이식본이다.

흐름:
    POST /start-login    : `claude auth login`을 Popen으로 열고 stdout에서
                           인증 URL을 추출해 반환(프로세스는 코드 입력 대기 유지).
    POST /complete-login : 프론트가 보낸 `code#state`를 로그인 프로세스 stdin에
                           써서 인증 완료(EOF까지 읽어 성공/실패 판정).

⚠️ 함정(claude-hacker CLAUDE.md 계승):
    - 인코딩: Windows(cp949)에서 UTF-8 출력 디코딩 실패 방지 위해 Popen은
      반드시 text=True, encoding='utf-8', errors='replace'.
    - 커맨드명: 로그인은 `claude auth login`(‘claude login’은 없음).
    - 코드 붙여넣기 방식: 자동 콜백이 아니라 `code#state`를 stdin에 주입.
      CLI가 TTY 전용으로 코드를 읽으면 pty(pywinpty) 우회가 필요할 수 있다.

TODO(Phase 6 배포 시 검토): 컨테이너에서 stdin 파이프 주입 동작 재검증.
"""

from __future__ import annotations

import re
import subprocess

from flask import Blueprint, jsonify, request

auth_bp = Blueprint("auth", __name__)

# ANSI 색상 코드(터미널 특수문자) 제거용 정규식
ansi_escape = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")

# 로그인 프로세스는 2-스텝에 걸쳐 살아있어야 하므로 모듈 전역으로 보관
_login_process = None


@auth_bp.route("/start-login", methods=["POST"])
def start_login():
    """claude 로그인 프로세스 시작 및 인증 URL 추출."""
    global _login_process

    # 기존 로그인 프로세스가 있으면 강제 종료
    if _login_process:
        _login_process.kill()

    try:
        _login_process = subprocess.Popen(
            ["claude", "auth", "login"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )

        auth_url = None

        # 출력 스트림에서 https:// 로 시작하는 인증 링크 찾기
        for line in iter(_login_process.stdout.readline, ""):
            url_match = re.search(r"(https://[^\s]+)", line)
            if url_match:
                auth_url = url_match.group(1)
                break  # URL을 찾았으므로 읽기 중단(프로세스는 콜백 대기로 유지)

        if auth_url:
            return jsonify({"status": "success", "url": auth_url})
        return jsonify({"status": "error", "message": "로그인 URL을 찾을 수 없습니다."})

    except Exception as e:  # noqa: BLE001 - 사용자에게 에러 메시지로 전달
        return jsonify({"status": "error", "message": str(e)})


@auth_bp.route("/complete-login", methods=["POST"])
def complete_login():
    """프론트에서 받은 인증 코드(code#state)를 로그인 프로세스 stdin에 써서 완료."""
    global _login_process
    data = request.json or {}
    code = (data.get("code") or "").strip()

    if not code:
        return jsonify({"status": "error", "message": "인증 코드가 비어 있습니다."})

    if not _login_process or _login_process.poll() is not None:
        return jsonify(
            {
                "status": "error",
                "message": "대기 중인 로그인 프로세스가 없습니다. 1단계부터 다시 시작하세요.",
            }
        )

    try:
        # 코드 붙여넣기를 기다리는 CLI의 stdin에 코드를 써줌
        _login_process.stdin.write(code + "\n")
        _login_process.stdin.flush()

        # 코드 교환이 끝나면 CLI가 남은 출력을 뱉고 종료 → EOF까지 읽어 판정
        output_lines = []
        for line in iter(_login_process.stdout.readline, ""):
            clean_line = ansi_escape.sub("", line).strip()
            if clean_line:
                output_lines.append(clean_line)

        _login_process.wait(timeout=15)
        rc = _login_process.returncode
        result_text = "\n".join(output_lines)
        _login_process = None

        if rc == 0:
            return jsonify(
                {"status": "success", "message": "로그인 완료!", "detail": result_text[-500:]}
            )
        return jsonify(
            {"status": "error", "message": f"로그인 실패 (exit {rc}): {result_text[-300:]}"}
        )
    except Exception as e:  # noqa: BLE001
        return jsonify({"status": "error", "message": str(e)})
