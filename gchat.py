#!/usr/bin/env python3
"""gchat.py — **하위호환 별칭**. 실체는 :mod:`notify_report`.

이 도구는 Google Chat 전용이던 시절 ``gchat.py`` 라는 이름을 가졌다. 알림이
provider 어댑터(none|google_chat|slack|generic_webhook)로 일반화되면서 실체는
``notify_report.py`` 로 옮겼지만, **옛 호출 경로를 깨지 않는다**:

    python /app/gchat.py --ticket HAN-1 --report-file <path>   # 계속 동작(구 프롬프트·문서)
    python /app/notify_report.py --ticket HAN-1 ...            # 신규 경로(권장)

구현: 임포트되면 이 모듈 객체 자리에 ``notify_report`` 를 그대로 꽂는다
(``sys.modules`` 별칭) — ``import gchat`` 은 같은 모듈 객체를 돌려주므로 함수·상수·
monkeypatch 대상이 하나로 유지된다(별칭이 원본과 갈라지지 않는다). 스크립트로 직접
실행되면(``__main__``) 별칭 없이 ``notify_report.main()`` 을 그대로 위임한다.

⚠️ 신규 코드·문서·프롬프트는 ``notify_report.py`` 를 쓴다. 이 파일은 기존 배포·구
프롬프트가 남아 있는 동안의 얇은 shim 이며 자체 로직을 갖지 않는다.

POLICY-ENCODING: UTF-8(BOM 없음)·LF.
"""

from __future__ import annotations

import sys

import notify_report

if __name__ == "__main__":  # pragma: no cover — 얇은 CLI 진입(옛 경로)
    raise SystemExit(notify_report.main())

# 임포트 경로: 이 모듈 이름을 실체 모듈에 바인딩한다(별칭 — 상태 공유).
sys.modules[__name__] = notify_report
