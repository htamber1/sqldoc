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

## 4. Cloud-managed containers

Templates that deploy the agent as a managed container service with persistent
storage and env-var credentials. All three write your `.sqldoc.yml` from a
base64 secret at startup and mount durable storage at `/data`.

### Azure Container Apps (Bicep) — `deploy/azure-container-app.bicep`
Persists to an Azure Files share mounted at `/data`; public HTTP ingress on 8080.
```bash
az deployment group create -g <rg> -f deploy/azure-container-app.bicep \
  -p image='<registry>/sqldoc:2.7.0' \
     configBase64="$(base64 -w0 .sqldoc.yml)" \
     anthropicApiKey='***'
```

### AWS ECS Fargate (CloudFormation) — `deploy/aws-ecs-fargate.yaml`
Persists to an EFS access point mounted at `/data`; deploy into an existing VPC.
```bash
aws cloudformation deploy --template-file deploy/aws-ecs-fargate.yaml \
  --stack-name sqldoc --capabilities CAPABILITY_IAM \
  --parameter-overrides Image=<ecr-image> VpcId=vpc-xxx \
    SubnetIds=subnet-a,subnet-b ConfigBase64="$(base64 -w0 .sqldoc.yml)"
```

### Google Cloud Run (Terraform) — `deploy/gcp-cloud-run.tf`
Persists to a Cloud Storage bucket mounted at `/data` via gcsfuse.
```bash
terraform -chdir=deploy init
terraform -chdir=deploy apply -var project=<proj> \
  -var image=gcr.io/<proj>/sqldoc:2.7.0 \
  -var config_base64="$(base64 -w0 .sqldoc.yml)"
```

### Notes
- **Persistence matters**: the agent's history, metrics, alert/approval state, and
  rendered docs live in `/data`. Always mount a volume there.
- **Credentials**: pass cloud AI / integration credentials as environment
  variables (secrets) rather than baking them into the image.
