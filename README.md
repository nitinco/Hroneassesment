# Candidate kit

Read in this order:

1. `PROBLEM_STATEMENT.docx`: what you are asked to do, how it is evaluated.
2. `openapi.yaml`: the API contract and the business rules (R1 - R10).
3. `DATA_MODEL.md`: what is stored in MongoDB, with nine sample documents.
4. `app/main.py`: the starter. Put **all** your code in this one file. It must start with `uvicorn app.main:app --port 8000`.
5. `REVIEW.md`, `DECISIONS.md`: templates to fill in.

`sample_data/` holds the nine sample documents as MongoDB Extended JSON; `sample_seed.py` loads them. The grader's dataset is
much larger and contains cases they do not show.

## Local setup
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # set MONGO_URI / MONGO_DB (local docker: docker run -p 27017:27017 mongo:7)
python sample_seed.py
uvicorn app.main:app --port 8000 --reload
```
MongoDB **6.0 or newer** is required (the grader uses 7.0). MongoDB Atlas free tier (M0) works.

## Submit
A public Git repository with `app/main.py`, `requirements.txt`, `REVIEW.md`, `DECISIONS.md` and a short `README.md`.
No `.env`, no secrets, no Dockerfile.

## Validation checklist

From the project directory, run:

```bash
python -m py_compile app/main.py
python sample_seed.py
uvicorn app.main:app --port 8000
```

Then open `http://127.0.0.1:8000/docs` or check `http://127.0.0.1:8000/health`.
The root URL `/` is intentionally not an API route and may return 404.

## Smoke-test checklist

The following commands use `curl.exe` on Windows PowerShell. Run them in order after MongoDB and Uvicorn are running.

```powershell
# 1. Readiness and contract
curl.exe -i http://127.0.0.1:8000/health
curl.exe -i http://127.0.0.1:8000/openapi.json

# 2. Create an employee
curl.exe -i -X POST http://127.0.0.1:8000/employees `
  -H "Content-Type: application/json" `
  -d '{"emp_code":"EMP9001","name":"Smoke User","email":"smoke@example.com","department":"QA","shift_start":"09:30","shift_end":"18:30","joined_on":"2026-10-01"}'

# 3. List employees with pagination
curl.exe -i "http://127.0.0.1:8000/employees?department=QA&page=1&page_size=20"

# 4. Punch in at 09:40 IST (04:10 UTC), inside the grace period
curl.exe -i -X POST http://127.0.0.1:8000/attendance/punch-in `
  -H "Content-Type: application/json" `
  -d '{"emp_code":"EMP9001","punched_at":1791778200000,"status":"PRESENT"}'

# 5. Punch out at 18:35 IST (13:05 UTC)
curl.exe -i -X POST http://127.0.0.1:8000/attendance/punch-out `
  -H "Content-Type: application/json" `
  -d '{"emp_code":"EMP9001","punched_at":1791810300000}'

# 6. List attendance with filters
curl.exe -i "http://127.0.0.1:8000/attendance?emp_code=EMP9001&date_from=2026-10-01&date_to=2026-10-31&status=PRESENT&page=1&page_size=20"

# 7. Regularize the punch-in and verify history
curl.exe -i -X PATCH http://127.0.0.1:8000/attendance/EMP9001/2026-10-12 `
  -H "Content-Type: application/json" `
  -d '{"punch_in":1791777600000,"reason":"correct biometric time","regularized_by":"qa.admin"}'

# 8. Analytics
curl.exe -i "http://127.0.0.1:8000/analytics/employees/EMP9001/monthly?month=2026-10"
curl.exe -i "http://127.0.0.1:8000/analytics/departments/summary?month=2026-10"
curl.exe -i "http://127.0.0.1:8000/analytics/leaderboard/late?month=2026-10&limit=10"
curl.exe -i "http://127.0.0.1:8000/analytics/departments/QA/trend?from=2026-10-01&to=2026-10-31"

# 9. Explain plans
curl.exe -i "http://127.0.0.1:8000/admin/explain/attendance_list?date_from=2026-10-01&date_to=2026-10-31&page=1&page_size=20"
curl.exe -i "http://127.0.0.1:8000/admin/explain/employee_monthly?emp_code=EMP9001&month=2026-10"
curl.exe -i "http://127.0.0.1:8000/admin/explain/department_trend?department=QA&from=2026-10-01&to=2026-10-31"

# 10. Expected negative checks
curl.exe -i -X POST http://127.0.0.1:8000/employees `
  -H "Content-Type: application/json" `
  -d '{"emp_code":"EMP9001","name":"Duplicate","email":"duplicate@example.com","department":"QA","joined_on":"2026-10-01"}'
curl.exe -i "http://127.0.0.1:8000/analytics/departments/QA/trend?from=2026-10-01&to=2027-01-01"
```

Expected status checks: health/docs `200`, first employee create `201`, duplicate employee `409`, valid punch-in `201`, valid punch-out `200`, regularization `200`, and invalid/range-conflict requests `422`.

The same payloads can be entered in Swagger UI at `http://127.0.0.1:8000/docs`; use **Try it out**, paste the JSON body, and execute each operation in the same order.
