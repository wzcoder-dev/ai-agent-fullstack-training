FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml ./
COPY app ./app
COPY prompts ./prompts
COPY gateway.example.yaml ./

RUN pip install --no-cache-dir .

EXPOSE 8000

# 用配置工厂启动；配置路径经 GATEWAY_CONFIG 指定（默认读 gateway.example.yaml）
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
