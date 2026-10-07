import os

# Tests never talk to the user's real credentials: point at the docker-compose
# Postgres unless the caller exported something explicit.
os.environ.setdefault("DATABASE_URL", "postgresql+psycopg2://postgres:postgres@localhost:5433/meritech_db")
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-test-secret-test-secret-123456")
