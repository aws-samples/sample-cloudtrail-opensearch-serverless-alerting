FROM public.ecr.aws/docker/library/python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py query.py ./
# Fail the build on a syntax error instead of shipping an image that crashes at runtime.
RUN python -m py_compile app.py query.py && rm -rf __pycache__

RUN useradd --uid 10001 --no-create-home appuser
USER appuser

# Short-lived batch task: it runs one query, exits, and serves no requests, so there is nothing to
# probe. ECS records success or failure through the exit code (0 = success, 1 = failure).
HEALTHCHECK NONE

# CMD (not ENTRYPOINT) so a one-off task can override it to run query.py.
CMD ["python", "app.py"]
