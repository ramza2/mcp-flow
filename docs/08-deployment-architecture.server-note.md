# Server deployment variant note

`docs/08-deployment-architecture.md`의 기본 배포 원칙을 유지하면서, 기존 공용 Traefik이 이미 운영되는 서버에서는 MCPFlow 자체 Traefik을 중복 기동하지 않는다.

서버 배포의 구체적인 운영 절차와 외부 Traefik Docker-label 계약은 [`server-deployment-runbook.md`](./server-deployment-runbook.md)를 따른다.

적용 관계:

```text
compose.yaml
  + compose.server.yaml
  + .env.server
  + infra/secrets/server/*
  -> existing external Traefik
```

이 variant에서도 다음 `docs/08` 원칙은 동일하게 유지한다.

- PostgreSQL이 상태 원본이다.
- migration은 runtime rollout 전에 one-shot으로 성공해야 한다.
- PostgreSQL/Redis/Object Storage는 host에 공개하지 않는다.
- Secret은 Git/image/.env 평문에 저장하지 않는다.
- API와 frontend만 ingress 경계에 연결한다.
- worker/outbox의 app/data/mcp network 경계를 유지한다.
