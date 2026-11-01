> Historical B-only evidence. Current A+B scope and verification are documented in [INTEGRATION_VALIDATION.md](INTEGRATION_VALIDATION.md). Counts and A integration limitations below describe the earlier B branch.

# B 구현 검증 기록

2026-11-01 로컬 Windows / Python 3.12. Git branch `feature/b-content-pipeline-20261101`.

## 로컬

- `python -m pytest -q --junitxml=test-results/local.xml`: **416 passed, 1 skipped**, 110.37초. skip은 로컬 MySQL 서버 URL 부재를 명시한 테스트 1개입니다.
- `python -m ruff check app tests scripts`, `python -m ruff format --check app tests scripts`: 통과.
- `git diff --check`: 통과. tracked 파일에 Anthropic/GitHub 토큰·private key 패턴 검출 없음.
- TestClient의 httpx deprecation warning 1개가 있습니다. 테스트 동작은 통과하며 현재 lock 버전을 기록했습니다.

## 실행한 경로

- 실제 loopback HTTP → SQL queue → worker → DB → owner GET. 별도 worker 프로세스도 같은 job/vault를 읽어 완료했습니다.
- 자료 private upload, TXT/PDF/image 검증·격리 추출, grounded 분석, draft→immutable confirmed version, 문제 생성·정답 비노출, 별도 자습 설명 생성·이력.
- A 임시 gateway를 통한 owner/단원/고정 버전/solve 유형/활성 revision/잔여 목표 검증 및 기존 답안 읽기. B의 task 완료량 쓰기는 0회. 과거 task 고정 버전도 worker 저장까지 검증했습니다.
- 동시 멱등 요청, expiry/replay/conflict, 동시 worker claim, 활성 resource/fingerprint 제약, lease 전후 crash, 늦은 worker, 동시 restart, retry 최대3회.
- 키 TTL/암호문 변조/삭제, 호출 전 만료, provider auth/rate limit와 불명확 timeout 구분, 취소 후 늦은 결과 차단.
- 타인 resource·source 접근 차단, signed token 목적/자료/만료, 서명 cursor 사용자/filter 범위, output 스키마/근거 핀/oversize 거부, 원자 저장.
- 파서 20초 timeout, 텍스트/이미지/PDF 상한, 추출 동시 slot 제한. provider 고정 목적지, 생성 300초 / 키 검증 15초 total timeout, 2MiB response bound, 토큰 cap.
- 공개 B 계약의 **24개 operation**이 실제 등록 route와 대응하며 A/결제/Teach-back route는 추가하지 않았습니다.
- 결과 불명 작업의 successor가 queued/running/succeeded/failed/outcome_unknown인 모든 경우, 연쇄 restart 후 성공한 경우에도 원계약의 추가 과금 동의 조건을 검사했습니다. 활성 job은 `JOB_IN_PROGRESS`를 우선 반환합니다.
- 앞뒤 공백을 포함한 원문 길이가 한도를 넘는 생성 결과는 `failed/INVALID_OUTPUT`이며 부분 결과를 저장하지 않습니다. DB 오류로 결과 불명 상태가 되는 회귀를 검사했습니다.
- UTF-8/UTF-16 키 파일 형식·모호한 wrapper 거절·비밀 없는 오류, ledger 생성/쓰기 실패의 FD·잠금 정리, live 실행 실패의 DB·메모리 정리를 합성 fixture로 검사했습니다. vault는 GC에 기대지 않고 모든 연결을 닫으며 Windows 임시 디렉터리를 즉시 정리할 수 있습니다.
- JWT 식별자는 DB collation과 별개로 정확히 비교합니다. 멱등 claim의 MySQL 1213만 콜백 진입 전에 최대 3회 재시도하며, 콜백·commit·불확실한 네트워크 오류는 재실행하지 않는 회귀를 포함합니다.

## MySQL CI

PR #1의 SHA `b3406a16a4fab06bf100f172c3fe56b0fa05dabc`에서 [MySQL 8.0 migration 001~004와 HTTP E2E가 통과](https://github.com/1st-line-tone-team-4/Backend/actions/runs/41226085460)한 것을 확인했습니다. 후속 수정의 CI는 추가로 PAD SPACE JWT 별칭, 실제 1213 deadlock 강제 경합과 단일 콜백, 오래된 RR snapshot 중 다른 요청이 결과 불명으로 완료되는 HTTP 경합을 실행합니다. 이 검사는 로컬 SQLite 통과로 대체하지 않습니다. 테스트 DB의 A 참조는 최소 fixture이며 실제 팀 A migration/운영 DB 검증으로 확대 해석하지 않습니다.

후속 변경의 최종 확인은 해당 commit의 동일 workflow 결과를 따릅니다. PR #1의 원 커밋은 저자와 출처를 보존해 기존 feature 브랜치로 cherry-pick했으며, 공개 이력 rewrite나 운영 배포는 하지 않았습니다.

## 외부 AI와 비밀 처리

OpenAI/Anthropic 운영 어댑터를 mock transport로 검사했습니다. 실제 계정·모델의 생성 호출은 **0회**입니다. 이전 live 시험은 입력 단계에서 중단됐으며, 이번 수정·검수는 실제 키 파일을 읽지 않고 합성 키만 사용했습니다. 비용 예약과 알려진 과금은 0입니다. Anthropic 인증은 공식 지원되는 `x-api-key`를 사용합니다. 인증 설명과 현재 공식 출처는 [N-series 검수 기록](REVIEW-N-FIXES.md)에 연결했습니다.

합성 live harness와 한 개의 중앙 예산 ledger는 준비되어 있습니다. 실제 키를 memory-only credential adapter로 처리하며 DB/vault/Git/로그에 저장하지 않습니다. Haiku5.5의 보수적인 호출당 예약 $0.51024 × 최대8회 = $4.08192, 총 승인상한 $10입니다. 모든 실행이 같은 ledger를 사용해야 하고 결과불명은 중단합니다.

OCR 의미 정확성, 실제 모델 품질/지원 capability, 실제 팀 A 통합, 운영 용량·지속장애 시 삭제 SLA·분산 secret store는 아직 검증하지 않았습니다.

## 추가 검토 문서와 정책 대조

Notion의 [수정 요청](https://app.notion.com/p/40a5df12680381fd9497d29c0b025ea6)과 [작업 파악 내역](https://app.notion.com/p/40a5df1268038104b8b4fff16e37c0d7)을 읽어 R1~R4 및 유지 항목을 대조했습니다. 성공한 successor만으로 이전 호출의 과금 여부가 확정되지는 않습니다. 따라서 R1의 성공 후 경고 해제 제안은 채택하지 않고, canonical OpenAPI 1.3의 기존 outcome_unknown 작업 존재 시 추가 동의 요구를 유지합니다. A 공유 Job 추가 열 4개와 활성 resource 유일 제약은 migration 문서에 계속 기록합니다. 대규모 도메인 재작성은 수행하지 않았습니다.

## N1·N3–N9 추가 검수

[항목별 변경·공식 근거·계약 제약](REVIEW-N-FIXES.md)을 기록했습니다. Astra 독립 검수에서 COMMIT 응답 유실 시 자격증명 삭제, SQLite savepoint 이후 외부 롤백, Anthropic 8,000픽셀 경계 누락을 추가로 재현해 수정했습니다. 최종 코드에 대해 동일 재현이 해소됐음을 별도 확인했습니다.

실제 HTTP 검사에는 키 검증 중 claim 행 쓰기, 분석 준비 중 Job 폴링과 자료 행 쓰기, 자료 삭제 후 결과 불명 감사 이력 보존을 추가했습니다. MySQL CI는 새/만료 멱등 claim 경쟁 및 최초 complete와 삭제의 gap-lock 경합도 검사합니다. N10 개념 확정 PATCH 멱등 계약은 사용자의 통합 작업으로 제외했습니다. Notion 상태는 수정하지 않았습니다.
