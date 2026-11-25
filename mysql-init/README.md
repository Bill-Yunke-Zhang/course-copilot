# mysql-init

SQL files in this directory run once when the MySQL container initializes a
fresh data volume (standard `docker-entrypoint-initdb.d` behavior). The
application schema itself is managed by Alembic (`backend/app/migrations/`),
not by files here — this directory exists for grants or bootstrap SQL that
must run before the app connects.
