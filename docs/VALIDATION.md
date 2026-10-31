# B 구현 검증 기록

2026-11-01 로컬 Windows / Python 3.12. Git branch `feature/b-content-pipeline-20261101`.

## 로컬

- `python -m pytest -q --junitxml=test-results/local.xml`: **110 passed, 1 skipped**, 39.01초. skip은 로컬 MySQL 서버 URL 부재를 명시한 테스트 1개입니다.
- `python -m ruff check app tests scripts`, `python -m ruff format --check app tests scripts`: 통과.
- `git diff --check`: 통과. tracked 파일에 Anthropic/GitHub 토큰·private key 패턴 검출 없음.
- TestClient의 httpx deprecation warning 1개가 있습니다. 테스트 동작은 통과하며 현재 lock 버전을 기록했습니다.

## 실행한 경로

- 실제 loopback HTTP → SQL queue → worker → DB → owner GET. 별도 worker 프로세스도 같은 job/vault를 읽어 완료했습니다.
- 자료 private upload, TXT/PDF/image 검증·격리 추출, grounded 분석, draft→immutable confirmed version, 문제 생성·정답 비노출, 별도 자습 설명 생성·이력.
- A 임시 gateway를 통한 task/버전 검증 및 기존 답안 읽기. B의 task 완료량 쓰기는 0회.
- 동시 멱등 요청, expiry/replay/conflict, 동시 worker claim, 활성 resource/fingerprint 제약, lease 전후 crash, 늦은 worker, 동시 restart, retry 최대3회.
- 키 TTL/암호문 변조/삭제, 호출 전 만료, provider auth/rate limit와 불명확 timeout 구분, 취소 후 늦은 결과 차단.
- 타인 resource·source 접근 차단, signed token 목적/자료/만료, 서명 cursor 사용자/filter 범위, output 스키마/근거 핀/oversize 거부, 원자 저장.
- 파서 20초 timeout, 텍스트/이미지/PDF 상한, 추출 동시 slot 제한. provider 고정 목적지, 60초 total timeout, 2MiB response bound, 토큰 cap.
- 공개 B 계약의 **24개 operation**이 실제 등록 route와 대응하며 A/결제/Teach-back route는 추가하지 않았습니다.

## MySQL CI

첫 통합 SHA `259675314c0cf2bb7e4d7120e4206d4907451578`의 [GitHub Actions 실행](https://github.com/1st-line-tone-team-4/Backend/actions/runs/41216673636)이 성공했습니다. Linux 회귀와 새 MySQL 8.0 service에서 001/002/003 migration 및 실제 HTTP 자료→문제→설명 흐름을 모두 실행했습니다. 테스트 DB의 A 참조는 최소 fixture이며 실제 팀 A migration/운영 DB 검증으로 확대 해석하지 않습니다.

후속 변경의 최종 확인은 해당 commit의 동일 workflow 결과를 따릅니다. 실제 배포·main merge·PR 생성은 하지 않았습니다.

## 외부 AI와 비밀 처리

OpenAI/Anthropic 운영 어댑터를 mock transport로 검사했습니다. 실제 계정·모델의 생성 호출은 **0회**입니다. 승인된 live Claude 테스트는 지정된 자격증명이 직접 Anthropic API 키 형식으로 확인되지 않아 외부 요청 전 중단되었습니다. 비용 예약 0건, 알려진 과금 0이며 제공업체 확인 전 다른 API에 임의 전송하지 않습니다.

합성 live harness와 한 개의 중앙 예산 ledger는 준비되어 있습니다. 실제 키를 memory-only credential adapter로 처리하며 DB/vault/Git/로그에 저장하지 않습니다. Haiku5.5의 보수적인 호출당 예약 $0.51024 × 최대8회 = $4.08192, 총 승인상한 $10입니다. 모든 실행이 같은 ledger를 사용해야 하고 결과불명은 중단합니다.

OCR 의미 정확성, 실제 모델 품질/지원 capability, 실제 팀 A 통합, 운영 용량·지속장애 시 삭제 SLA·분산 secret store는 아직 검증하지 않았습니다.
