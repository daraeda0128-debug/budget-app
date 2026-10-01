# Budget V2 — 준비 단계

기존 루트 index.html, main, Firebase는 보존한다. V2에는 세션 로그인 화면, 월별 수입·지출·고정 현금지출 요약, 카테고리별 소비, 거래 검색/담당자 필터, 거래 추가/수정/삭제, CSV 미리보기 및 멱등 일괄 가져오기가 구현되어 있다. 모든 거래 입력은 FastAPI와 PostgreSQL에 저장된다. 급여 자동반영, 이월 계산, 고정지출 편집, 시뮬레이션 및 상세 현금흐름은 다음 단계이며 기존 서비스와 동시 운영 중이다.

## 실제 맥미니 점검 (2026-10-01 KST)

- arm64 / macOS 26.6.2 / RAM 32GiB / 여유 디스크 약 211GiB.
- Docker Desktop 설치, CLI 29.4.1 / Compose 5.1.3. SSH PATH에 /usr/local/bin이 빠짐. 최초 확인 시 엔진 중지 상태. Docker Desktop 시작 후 재검증.
- 기존 7 Days 서버 26900, 8081; ControlCenter 5000/7000; NoMachine 7003 등. 관측된 TCP 목록에서 80/443은 비어 있음. UDP/관리자 전용 프로세스 및 공유기 포워딩은 별도 확인 필요.
- DNS 레코드 화면에서 wallet A 레코드가 추가됐고, 권한 네임서버 및 일반 조회 모두 `wallet.picknote.store → 59.17.107.163`을 반환함. AAAA 없음. 맥미니 공인 IPv4와 일치.
- Ollama 두 프로세스: IPv6 *:11434와 IPv4 127.0.0.1:11434. 기존 프로세스를 임의 중단하지 않는다. 전체 인터페이스 리스너 원인을 확인하고 호스트 방화벽과 IPv6 접근을 검증하기 전 LLM 기능은 활성화하지 않는다.

Docker Desktop 시작 후 기존 carsystem-backend(3000 공개), carsystem-postgres(5432 공개)가 확인됨. 기존 서비스 변경 없음. V2는 별도 budget-v2 프로젝트 및 볼륨, DB/API host ports 없이 기동 검증 완료. .env는 wallet.picknote.store로 설정 예정이며 무작위 DB 암호는 저장소에 포함하지 않음. DNS A 레코드가 전파되어 조회됨. 공유기 포트 전달 및 공인 HTTPS는 미확인.

검증 결과: 이전 단위 테스트 5개 통과; ARM64 API 이미지 빌드; DB healthcheck; 실제 세션 로그인/보호 API/Origin 거부/CSRF 거부/로그아웃 세션 무효화; 실제 SQL 이전 건수·합계 대조 및 동일 batch 재실행 모두 통과. 샘플 및 테스트 계정 정리 후 users/transactions 0건 확인. Caddy validate 통과 (공인 인증서 발급은 미검증). API 컨테이너에서 host.docker.internal:11434/api/version 접속 성공, Ollama 0.33.3. 호스트/LAN 방화벽 범위는 아직 미검증.

## 경계와 실행

인터넷 → Caddy(80 리디렉션/443 HTTPS) → FastAPI(컨테이너 8000) → PostgreSQL(내부망 5432). DB/API는 host ports 없음. Caddy 관리 API 비활성화. 기존 앱의 PIN과 블러는 서버 인증이 아니므로 V2 사용자 암호로 이전하지 않는다.

```sh
cd v2
cp .env.example .env
# wallet.picknote.store, PUBLIC_ORIGIN 및 무작위 DB 암호 설정
/usr/local/bin/docker --context desktop-linux compose config --quiet
/usr/local/bin/docker --context desktop-linux compose up -d --build
/usr/local/bin/docker --context desktop-linux compose exec api python create_user.py jinsu
```

처음 첨부된 화면은 DNS 호스트(글루)였지만, 후속 DNS 레코드 화면에서 실제 A 레코드를 추가했다. 권한 네임서버 조회 결과 `wallet.picknote.store A 59.17.107.163` 확인. AAAA는 없음. 공유기에서 80/443만 맥미니 LAN 주소로 전달하고 5432/8000/11434는 전달하지 않는다. IPv6 AAAA는 추가하지 않았다. 공인 인증서는 A 레코드 전파와 외부 80/443 연결 확인 후 발급한다. 집 공인 IP가 바뀌면 A 레코드도 갱신한다. IPv6는 별도로 방화벽 확인. Docker Desktop 자동시작과 맥미니 절전/재부팅 후 복구를 검증한다.

서버 세션은 DB의 해시 토큰, Secure/HttpOnly/SameSite 쿠키, 12시간 만료, 로그인 시 교체, 로그아웃 시 폐기, Origin+CSRF 검증. Argon2 암호 해시와 계정별 지수 지연 적용. 공개 전 추가 IP 단위 제한/감사 기록/암호 변경 및 세션 청소를 구현한다. passkeys 테이블은 향후 WebAuthn용이며 등록/인증 challenge와 RP ID 검증은 미구현이다.

Ollama는 macOS 네이티브 유지. API의 OLLAMA_URL은 host.docker.internal을 사용하지만 호출 기능은 아직 없다. loopback 접속 가능 여부는 Docker Desktop 환경에서 실제 확인하고, 실패한다고 0.0.0.0 바인딩으로 바꾸지 않는다. 제한된 호스트 브리지/방화벽 구성을 검증한다.

## 기존 기능과 데이터 모델

| 기존 저장/동작 | V2 반영 |
|---|---|
| tx/YYYY-MM/Firebase key; date,name,cat,amt/amount,type,who,payMethod,memo,ts | 거래 원본 JSON, 안정된 source ID, 정수 KRW, 거래일과 원본 월, 진수/미나/공동. 결제수단 누락은 unknown 유지 |
| fixed/YYYY-MM + fixedItems_YYYY-MM, 미래 12개월 복사, 기간/출금일/순서 | fixed_snapshots 월별 원본 보존. 추후 recurring_rules로 명시적 전환, 월별 예외 유지 |
| salaryJ/M, autoSalary; 급여일 10/17 | 설정 보존. rule_occurrences의 rule/month 유일 키로 향후 중복 방지. 기존 자동반영은 캐시만 생성되므로 실제 수입과 예상 수입 분리 |
| carryAnchors, carryover, carryoverBaseYm | 앵커+설정 보존. 이전/이후 앵커 정방향/역방향 누적 계산을 회귀 검증 |
| 카드 소비/고정 카드 익월 반영, 카드대금 | 발생일 소비와 실제 현금 이동 별도 집계. 카드사/결제일/청구서 모델은 후속; 실제 카드대금과 추정 청구 중복 차감 금지 |
| 대납/정산 | 거래 카테고리 원본 유지, settlements로 부분정산 연결. 생활 소비 합계에서 제외하되 현금 흐름 반영 |
| 부업 진수/미나 수입-지출 상계 | 담당자별 순액 별도 집계. 소비패턴에서 제외하되 현금 순액 반영 |
| simEvents 단발 ym / 매년 months,startYear,endYear | simulation_events 원본 보존; 실제 원장과 예상 이벤트 분리 |
| CSV / 자동카테고리 | 후속 미리보기·열 매핑·csv 표준 파서·파일 해시+행별 ID. 같은 금액/가맹점의 정상 중복은 삭제하지 않음 |

기존 코드 검토: readCSV는 단순 쉼표 분리로 인용된 쉼표 처리 불가. doImport는 배열을 넘기지만 postToSheet는 data.date가 있을 때만 저장하므로 일괄저장 누락 가능. 거래 수정은 원본 삭제 후 저장해 실패 시 손실 가능. V2는 DB 트랜잭션 기반 수정으로 교체한다. 기존 getCardUsage는 대납/정산/카드대금/부업을 제외하며 정확한 현금흐름 재현은 getMonthNet/시뮬레이션과 함께 후속 테스트한다.

## 이전: 원본 유지, 충돌 차단, 검증 후 전환

1. Firebase Console에서 루트 JSON export. 각 사용 기기에서 기존 사이트 localStorage도 JSON으로 export. 민감 파일은 private/에 보관, Git 제외. PIN은 새 암호로 이전하지 않음.
2. 원본 두 종류의 SHA256 기록, 암호화 백업. localStorage 차이가 있으면 자동 우선순위를 적용하지 않음.
3. `python backend/migrate.py private/firebase.json --local-export private/local.json`은 읽기 전용 검토 요약/hash만 출력. 누락 payMethod와 월별/담당자/방향/결제수단/카테고리 합계를 검토. export가 빈 데이터라도 실제 원본과 대조.
4. 별도 빈 V2 DB에서 API 컨테이너로 private 파일을 임시 복사 후 동일 명령에 `--apply-reviewed-sha256 <검토한 hash>` 추가. 충돌이면 거절. 원자적 적용 및 건수/분류별 합계 대조 실패 시 rollback. 동일 batch 재실행은 무변경; 다른 batch나 운영 데이터 덮어쓰기 거절.
5. 고정 월 사본, 급여 설정, 앵커, 시뮬레이션 값을 원본과 대조. 설정 부재 시 기존 하드코딩 기본값을 실제 기록으로 자동 생성하지 않음. 멀티기기 미동기화 내역을 따로 확인.
6. UI/계산 완성 뒤 Firebase 입력을 잠시 멈추고 최종 export/새 빈 DB 이전, 잔액·카드청구·부분정산 비교. 승인된 전환 시점에만 새 도메인으로 이용 시작. Firebase 삭제 없음.

백업: `docker compose exec -T db pg_dump -U budget -d budget -Fc > private/budget.dump` (POSIX 셸). 배포 전 별도 DB에 pg_restore 복구시험. rollback은 이전 사이트로 복귀하되 V2 신규 거래를 먼저 export/대조하고 양쪽 동시쓰기 금지. `compose down`은 볼륨 유지; `down -v` 사용 금지. 001.sql은 첫 볼륨 생성에만 실행되므로 후속 스키마 변경은 버전 migration 필요.

초기 사용자 계정은 맥미니 터미널에서 `cd ~/Developer/budget-app-v2/v2 && /usr/local/bin/docker --context desktop-linux compose exec api python create_user.py jinsu`를 실행해 직접 설정한다. 현재 데이터베이스에는 사용자가 아직 없다. 비밀번호는 화면 입력으로만 전달되며 채팅에 보낼 필요가 없다.\r\n\r\n공식 참고: https://docs.docker.com/desktop/features/networking/networking-how-tos/ , https://docs.ollama.com/faq
