# Deploying sqldoc

Three ways to run the sqldoc monitoring agent as a container. All three run the
agent in the foreground as PID 1, read your `.sqldoc.yml`, keep the SQLite store
on a persistent volume (`SQLDOC_AGENT_HOME=/data`), and expose the dashboard on
port 8080.

> The image bundles the Microsoft ODBC Driver 18 (for SQL Server via pyodbc) and
> a broad set of optional extras. Trim the `pip install` line in the `Dockerfile`
> to slim the image for your dialect.

---

## 1. Docker (single container)

```bash
docker build -t sqldoc:latest .
docker run -d --name sqldoc-agent \
  -v "$(pwd)/.sqldoc.yml:/app/.sqldoc.yml:ro" \
  -v sqldoc-data:/data \
  -p 8080:8080 \
  -e ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" \
  sqldoc:latest
```

Open the dashboard at http://localhost:8080. Run any other command by overriding
the entrypoint args, e.g. a one-off scan:

```bash
docker run --rm -v "$(pwd)/.sqldoc.yml:/app/.sqldoc.yml:ro" sqldoc:latest \
  scan --connection-string "$DB_CONN" --output /data/pii.html
```

## 2. Docker Compose (local / single host)

```bash
# put your config in ./.sqldoc.yml first
docker compose up -d
docker compose logs -f
```

Persists the agent store in the named volume `sqldoc-data`. Uncomment the
`command:` line in `docker-compose.yml` to run the REST API (`serve`) instead of
the agent.

## 3. Kubernetes (Helm)

```bash
# render your .sqldoc.yml into the release (or use existingSecret)
helm install sqldoc ./helm/sqldoc \
  --set-file config=./.sqldoc.yml \
  --set image.tag=2.7.0

# dashboard
kubectl port-forward svc/sqldoc 8080:8080
```

The chart creates a `Deployment` (single replica, `Recreate` strategy — the
store is stateful), a `Service`, a `PersistentVolumeClaim` for `/data`, and a
`Secret` holding your config (or references `existingSecret`). Configure
resources, storage class, node placement, and extra env/secret credentials in
`helm/sqldoc/values.yaml`.

```bash
helm template sqldoc ./helm/sqldoc --set-file config=./.sqldoc.yml   # dry-run
helm upgrade sqldoc ./helm/sqldoc --set-file config=./.sqldoc.yml    # update
```

---

### Notes
- **Persistence matters**: the agent's history, metrics, alert/approval state, and
  rendered docs live in `/data`. Always mount a volume there.
- **Credentials**: pass cloud AI / integration credentials as environment
  variables (`env` / `envFromSecret` in the chart) rather than baking them in.
- **Cloud-managed containers** (Azure Container Apps, AWS ECS Fargate, Google
  Cloud Run): see the templates under `deploy/` (`azure-container-app.bicep`,
  `aws-ecs-fargate.yaml`, `gcp-cloud-run.tf`).
