FROM ghcr.io/open-webui/open-webui@sha256:41812075aede8ada8497cb2b71c76412978633eee72775bb77796f593d03b677
# These helpers are already present in the qualified image. Never pip-upgrade
# OWUI's dependency graph just to obtain a test client.
RUN python -c 'import importlib.metadata as m; expected={"python-socketio":"5.16.2","aiohttp":"3.14.5","redis":"8.0.1","psycopg2-binary":"2.9.12","pgvector":"0.4.2","httpx":"0.28.1"}; assert all(m.version(k)==v for k,v in expected.items()); import socketio, aiohttp, redis, psycopg2, pgvector, httpx' \
    && python -m pip install --no-deps --disable-pip-version-check 'https://files.pythonhosted.org/packages/96/c5/1e741d26306c42e2bf6ab740b2202872727e0f606033c9dd713f8b93f5a8/cachetools-6.2.1-py3-none-any.whl#sha256=09868944b6dde876dfd44e1d47e18484541eaf12f26f29b7af91b26cc892d701' \
    && python -c 'import cachetools, yaml, pydantic; assert int(pydantic.__version__.split(".")[0]) >= 2' \
    && python -m pip check
COPY worker.py entrypoint.py preflight.py guard.py transport_preflight.py /opt/readiness/
ENV PYTHONPATH=/opt/readiness:/app/backend \
    OFFLINE_MODE=true HF_HUB_OFFLINE=1 ENABLE_VERSION_UPDATE_CHECK=false \
    ENABLE_OPENAI_API=false ENABLE_OLLAMA_API=false \
    OLLAMA_BASE_URL=http://127.0.0.1:9 OPENAI_API_BASE_URL=http://127.0.0.1:9/v1 \
    ENABLE_DB_MIGRATIONS=false ENABLE_PERSISTENT_CONFIG=false \
    ENABLE_PIP_INSTALL_FRONTMATTER_REQUIREMENTS=false \
    ENABLE_SIGNUP=true WEBUI_AUTH=true \
    WEBSOCKET_MANAGER=redis VECTOR_DB=pgvector \
    ENABLE_WEBSOCKET_SUPPORT=true \
    DATA_DIR=/app/backend/data
WORKDIR /app/backend
ENTRYPOINT ["python", "/opt/readiness/entrypoint.py"]
