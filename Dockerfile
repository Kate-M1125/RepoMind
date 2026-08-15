FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# bubblewrap 为 CI 补丁测试提供 Linux 网络命名空间隔离；git 用于临时 worktree。
RUN apt-get update && apt-get install -y --no-install-recommends git bubblewrap \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements*.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

RUN useradd --create-home --uid 10001 repomind \
    && mkdir -p /data/jobs /data/repos \
    && chown -R repomind:repomind /app /data
USER repomind

ENV REPOMIND_JOB_DB=/data/jobs/api_jobs.sqlite \
    CLONE_BASE=/data/repos
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "backend.app:app", "--host", "0.0.0.0", "--port", "8000"]
