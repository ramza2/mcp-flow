# MCPFlow – 내부 MCP Everything 테스트 서버 운영 (선택 사항)

## 목적과 범위

이 구성은 공식 `@modelcontextprotocol/server-everything`의 **MCP 2026-07-28 Streamable HTTP** 연결을 검증하기 위한 기반입니다. 기능 구현과 한국어 UI 정비가 완료되기 전까지 실사용 업무 자동화나 종합 성능 측정에 사용하지 않습니다.

- 공식 서버: https://github.com/modelcontextprotocol/servers/tree/main/src/everything
- npm 패키지: `@modelcontextprotocol/server-everything@2.1.0` (버전 고정)
- Node.js: 24 (전용 Docker 이미지에서 실행)
- 테스트용 내부 주소: `http://everything-mcp-test:3001/mcp`
- 운영 서비스 외부 공개: **없음** (호스트 포트, Traefik 라벨 없음)
- 인증: `NONE` (외부에 공개하지 않은 **격리된 시험용 서버에 한정**)
- 네트워크: `mcpflow_mcp_test` (Docker `internal: true`)
- 새 컨테이너에는 DB/모델/서비스 인증정보나 호스트 볼륨을 제공하지 않음.

## 사전 확인

운영 서버 `~/mcp-flow`에서 실행합니다. `main` 병합 및 CI 완료 후 사용하십시오. 현재 배포에서는 백엔드 API와 Worker 이미지 태그가 서로 다르므로 **전체 `deploy.sh` / `docker compose up`을 실행하면 안 됩니다.** 이 절차는 API·Worker 이미지를 빌드하거나 재시작하지 않습니다.

```bash
cd ~/mcp-flow
git status --short
docker compose --version
docker ps --filter label=com.docker.compose.project=mcpflow \
  --format 'table {{.Names}}\t{{.Status}}'
```

## 설치 / 중지

```bash
cd ~/mcp-flow
bash scripts/ops/mcp-everything-test.sh start
bash scripts/ops/mcp-everything-test.sh status
# 필요한 경우에만 중지
bash scripts/ops/mcp-everything-test.sh stop
```

`start`는 선택적인 `everything-mcp-test` 컨테이너만 빌드·시작하고, 이미 실행 중인 MCPFlow API 및 Worker를 테스트망에 **재시작 없이** 연결합니다. 두 컨테이너에서 TCP 연결까지 검증합니다. 배포한 뒤 API·Worker가 다른 이유로 재생성되면 `start`를 다시 실행해 테스트망 연결을 복구하십시오.

`stop`은 테스트 서버만 중지하며 API·Worker의 테스트망 연결은 남겨 둡니다. 기존 운영 컨테이너와 볼륨을 삭제하거나 정리하지 않습니다.

## UI 등록 순서 (종합 테스트 전 기반 준비)

1. **MCP 서버 → 서버 등록**에서 이름을 `Everything – 내부 프로토콜 검증`으로 입력합니다.
2. 전송 방식 `STREAMABLE_HTTP`, URL `http://everything-mcp-test:3001/mcp`, 인증 `NONE`을 지정하고 **DRAFT**로 저장합니다.
3. **연결 테스트**로 Current MCP 협상 및 Tool 목록 접근을 확인합니다.
4. **Tool Discovery**를 **미적용 미리보기**(`apply_changes=false`)로 실행하고 차이 및 스키마를 검토합니다.
5. 검토 후에만 Discovery 적용을 수행합니다. 실제 Tool 활성화·Resource Grant·Tool Policy는 별도 관리자 승인 대상으로 유지합니다. **등록·발견만으로 실행 권한을 부여하지 않습니다.**
6. 초기에는 `echo` 같은 무해한 읽기성 Tool만 선택하고, 실행 부작용·장시간 작업·환경변수 조회 등의 Tool을 한꺼번에 활성화하지 않습니다.

Everything 서버는 실제 업무 서비스가 아닌 테스트 도구입니다. 프로토콜 검증이 끝나면 `stop`으로 중지합니다. 실제 외부 MCP 서버의 Bearer/API Key/OAuth 인증과 사용권한은 별도 검증 흐름에서 확인해야 하며, `NONE`을 일반 서버의 기본 승인 정책으로 해석하지 마십시오.

## 보안·운영 주의

- `internal` 네트워크는 외부 송수신을 제한하지만, **같은 테스트망에 연결된 API·Worker와의 통신까지 차단하는 것은 아닙니다.** 완전히 신뢰할 수 없는 서버 코드는 별도 VM/호스트에 배치하십시오.
- 서버는 **이미지 빌드 시** npm에서 설치됩니다. 배포 전 패키지 출처·버전·CI를 확인하고, 버전 업그레이드는 별도 PR로 관리합니다.
- 테스트용 `get-env` Tool이 존재할 수 있으므로, 테스트 컨테이너에는 비밀 환경변수를 전달하지 않습니다.
- 마이그레이션, DB 데이터 수정, 운영 Tool 권한 자동 할당, 성능 지표 기록은 이 설치 절차에 포함하지 않습니다.
- 기본 `compose.yaml` 및 `compose.server.yaml`에 새 서비스를 자동으로 추가하지 않아 평소의 운영 배포에는 영향을 주지 않습니다.
