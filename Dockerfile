FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /opt/minipacs
RUN groupadd --gid 10001 minipacs && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin minipacs
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY templates ./templates
COPY static ./static
RUN chown -R root:root /opt/minipacs && chmod -R a=rX /opt/minipacs
USER 10001:10001
EXPOSE 8000 11112
