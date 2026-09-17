# MCPFlow Server Deployment Runbook

이 문서는 기존 **Traefik Docker provider + label 기반 Reverse Proxy**가 운영 중인 Linux 서버에 MCPFlow를 배포하는 절차를 정의한다.

서버 배포는 `compose.yaml` + `compose.server.yaml` 조합을 사용한다. 저장소에 포함된 embedded/file-provider Traefik은 서버 배포에서 기동하지 않는다.

## 1. 사전 조건

서버에는 다음이 준비되어 있어야 한다.

- Docker Engine
- Docker Compose v2 (`docker compose`)
- Python 3 (최초 server secret 자동 생성 시 사용)
- 기존 Traefik 컨테이너
- Traefik Docker provider 활성화
- Traefik이 연결된 external Docker network
- 사용할 HTTPS entrypoint와 certificate resolver
- 서비스 도메인의 DNS가 해당 서버를 가리키도록 설정

MCPFlow의 PostgreSQL, Redis, Object Storage, worker, outbox는 외부 Traefik network에 연결하지 않는다. 외부 Traefik network에는 `frontend`와 `api`만 연결한다.

## 2. 배포 파일

```text
compose.yaml             공통 서비스/네트워크/볼륨 계약
compose.local.yaml       로컬 개발용 hot reload/진단 포트
compose.server.yaml      기존 외부 Traefik 연계 서버 배포 override
scripts/deploy.sh        서버 배포 진입점
.env.server              서버 비민감 배포 설정, 자동 생성, Git 제외
infra/secrets/server/    서버용 secret 파일, 자동 생성, Git 제외
```

서버에서 `compose.local.yaml`을 함께 사용하지 않는다.

## 3. 최초 배포

PR/branch 또는 배포 대상 revision을 checkout한 뒤 저장소 루트에서 실행한다.

```bash
./scripts/deploy.sh
```

최초 실행 시 다음 값을 입력한다.

| 항목 | 필수 여부 | 기본값 |
|---|---|---|
| Service domain | 필수 | 없음 |
| Traefik Docker network | 선택 | `traefik` |
| Traefik HTTPS entrypoint | 선택 | `websecure` |
| TLS certificate resolver | 선택 | `letsencrypt` |
| Deployment environment | 선택 | `pilot` |
| Log level | 선택 | `INFO` |
| API docs enabled | 선택 | `false` |
| Image tag | 선택 | 현재 Git short SHA |
| Execution lease seconds | 선택 | `60` |

선택 항목은 Enter 입력 시 기본값을 사용한다.

예시:

```text
MCPFlow server deployment configuration

Service domain (required, e.g. mcpflow.example.com): mcpflow.example.com
Traefik Docker network [traefik]:
Traefik HTTPS entrypoint [websecure]:
TLS certificate resolver [letsencrypt]:
Deployment environment [pilot]:
Log level [INFO]:
API docs enabled (true/false) [false]:
Image tag [a1b2c3d]:
Execution lease seconds [60]:
```

설정은 `.env.server`에 저장되고 secret 값은 저장하지 않는다.

## 4. Secret

최초 배포 시 `infra/secrets/server/`가 비어 있으면 `infra/scripts/generate_local_secrets.py`를 사용해 다음 파일을 자동 생성한다.

```text
postgres_admin_password
postgres_migration_password
postgres_app_password
minio_root_user
minio_root_password
```

기존 파일이 있으면 덮어쓰지 않는다. Secret 값은 배포 로그에 출력하지 않는다.

`.env.server`와 `infra/secrets/server/`는 Git 대상이 아니다.

## 5. 배포 순서

`./scripts/deploy.sh`는 다음 순서를 수행한다.

```text
Docker / Docker Compose 확인
→ external Traefik network 확인
→ server 설정/secret 확인
→ docker compose config 검증
→ backend/frontend image build
→ PostgreSQL / Redis / Object Storage 기동
→ dependency health 확인
→ Alembic migration one-shot
→ migration exit code 0 확인
→ API / Worker / Outbox / Frontend 기동
→ runtime health 확인
→ https://<domain>/health/ready smoke test
```

Migration이 실패하면 API/worker/outbox/frontend rollout을 진행하지 않는다.

## 6. Traefik Routing

`compose.server.yaml`이 Docker label로 다음 routing을 등록한다.

```text
https://<host>/api/v1
https://<host>/api/v1/*
https://<host>/health
https://<host>/health/*
    → api:8000

https://<host>/*
    → frontend:8080
```

API/health router priority는 100, frontend catch-all priority는 1이다.

Path boundary는 `/api/v1` 및 `/api/v1/`, `/health` 및 `/health/`만 매칭하도록 정의한다. `/api/v10` 같은 경로가 API router에 잘못 매칭되지 않아야 한다.

TLS 종료와 인증서 발급은 기존 서버 Traefik이 담당한다. `MCPFLOW_TRAEFIK_ENTRYPOINT`와 `MCPFLOW_TRAEFIK_CERTRESOLVER` 값은 서버 Traefik 설정과 일치해야 한다.

## 7. 재배포 및 운영 명령

기존 설정을 사용한 재배포:

```bash
./scripts/deploy.sh
```

설정을 다시 입력하고 재배포:

```bash
./scripts/deploy.sh --reconfigure
```

상태 확인:

```bash
./scripts/deploy.sh --status
```

주요 서비스 로그:

```bash
./scripts/deploy.sh --logs
```

API/worker/outbox/frontend 재시작 후 health/smoke 확인:

```bash
./scripts/deploy.sh --restart
```

서비스 중지:

```bash
./scripts/deploy.sh --down
```

`--down`은 named volume을 삭제하지 않는다. PostgreSQL/Redis/Object Storage volume 삭제는 일반 배포 절차에 포함하지 않는다.

## 8. 배포 후 확인

최소 확인 항목:

```text
migration     Exited (0)
postgres      healthy
redis         healthy
object-storage healthy
api           healthy
frontend      healthy
worker        running
outbox        running
```

HTTPS 확인:

```bash
curl -fsS https://<domain>/health/live
curl -fsS https://<domain>/health/ready
```

Traefik에서 API router와 frontend router가 같은 host에 등록되고, API router가 더 높은 priority를 갖는지 확인한다.

## 9. 보안/운영 원칙

- 서버 배포에서 MCPFlow 자체 Traefik 컨테이너를 기동하지 않는다.
- PostgreSQL, Redis, Object Storage admin port를 host에 publish하지 않는다.
- `frontend`와 `api`만 external Traefik network에 연결한다.
- Secret을 `.env.server`, Git, Docker image layer에 저장하지 않는다.
- deployment 전에 DB/Object Storage의 복구 지점을 운영 정책에 따라 확인한다.
- destructive migration 또는 volume 삭제는 `deploy.sh`에서 자동 수행하지 않는다.
- 운영 장애 시 migration/runtime 로그와 기존 Traefik 로그를 함께 확인한다.

## 10. 수동 Compose 진단

배포 스크립트가 사용하는 Compose 조합은 다음과 같다.

```bash
docker compose \
  --env-file .env.server \
  -f compose.yaml \
  -f compose.server.yaml \
  config
```

서버에서 별도 진단이 필요한 경우 이 조합을 기준으로 한다.
