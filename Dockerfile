# sqldoc — production container. Runs the monitoring agent as the entrypoint.
#
#   docker build -t sqldoc:latest .
#   docker run -v $(pwd)/.sqldoc.yml:/app/.sqldoc.yml:ro -v sqldoc-data:/data \
#              -p 8080:8080 sqldoc:latest
#
# The agent reads /app/.sqldoc.yml (mount yours) and keeps its SQLite store in
# /data (mount a volume for persistence). The dashboard listens on 8080.
FROM python:3.11-slim AS base

# --- SQL Server ODBC driver (for pyodbc) + build deps ----------------------
ENV ACCEPT_EULA=Y DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl gnupg apt-transport-https ca-certificates git unixodbc \
    && curl -fsSL https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor -o /usr/share/keyrings/microsoft.gpg \
    && echo "deb [arch=amd64,arm64 signed-by=/usr/share/keyrings/microsoft.gpg] https://packages.microsoft.com/debian/12/prod bookworm main" \
         > /etc/apt/sources.list.d/mssql-release.list \
    && apt-get update \
    && ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . /app

# Install sqldoc with a broad set of optional extras so most enterprise features
# (extra DBs, AI backends, SSO, Active Directory) work out of the box. Trim this
# to `.[all]` (or a narrower set) to slim the image.
RUN pip install --no-cache-dir ".[all,sso,saml,activedirectory]"

# Non-root runtime + a data dir for the agent store.
RUN useradd --create-home --uid 10001 sqldoc \
    && mkdir -p /data && chown -R sqldoc:sqldoc /data /app
USER sqldoc

ENV SQLDOC_AGENT_HOME=/data
VOLUME ["/data"]
EXPOSE 8080

# Run the agent in the foreground (PID 1) so container lifecycle == agent
# lifecycle. Override CMD to run any other sqldoc command.
ENTRYPOINT ["sqldoc"]
CMD ["agent", "start", "--foreground"]
