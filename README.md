# Contract Agent

AI Contract-Expiry Notification Agent — full lifecycle automation with human-in-the-loop, AI agent orchestration, multi-channel outreach, observability, and compliance.

## 🎯 What it does

1. Admin uploads a contract PDF (or system pulls from company API)
2. AI extracts key fields (DOB, start/end date, email, phone, address) with **confidence scores**
3. Low-confidence fields → **human review queue**
4. Reminders scheduled at **T-60 / T-30 / T-14** days before contract end
5. AI agent (smolagents + Ollama) picks best channel: **email / SMS / AI voice call**
6. Voice: voicemail detection (Whisper AMD) → TTS message if machine answers
7. Track customer response within **2 weeks**; escalate to human operator if silent
8. Every event flows through **Kafka / StrikeMQ**
9. Every decision tracked in **MLflow**

## 🛡️ Fail-Safe Principles

- NEVER notify from low-confidence extraction without human review
- NEVER use Twilio free tier — use **textbee**
- NEVER "sleep for N days" — always **daily-scan + date compare**
- ALWAYS log with correlation ID
- ALWAYS idempotent (same PDF twice → no duplicate notifications)
- ALWAYS rollback-safe (DB + Kafka atomicity)
- ALWAYS escalate when uncertain
- ALWAYS free local dev path

## 🚀 Quickstart (Windows PowerShell)

```powershell
# 1. Clone + venv
git clone <repo> contract-agent; cd contract-agent
python -m venv venv
.\venv\Scripts\Activate.ps1

# 2. Install
pip install -e ".[dev]"

# 3. Configure
Copy-Item .env.example .env
# Edit .env — generate ENCRYPTION_KEY:
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# 4. Run
uvicorn contract_agent.main:app --reload --port 8000

# 5. Verify
# http://localhost:8000/health
# http://localhost:8000/health/ready
# http://localhost:8000/metrics
# http://localhost:8000/docs
```

## 🧪 Tests

```powershell
.\scripts\test.ps1
```

## 🛠️ Lint

```powershell
.\scripts\lint.ps1     # check
.\scripts\format.ps1   # auto-fix
```

## 📁 Structure

See `docs/` (added in M33) for architecture diagram and runbook.

## 📄 License

MIT — see `LICENSE`.
