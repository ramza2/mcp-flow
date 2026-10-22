# ModelOps Gateway X-AI-Client 적용 준비 — MCPFlow

**적용 범위:** MCPFlow Backend OpenAI-compatible 공통 모델 클라이언트

**현상:** MCPFlow의 ModelOps 서비스 키는 아직 발급되지 않았습니다. MODELOPS_CLIENT_KEY를 비워두면 기존 요청에는 새 헤더가 붙지 않습니다. **현재 실운영 키 등록·호출 검증은 하지 않았습니다.**

## 준비된 동작

- app/model_provider/openai_compatible.py의 공통 HTTP 헤더 생성기가 환경변수의 실제 키를 읽어 X-AI-Client 헤더에 전달합니다.
- HTTPS https://modelops-gateway.openlink.kr 의 표준 443포트 요청에만 적용합니다. 임의 외부 OpenAI-compatible 서버에 프로젝트 키를 전달하지 않습니다.
- LLM JSON 생성, Embedding 생성, LLM 연결 점검, Embedding 연결 점검의 네 호출 경로에 동일한 헤더를 적용합니다.
- 값이 없거나 비어 있으면 기존 요청을 유지하며 X-AI-Client는 생략합니다.
- 헤더 값은 CR/LF 주입 방지를 위해 영문/숫자로 시작하는 1~128자리 A-Z a-z 0-9 . _ -만 허용합니다. 잘못된 형식은 HTTP 요청 전에 거부합니다.
- X-AI-Client는 **시스템 식별자**이며 Authorization 인증키와 별개입니다. Bearer Credential Secret 로직은 변경하지 않습니다.
- Base URL, Alias qwen3-14b/qwen25-vl/bge-m3, JSON/Streaming/Timeout/Embedding 차원은 변경하지 않습니다.
- Compose Backend 환경변수로 선택적 설정을 전달합니다. deploy.sh --reconfigure를 실행해도 기존 등록값을 유지합니다.

## 서비스 키 발급 후 적용

1. 사내 ModelOps 관리자 http://192.168.10.104:18081/clients 에서 AI 사용 시스템 → 신규 시스템 등록을 진행합니다.
2. 예: 서비스 키 mcpflow, 표시 이름 MCPFlow, 설명 MCP 연계 업무 자동화 AI 에이전트. **mcpflow는 예시일 뿐 실제 발급 키가 아닙니다.** 정확히 등록된 키를 사용합니다.
3. 서버 ~/mcp-flow/.env.server 에 MODELOPS_CLIENT_KEY=<발급한 값> 을 입력합니다. 실제 값은 Git에 올리지 않습니다.
4. 서버에서 ./scripts/deploy.sh deploy 실행으로 백엔드 컨테이너를 재생성하고 환경변수를 반영합니다. docker compose restart만으로는 새로운 컨테이너 환경변수가 적용되지 않습니다.
5. 운영의 기존 Backend 기능으로 최소 호출 검증 후 ModelOps 관리자 화면에서 해당 서비스 ID로 집계되는지 확인합니다.

키가 발급되기 전에는 **키 값 입력, 백엔드 재배포, 실제 ModelOps 요청이 모두 불필요**합니다.

## 네트워크 없는 회귀 테스트

- 미설정 시 헤더 생략
- LLM/Embedding/연결검사 4종에 정확한 식별 헤더
- 외부·위장 호스트·HTTP·비표준 포트에 헤더 미전송
- 잘못된 키는 요청 전에 차단
- 요청 Body의 모델명, JSON 파라미터, 응답 계약 불변

**CI 성공은 실키 발급이나 운영 등록/추적 성공의 증명이 아닙니다.**
