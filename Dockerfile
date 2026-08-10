# jira-auto-dispatcher — 컨테이너 이미지
# 자리표시자 골격만. 실제 완성은 Phase 6(배포)에서.
#
# 완성 시 담을 것(Phase 6):
#   - 베이스: python 슬림 + node(claude CLI 설치 전제) 런타임
#   - claude CLI 설치 및 PATH 배선
#   - requirements.txt 설치
#   - 앱 소스 COPY
#   - 볼륨 전제: ~/.claude(세션·auth 영속), state/, workspace/,
#     그리고 런타임 clone 되는 orchestrator/ dlc-meta/ dataspace_docs/
#   - 프로덕션 기동: gunicorn 등 WSGI + 백그라운드 스레드(폴러/워커/스케줄러)
#   - POLICY-ENCODING: 생성 파일 UTF-8(BOM 없음)·LF
#
# TODO(Phase 6): 아래를 실제 지시어로 채운다.
# FROM python:3.12-slim
# WORKDIR /app
# COPY requirements.txt .
# RUN pip install --no-cache-dir -r requirements.txt
# COPY . .
# EXPOSE 5000
# CMD ["python", "-m", "app.main"]
