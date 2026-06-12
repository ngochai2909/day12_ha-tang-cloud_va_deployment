# Lab 6 - FinTrack Production Agent

FinTrack nhan cau noi tu nhien cua nguoi dung va bien thanh giao dich tai chinh an toan:

- AI parser (OpenAI) hieu y dinh
- backend kiem tra vi/danh muc
- tao giao dich JSON + cap nhat so du vi
- frontend co the refetch dashboard de thay doi ngay

## Checklist Deliverable

- [x] Dockerfile multi-stage, non-root, healthcheck
- [x] docker-compose (agent + redis)
- [x] Health (`GET /health`) + Readiness (`GET /ready`)
- [x] API Key auth (`X-API-Key`)
- [x] Rate limiting
- [x] Cost guard ($10/month per user)
- [x] Config tu environment variables
- [x] Structured logging JSON
- [x] Graceful shutdown (`SIGTERM`)
- [x] Render deploy config
- [x] Stateless design (state trong Redis)
- [x] Nginx load balancer

## Cau truc project

```
06-lab-complete/
├── app/
│   ├── main.py              # FinTrack API + production guards
│   ├── config.py            # 12-factor settings
│   ├── fintrack_models.py   # Pydantic models
│   ├── fintrack_parser.py   # OpenAI parser + fallback parser
│   └── fintrack_store.py    # JSON persistence + wallet updates
├── data/
│   ├── wallets.json         # Seed wallets
│   └── transactions.json    # Seed transactions
├── runtime-data/            # Runtime DB (ignored by git)
├── Dockerfile
├── docker-compose.yml
├── render.yaml
├── .env.example
└── check_production_ready.py
```

## Endpoints

- `POST /ask` - parse text + create transaction + return wallet/dashboard snapshot
- `GET /transactions` - list lich su giao dich
- `GET /wallets` - list so du vi
- `GET /dashboard` - tong hop thu/chi/net/by_category/by_wallet
- `GET /health` - liveness
- `GET /ready` - readiness
- `GET /metrics` - basic metrics (protected)

## Chay local (Docker Compose)

```bash
cd 06-lab-complete
docker compose up --build --scale agent=3
```

Dat env de bat OpenAI that:

```bash
export OPENAI_API_KEY="sk-..."
export LLM_MODEL="gpt-4o-mini"
```

Lay API key va test:

```bash
API_KEY=dev-key-change-me

curl -H "X-API-Key: $API_KEY" http://localhost:8080/health

curl -X POST http://localhost:8080/ask \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"user_id":"demo-user","question":"do xang 19k tu vi hang ngay"}'

curl -X POST http://localhost:8080/ask \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"user_id":"demo-user","question":"nhan luong 15 trieu"}'
```

Voi Nginx LB, test qua port 8080:

```bash
curl -H "X-API-Key: $API_KEY" http://localhost:8080/health
curl -H "X-API-Key: $API_KEY" "http://localhost:8080/dashboard?user_id=demo-user"
```

## Luong frontend invalidate cache

Sau khi goi `POST /ask` thanh cong:

1. Invalidate cache `transactions`
2. Invalidate cache `wallets`
3. Invalidate cache `dashboard`
4. Refetch `GET /transactions`, `GET /wallets`, `GET /dashboard`

## Deploy Render

1. Push repo len GitHub
2. Render Dashboard -> New -> Blueprint
3. Chon `06-lab-complete/render.yaml`
4. Set secrets bat buoc: `AGENT_API_KEY`, `OPENAI_API_KEY`
5. Deploy, sau do test:

```bash
curl https://your-service.onrender.com/health
curl -X POST https://your-service.onrender.com/ask \
  -H "X-API-Key: YOUR_KEY" \
  -H "Content-Type: application/json" \
  -d '{"user_id":"demo-user","question":"do xang 25k tu vi hang ngay"}'
```

## Kiem tra production readiness

```bash
cd 06-lab-complete
python check_production_ready.py
```

## Luu y ve du lieu runtime va GitHub

- `data/*.json` la seed data mau de demo.
- Du lieu phat sinh khi user su dung app duoc ghi vao `runtime-data/`.
- `runtime-data/` da duoc ignore trong `.gitignore`, nen ban se khong vo tinh push du lieu user len GitHub.
- Runtime state chinh duoc luu trong Redis de dat yeu cau stateless khi scale nhieu agent instances.
